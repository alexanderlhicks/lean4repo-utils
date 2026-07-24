"""Lean inspection tools for the review agents (CLI backend).

Gives agents a way to check claims against the real Lean toolchain instead of
guessing — "does this actually typecheck?", "does this lemma exist and what is
its type?", "what does this definition/lemma state?", "what axioms does it
depend on?". This kills the most common false-positive class: confident but
wrong claims about Lean semantics (e.g. "this won't typecheck" when CI builds it
fine).

Backend: `lake env lean --stdin`, the same mechanism as ``lean_info_extractor``.
The public surface — :meth:`LeanToolbox.specs` (OpenAI tool schemas) and
:meth:`LeanToolbox.run` — is a stable interface, so a richer ``lean-lsp-mcp``
backend (goal state, diagnostics, loogle/leansearch) can be slotted in later
without touching the agent-side wiring.

Safety: the Lean subprocess runs with secret-looking environment variables
scrubbed, so model-directed code cannot read credentials (e.g.
``#eval IO.getEnv "GITHUB_TOKEN"``) and smuggle them into the review output.
"""

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

# Env var names matching this are removed from the Lean subprocess environment.
_SECRET_ENV_RE = re.compile(r'(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)', re.IGNORECASE)

# Cap on a single tool's returned text, to keep tool results from bloating the
# conversation fed back to the model.
MAX_TOOL_OUTPUT_CHARS = 4000


def lean_available() -> bool:
    """True if the `lake` executable is on PATH (tools degrade to no-op otherwise)."""
    return shutil.which("lake") is not None


def scrubbed_env() -> Dict[str, str]:
    """A copy of the process env with secret-looking variables removed.

    `lake env lean` elaborates PR-controlled code (the imported module), so the
    child process must never inherit API keys or tokens: in the secret-bearing
    run-review step an elaboration-time exploit could otherwise read
    `API_KEY`/`GITHUB_TOKEN` straight from its environment. Scrubbing is the
    default for every Lean subprocess spawned by the review action (both the
    model-directed toolbox here and the axiom/coverage extractors in
    lean_info_extractor, which import this). Full sandboxing of model-directed
    Lean IO is the separate S7 roadmap item.
    """
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}


def _run_lean(command: str, module: Optional[str], timeout: int) -> str:
    """Run `command` through `lake env lean --stdin`, optionally importing
    `module` first so its declarations are in scope. Returns combined
    stdout+stderr (truncated), or a bounded error string on failure/timeout."""
    prelude = f"import {module}\n" if module else ""
    code = f"{prelude}{command}\n"
    try:
        result = subprocess.run(
            ["lake", "env", "lean", "--stdin"],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=scrubbed_env(),
        )
    except subprocess.TimeoutExpired:
        return f"(lean tool timed out after {timeout}s)"
    except (FileNotFoundError, OSError) as e:
        return f"(lean tool unavailable: {e})"
    out = (result.stdout + result.stderr).strip()
    if not out:
        return "(no output — elaborated with no messages)"
    return out[:MAX_TOOL_OUTPUT_CHARS]


class LeanToolbox:
    """Lean-inspection tools scoped to a `module` (the file under review, which
    is imported so its declarations are in scope).

    Toolbox interface used by the provider tool loop:
      * ``specs()`` -> list of OpenAI function-tool schemas
      * ``run(name, args)`` -> tool result text
    """

    def __init__(self, module: Optional[str] = None, timeout: int = 30):
        self.module = module
        self.timeout = timeout

    def specs(self) -> List[dict]:
        scope = f" (with `{self.module}` imported)" if self.module else ""
        return [
            {"type": "function", "function": {
                "name": "lean_check",
                "description": (
                    f"Run Lean `#check` on an expression{scope} to get its type, or an "
                    "error if it does not elaborate. Use to confirm a name exists and what "
                    "its type/signature is."
                ),
                "parameters": {"type": "object", "properties": {
                    "expr": {"type": "string", "description": "Expression or name to #check, e.g. 'List.map' or '(2 : Nat) + 2'."},
                }, "required": ["expr"]},
            }},
            {"type": "function", "function": {
                "name": "lean_print",
                "description": (
                    f"Run Lean `#print` on a declaration{scope} to see its definition or "
                    "statement. Use to confirm what a lemma actually states or what a "
                    "definition unfolds to."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Fully-qualified declaration name."},
                }, "required": ["name"]},
            }},
            {"type": "function", "function": {
                "name": "lean_print_axioms",
                "description": (
                    f"Run Lean `#print axioms`{scope} to list the axioms a declaration "
                    "depends on (e.g. detect `sorryAx`, `Classical.choice`)."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Fully-qualified declaration name."},
                }, "required": ["name"]},
            }},
            {"type": "function", "function": {
                "name": "lean_typecheck",
                "description": (
                    f"Elaborate a Lean code snippet{scope} and return diagnostics "
                    "(errors/warnings), or a note that it elaborated cleanly. Use to test "
                    "whether specific code actually compiles BEFORE claiming it does or does not."
                ),
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "Lean code to elaborate; may reference declarations from the imported module."},
                }, "required": ["code"]},
            }},
        ]

    def run(self, name: str, args: dict) -> str:
        args = args or {}
        if name == "lean_check":
            return _run_lean(f"#check {args.get('expr', '')}", self.module, self.timeout)
        if name == "lean_print":
            return _run_lean(f"#print {args.get('name', '')}", self.module, self.timeout)
        if name == "lean_print_axioms":
            return _run_lean(f"#print axioms {args.get('name', '')}", self.module, self.timeout)
        if name == "lean_typecheck":
            return _run_lean(args.get("code", ""), self.module, self.timeout)
        return f"(unknown tool: {name})"
