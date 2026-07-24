"""Lean Info Extractor — Extracts verified facts from Lean's toolchain.

Runs after `lake build` to extract:
1. Axiom dependencies for each declaration (`#print axioms`)
2. Type signatures for key definitions (`#check`)
3. Compiler warnings (sorry, unused variables, etc.)

This provides ground-truth information that complements LLM-based review.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time as _time
from typing import Dict, List, Optional

from leanrepo_common.lean_utils import (
    file_path_to_module_name,
    keyword_pattern,
    resolve_confined_path,
    scrub_line,
)

# Same secret denylist the reviewer's lean_tools subprocesses already use for
# the identical threat (case-insensitive, matches anywhere in the name): a
# narrower suffix-only list would leave DEPLOY_SECRET/DB_PASSWORD/lowercase
# spellings visible to import-time Lean IO in the secret-bearing step.
from lean_tools import _SECRET_ENV_RE


_DECL_RE = re.compile(
    # Attributes commonly share the declaration line (`@[simp] theorem ...`).
    # The previous expression only recognized attributes on their own line and
    # silently dropped the declaration—and therefore its axiom query.
    r'^\s*(?:@\[[^\]\n]*\]\s*)*'
    r'(?P<mods>(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+)*)'
    r'(?:def|theorem|lemma|abbrev|instance|structure|class|inductive|opaque|axiom)\s+'
    # Declaration name: must not start with a binder/type delimiter (rejects
    # anonymous `instance : Foo`, whose next token is `:`).
    r'(?P<name>(?![:({\[⦃|])[^\s:({\[⦃]+)'
)
_NAMESPACE_RE = re.compile(r'^\s*namespace\s+(\S+)\s*$')
_SECTION_RE = re.compile(r'^\s*(?:noncomputable\s+)?section(?:\s+(\S+))?\s*$')
_MUTUAL_RE = re.compile(r'^\s*mutual\s*$')
_END_RE = re.compile(r'^\s*end(?:\s+(\S+))?\s*$')


def get_lean_declarations(file_path: str) -> List[str]:
    """Extracts namespace-qualified declaration names from a Lean file.

    Tracks `namespace`/`section`/`mutual` … `end` blocks (over comment- and
    string-scrubbed lines) so a `def bar` under `namespace Foo` is reported as
    `Foo.bar` — the name `#print axioms` can actually resolve. `_root_.`-anchored
    names are returned absolute. `private` declarations and anonymous instances
    are skipped: they have no name resolvable from an importing module.
    """
    if not os.path.exists(file_path):
        return []

    declarations = []
    # Scope stack frames: ('ns', component) — one frame per namespace component,
    # so `end A.B` pops two — ('section', name-or-None), or ('mutual', None).
    scopes: List[tuple] = []
    try:
        comment_depth = 0
        in_string = False
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                code, comment_depth, in_string = scrub_line(line, comment_depth, in_string)

                m = _NAMESPACE_RE.match(code)
                if m:
                    for component in m.group(1).split('.'):
                        if component:
                            scopes.append(('ns', component))
                    continue
                m = _SECTION_RE.match(code)
                if m:
                    scopes.append(('section', m.group(1)))
                    continue
                if _MUTUAL_RE.match(code):
                    scopes.append(('mutual', None))
                    continue
                m = _END_RE.match(code)
                if m:
                    # `end` with a k-component name closes k namespace frames
                    # (or one named section); a bare `end` closes one frame.
                    pops = len([c for c in m.group(1).split('.') if c]) if m.group(1) else 1
                    for _ in range(min(pops, len(scopes))):
                        scopes.pop()
                    continue

                m = _DECL_RE.match(code)
                if m:
                    if 'private' in m.group('mods'):
                        continue
                    # Trim a trailing dot the greedy name class can pick up before
                    # an explicit-universe binder (`def foo.{u}` → `foo.`), so the
                    # #print axioms query uses the real name, not `foo.`.
                    name = m.group('name').rstrip('.')
                    if not name:
                        continue
                    if name.startswith('_root_.'):
                        declarations.append(name[len('_root_.'):])
                        continue
                    prefix = '.'.join(c for kind, c in scopes if kind == 'ns')
                    declarations.append(f"{prefix}.{name}" if prefix else name)
    except (OSError, UnicodeError):
        pass
    return declarations


def get_module_name(file_path: str) -> Optional[str]:
    """Convert a file path to a Lean module name."""
    return file_path_to_module_name(file_path)


def scrubbed_env() -> Dict[str, str]:
    """A copy of the process env with secret-looking variables removed.

    `lake env lean` elaborates PR-controlled code (the imported module), so the
    child process must never inherit API keys or tokens: in the secret-bearing
    run-review step an elaboration-time exploit could otherwise read
    `API_KEY`/`GITHUB_TOKEN` straight from its environment. Scrubbing is the
    default for every Lean subprocess spawned here — the pre-secret extractor
    step loses nothing by it. (Full sandboxing of model-directed Lean IO is the
    separate S7 roadmap item.)
    """
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


def run_lean_command(
    module_name: str,
    command: str,
    timeout: int = 30,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Runs a Lean command in the context of a module via `lake env lean`.

    The child env defaults to :func:`scrubbed_env` (secrets removed); pass an
    explicit ``env`` only in tests.
    """
    # Create a temporary Lean file that imports the module and runs the command
    lean_code = f"import {module_name}\n{command}\n"
    try:
        result = subprocess.run(
            ['lake', 'env', 'lean', '--stdin'],
            input=lean_code,
            capture_output=True,
            text=True,
            errors='replace',
            timeout=timeout,
            env=env if env is not None else scrubbed_env(),
        )
        # Combine stdout and stderr — Lean puts #check/#print output on stdout,
        # warnings on stderr
        return (result.stdout + result.stderr).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, UnicodeError):
        return None


def extract_axioms(
    module_name: str,
    declarations: List[str],
    deadline: Optional[float] = None,
) -> tuple[Dict[str, List[str]], List[str]]:
    """Extracts axiom dependencies for each declaration using #print axioms.

    ``declarations`` are the namespace-qualified names from
    :func:`get_lean_declarations`; they are queried as-is (the module path is
    only used for the ``import``, never as a name prefix — module paths are not
    namespaces). ``deadline`` is a ``time.monotonic()`` timestamp: once passed,
    remaining declarations are skipped with a recorded truncation error, so a
    file full of slow declarations cannot blow the caller's overall budget.

    Returns ``(axiom_map, errors)`` — failures are reported, never silently
    dropped, so an empty axiom section is distinguishable from a broken one.
    """
    axiom_map: Dict[str, List[str]] = {}
    errors: List[str] = []
    for i, decl in enumerate(declarations):
        timeout = 30
        if deadline is not None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                errors.append(
                    f"Axiom-extraction time budget exhausted in {module_name}: "
                    f"skipped {len(declarations) - i} remaining declaration(s)."
                )
                break
            timeout = max(1, min(30, int(remaining)))

        output = run_lean_command(module_name, f"#print axioms {decl}", timeout=timeout)
        if output is None:
            errors.append(f"`#print axioms {decl}` failed (lean invocation error or timeout).")
            continue

        # Lean 4 `#print axioms Foo.bar` emits one of:
        #     'Foo.bar' depends on axioms: [propext, Classical.choice, Quot.sound]
        #     'Foo.bar' does not depend on any axioms
        # The bracketed list may wrap across lines for long dependency sets, so
        # match across the whole output (DOTALL) and split the list on commas.
        if "does not depend on any axioms" in output:
            axiom_map[decl] = []
            continue

        match = re.search(r'depends on axioms:\s*\[(.*?)\]', output, re.DOTALL)
        if not match:
            # No recognizable axiom list (e.g. unknown constant) — record it
            # rather than treating the message text as an axiom name.
            first_line = output.splitlines()[0][:200] if output else "(empty output)"
            errors.append(f"`#print axioms {decl}` gave no axiom list: {first_line}")
            continue
        axioms = [a.strip() for a in match.group(1).split(',') if a.strip()]
        if axioms:
            axiom_map[decl] = axioms

    return axiom_map, errors


def extract_sorry_warnings(file_path: str) -> List[str]:
    """Checks for sorry/admit by scanning source with nested block comment awareness."""
    sorry_locations = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            comment_depth = 0
            in_string = False
            for i, line in enumerate(f, 1):
                code, comment_depth, in_string = scrub_line(line, comment_depth, in_string)
                # Canonical shared matcher (C5): standalone-token boundary, so
                # `sorry'`/`sorryAx` don't false-hit. Kept as two checks to
                # preserve the one-location-per-keyword behaviour.
                if keyword_pattern('sorry').search(code):
                    sorry_locations.append(f"{file_path}:{i}")
                if keyword_pattern('admit').search(code):
                    sorry_locations.append(f"{file_path}:{i}")
    except (OSError, UnicodeError):
        pass
    return sorry_locations


def extract_diagnostics(file_path: str, timeout: int = 60) -> List[str]:
    """Captures compiler diagnostics (warnings, errors) for a single Lean file.
    Runs after lake build, so imports use cached oleans."""
    try:
        result = subprocess.run(
            ['lake', 'env', 'lean', file_path],
            capture_output=True,
            text=True,
            errors='replace',
            timeout=timeout
        )
        diagnostics = []
        for line in (result.stderr + result.stdout).splitlines():
            line = line.strip()
            if line and ('warning' in line.lower() or 'error' in line.lower()):
                diagnostics.append(line)
        return diagnostics
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, UnicodeError):
        return []


def extract_info_for_files(changed_files: List[str], time_budget: int = 300) -> Dict:
    """Main extraction function with a total time budget (seconds).
    Returns structured info for all changed Lean files."""
    start = _time.monotonic()
    results = {
        "files": {},
        "axiom_summary": {},
        "sorry_locations": [],
        "diagnostics": [],
        "errors": []
    }

    for file_path in changed_files:
        if not file_path.endswith('.lean'):
            continue
        if not os.path.lexists(file_path):
            continue
        if resolve_confined_path(file_path, os.getcwd(), "file") is None:
            results["errors"].append(
                f"Skipped unsafe changed path {file_path}: expected a regular file inside the checkout."
            )
            continue

        elapsed = _time.monotonic() - start
        if elapsed > time_budget:
            remaining = [f for f in changed_files if f not in results["files"] and f.endswith('.lean')]
            results["errors"].append(
                f"Time budget ({time_budget}s) exceeded after {elapsed:.0f}s. "
                f"Skipped {len(remaining)} remaining file(s)."
            )
            break

        file_info = {
            "declarations": [],
            "axiom_dependencies": {},
            "sorry_locations": [],
            "diagnostics": []
        }

        declarations = get_lean_declarations(file_path)
        file_info["declarations"] = declarations

        module_name = get_module_name(file_path)
        if module_name:
            # Extract axiom dependencies with per-file time awareness
            remaining_budget = time_budget - (_time.monotonic() - start)
            if len(declarations) <= 50 and remaining_budget > 30:
                axiom_map, axiom_errors = extract_axioms(
                    module_name, declarations, deadline=start + time_budget
                )
                file_info["axiom_dependencies"] = axiom_map
                if axiom_errors:
                    # Bound per-file error spam: these end up in an LLM prompt.
                    shown = axiom_errors[:5]
                    if len(axiom_errors) > len(shown):
                        shown.append(
                            f"… and {len(axiom_errors) - len(shown)} more axiom-extraction "
                            f"error(s) in {file_path}."
                        )
                    results["errors"].extend(shown)

                STANDARD_AXIOMS = {'propext', 'Quot.sound', 'Classical.choice'}
                for decl, axioms in axiom_map.items():
                    # decl is already the namespace-qualified name.
                    non_standard = [a for a in axioms if a not in STANDARD_AXIOMS]
                    if non_standard:
                        results["axiom_summary"][decl] = non_standard
            elif len(declarations) > 50:
                results["errors"].append(f"Skipped axiom extraction for {file_path}: too many declarations ({len(declarations)})")
            else:
                results["errors"].append(f"Skipped axiom extraction for {file_path}: insufficient time budget remaining")

            # Extract compiler diagnostics if time permits
            remaining_budget = time_budget - (_time.monotonic() - start)
            if remaining_budget > 60:
                diags = extract_diagnostics(file_path, timeout=max(1, min(60, int(remaining_budget))))
                file_info["diagnostics"] = diags
                results["diagnostics"].extend(diags)

        sorry_locs = extract_sorry_warnings(file_path)
        file_info["sorry_locations"] = sorry_locs
        results["sorry_locations"].extend(sorry_locs)

        results["files"][file_path] = file_info

    return results


def format_for_review(info: Dict) -> str:
    """Formats extracted info as a string suitable for injection into LLM prompts."""
    parts = ["**Lean Toolchain Analysis (compiler-verified facts):**\n"]

    # Sorry locations
    if info["sorry_locations"]:
        parts.append("**Incomplete Proofs (sorry/admit):**")
        for loc in info["sorry_locations"]:
            parts.append(f"- `{loc}`")
        parts.append("")

    # Non-standard axiom dependencies
    if info["axiom_summary"]:
        parts.append("**Non-Standard Axiom Dependencies:**")
        for decl, axioms in info["axiom_summary"].items():
            parts.append(f"- `{decl}` depends on: {', '.join(f'`{a}`' for a in axioms)}")
        parts.append("")

    # Compiler diagnostics
    if info.get("diagnostics"):
        parts.append("**Compiler Diagnostics:**")
        for diag in info["diagnostics"]:
            parts.append(f"- `{diag}`")
        parts.append("")

    # Per-file declaration counts
    for file_path, file_info in info["files"].items():
        decl_count = len(file_info["declarations"])
        axiom_count = sum(1 for v in file_info["axiom_dependencies"].values() if v)
        if axiom_count > 0:
            parts.append(f"- `{file_path}`: {decl_count} declarations, {axiom_count} with axiom dependencies")

    if info["errors"]:
        parts.append("\n**Extraction Warnings:**")
        for err in info["errors"]:
            parts.append(f"- {err}")

    if len(parts) == 1:
        parts.append("No issues detected by the Lean toolchain.")

    return "\n".join(parts)


def extract_light_info(summary_files: List[str]) -> Dict:
    """Light scanning for summary-context files: sorry/admit detection only (no subprocess calls)."""
    results = {"sorry_locations": [], "files_scanned": 0}
    for file_path in summary_files:
        if (not file_path.endswith('.lean')
                or resolve_confined_path(file_path, os.getcwd(), "file") is None):
            continue
        sorry_locs = extract_sorry_warnings(file_path)
        if sorry_locs:
            results["sorry_locations"].extend(sorry_locs)
        results["files_scanned"] += 1
    return results


def main():
    """CLI entry point. Reads changed files from args or CHANGED_FILES env var."""
    if len(sys.argv) > 1:
        changed_files = sys.argv[1].split(',')
    else:
        changed_files_str = os.environ.get('CHANGED_FILES', '')
        if not changed_files_str:
            print("::error::No changed files provided. Pass as argument or set CHANGED_FILES env var.")
            sys.exit(1)
        changed_files = [f.strip() for f in changed_files_str.split(',') if f.strip()]

    info = extract_info_for_files(changed_files)

    # Light scan summary-context files (sorry/admit only, no expensive axiom extraction)
    summary_files_str = os.environ.get('SUMMARY_FILES', '')
    if summary_files_str:
        summary_files = [f.strip() for f in summary_files_str.split(',') if f.strip()]
        light_info = extract_light_info(summary_files)
        if light_info["sorry_locations"]:
            info["sorry_locations"].extend(light_info["sorry_locations"])
            logging.info(f"Light scan of {light_info['files_scanned']} summary files found "
                        f"{len(light_info['sorry_locations'])} sorry/admit locations.")

    # Output as JSON for machine consumption
    json_output = json.dumps(info, indent=2)

    # Output as formatted text for LLM consumption
    formatted_output = format_for_review(info)

    # Write both outputs
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        import random
        import string
        eof_marker = 'EOF_LEAN_' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
        with open(github_output, 'a') as f:
            f.write(f"lean_info_json<<{eof_marker}_JSON\n")
            f.write(json_output + "\n")
            f.write(f"{eof_marker}_JSON\n")
            f.write(f"lean_info_formatted<<{eof_marker}\n")
            f.write(formatted_output + "\n")
            f.write(f"{eof_marker}\n")

    print(formatted_output)


if __name__ == "__main__":
    main()
