"""Deterministic coverage-matrix symbol resolution (R7 Gap B).

Given a repo-maintained coverage/audit matrix (paper result → Lean declaration
→ proven/admitted status), resolve every named symbol against the real
compiler (`#print axioms` via the same extractor the review pipeline already
trusts) and compare the claimed status with the kernel's answer. An
unresolved symbol or a status mismatch becomes a finding; infrastructure
failures become warnings, never findings.

Security model: the matrix is PR-tree content and therefore fully
attacker-influenced. The PARSER is the validation boundary — a row reaches
the Lean runner only after its symbol passes a strict Lean-identifier
allowlist and its file path resolves to a confined, non-symlink `.lean`
regular file inside the repository root. Rejected rows produce per-row
warnings and are never executed. See review/BOOTSTRAP_toolkit-gaps.md (Gap B)
for the spec; the accepted status vocabulary is documented on the
`coverage_matrix_path` input in action.yml.

Provenance: written clean-room from the committed bootstrap doc alone
(review/BOOTSTRAP_toolkit-gaps.md @ 982fe55). The unlicensed prior-art review
scripts (ArkLib PR505_FRESH_REVIEW_2026-07-17/scripts, abf26-review-mirror)
were not consulted; the ledger/matrix schemas are modeled on the format (not
content) of the untracked Review-B artifacts those reviews produced.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from leanrepo_common.lean_utils import file_path_to_module_name, resolve_confined_path

from lean_info_extractor import extract_axioms

# Caps: hostile or degenerate matrices must cost bounded parse time and a
# bounded number of Lean subprocesses. Truncation is warned, never silent.
MAX_MATRIX_BYTES = 1_000_000
MAX_MATRIX_ROWS = 100
# The phase owns its own budget: extract_axioms takes a deadline but has no
# ambient caller deadline here (the LEAN_INFO extractor's 300s budget belongs
# to a different step).
PHASE_BUDGET_SECONDS = 600.0

# Status vocabulary (word-boundary, case-insensitive). Must stay in sync with
# the `coverage_matrix_path` input description in action.yml.
_PROVEN_WORDS = ("proven", "complete", "done")
_ADMITTED_WORDS = ("sorry", "admitted", "partial", "wip", "stated")
# A negation within two tokens of a status keyword ("not proven", "no longer
# done") makes the row ambiguous — skip with a warning, never guess.
_NEGATION_WORDS = {"not", "no", "never", "isn't", "un"}

_BACKTICKED_CELL_RE = re.compile(r"^`([^`]+)`$")
_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:-]+$")


@dataclass
class MatrixRow:
    """One validated matrix claim, safe to hand to the Lean runner."""
    symbol: str
    file_path: str      # repo-relative, as written (after ./-strip)
    module: str
    claimed: str        # "proven" | "admitted"
    line_no: int


def is_valid_lean_symbol(symbol: str) -> bool:
    """Strict allowlist for a fully-qualified Lean identifier.

    Every dot-separated component must start with a letter (any Unicode
    letter — Lean names use Greek etc.) or underscore, and continue with
    letters, digits (including subscripts), underscore, or prime. Everything
    else — whitespace, newlines, `#`, backslash, brackets, guillemets,
    operators — is rejected, so a symbol can never smuggle Lean syntax into
    the `#print axioms` command line.
    """
    if not symbol or len(symbol) > 200:
        return False
    for component in symbol.split("."):
        if not component:
            return False
        if not (component[0].isalpha() or component[0] == "_"):
            return False
        for ch in component:
            if not (ch.isalnum() or ch in "_'"):
                return False
    return True


def _status_from_cells(cells: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Read the claimed status from candidate status cells.

    Returns (status, problem): status is "proven"/"admitted" or None;
    problem explains an ambiguous/contradictory read. Per-cell semantics:
    the caller passes only cells that are neither the symbol cell nor the
    file-path cell, so `foo_proven_iff` or `Proofs/Sorry.lean` can never set
    a status.
    """
    def _hit(cell: str, words: Tuple[str, ...]) -> Optional[str]:
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", cell, re.IGNORECASE):
                tokens = re.findall(r"[\w']+", cell.lower())
                try:
                    idx = tokens.index(word)
                except ValueError:
                    idx = -1
                if idx >= 0 and any(t in _NEGATION_WORDS for t in tokens[max(0, idx - 2):idx]):
                    return "negated"
                return word
        return None

    # A cell that IS a status keyword (nothing else) is the status cell.
    # When one exists, free-text cells (the paper-result description column,
    # which naturally contains words like "stated" or "done") do not get a
    # vote — otherwise ordinary prose silently vetoes legitimate rows.
    exact = [
        c for c in cells
        if c.strip().lower() in _PROVEN_WORDS + _ADMITTED_WORDS
    ]
    if exact:
        cells = exact

    statuses = []
    for cell in cells:
        proven_hit = _hit(cell, _PROVEN_WORDS)
        admitted_hit = _hit(cell, _ADMITTED_WORDS)
        if proven_hit == "negated" or admitted_hit == "negated":
            return None, "a negation sits next to the status keyword"
        if proven_hit and admitted_hit:
            return None, "both status classes appear in one cell"
        if proven_hit:
            statuses.append("proven")
        elif admitted_hit:
            statuses.append("admitted")
    if not statuses:
        return None, "no recognized status keyword (see the documented vocabulary)"
    if len(set(statuses)) > 1:
        return None, "conflicting status keywords across cells"
    return statuses[0], None


def parse_coverage_matrix(text: str, repo_root: str) -> Tuple[List[MatrixRow], List[str]]:
    """Parse and VALIDATE a markdown coverage matrix.

    Security validation is this function's job: only rows whose symbol passes
    the identifier allowlist and whose `.lean` path resolves to a confined,
    non-symlink regular file under ``repo_root`` are returned. Everything
    else yields a per-row warning. Warning text quotes at most the row's line
    number and a heavily truncated, backtick-stripped fragment.
    """
    rows: List[MatrixRow] = []
    warnings: List[str] = []

    def warn(line_no: int, reason: str) -> None:
        warnings.append(f"Coverage matrix line {line_no}: {reason} — row skipped.")

    data_lines = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # Rows inside ``` code fences are documentation examples, not claims —
        # executing them would produce phantom findings and wasted subprocesses.
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped.startswith("|"):
            continue
        inner = stripped.strip("|")
        if _TABLE_SEPARATOR_RE.match(inner):
            # Header detection is per-table: the row on the line directly
            # above a separator is that table's header, not a claim. (A
            # file-global "drop the first row" would eat a real data row of
            # any second table.)
            if data_lines and data_lines[-1][0] == line_no - 1:
                data_lines.pop()
            continue
        data_lines.append((line_no, [c.strip() for c in inner.split("|")]))

    if len(data_lines) > MAX_MATRIX_ROWS:
        warnings.append(
            f"Coverage matrix has {len(data_lines)} rows; only the first "
            f"{MAX_MATRIX_ROWS} are checked (front-load critical rows)."
        )
        data_lines = data_lines[:MAX_MATRIX_ROWS]

    for line_no, cells in data_lines:
        symbol_cells = []
        path_cells = []
        for cell in cells:
            m = _BACKTICKED_CELL_RE.match(cell)
            inner = m.group(1) if m else cell
            if inner.endswith(".lean"):
                path_cells.append((cell, inner))
            elif m and is_valid_lean_symbol(inner):
                symbol_cells.append((cell, inner))

        if len(symbol_cells) != 1:
            warn(line_no, f"expected exactly one backticked Lean identifier cell, found {len(symbol_cells)}")
            continue
        if len(path_cells) != 1:
            warn(line_no, f"expected exactly one `.lean` file-path cell, found {len(path_cells)}")
            continue

        symbol = symbol_cells[0][1]
        raw_path = path_cells[0][1]

        # Path validation BEFORE module derivation: relative, no backslash,
        # ./-stripped, confined non-symlink regular file under repo_root.
        if "\\" in raw_path or os.path.isabs(raw_path):
            warn(line_no, "file path must be a relative POSIX path")
            continue
        rel_path = raw_path[2:] if raw_path.startswith("./") else raw_path
        # Reject non-normalized paths outright (`Fri/./Spec.lean`, `Fri//x`,
        # `../x`): they would derive a broken module name (silently degrading
        # a real mismatch to an infra warning) and their literal spelling
        # would never match the git-diff changed-file keys that the caller's
        # causal-blocking rule compares against.
        if any(seg in ("", ".", "..") for seg in rel_path.split("/")):
            warn(line_no, "file path must be normalized (no `.`/`..`/empty segments)")
            continue
        resolved = resolve_confined_path(os.path.join(repo_root, rel_path), repo_root, "file")
        if resolved is None:
            warn(line_no, "file path does not resolve to a regular file inside the repository")
            continue

        status_cells = [
            c for c in cells
            if c not in (symbol_cells[0][0], path_cells[0][0])
        ]
        claimed, problem = _status_from_cells(status_cells)
        if claimed is None:
            warn(line_no, f"ambiguous status: {problem}")
            continue

        # The module string is interpolated into `import {module}` and is as
        # attacker-influenced as the symbol (it derives from a committed
        # filename) — hold it to the same identifier allowlist.
        module = file_path_to_module_name(rel_path)
        if not module or not is_valid_lean_symbol(module):
            warn(line_no, "file path yields no valid Lean module name")
            continue

        rows.append(MatrixRow(
            symbol=symbol,
            file_path=rel_path,
            module=module,
            claimed=claimed,
            line_no=line_no,
        ))

    return rows, warnings


@dataclass
class CoverageFinding:
    """A deterministic coverage-matrix finding.

    ``kind`` is "status-mismatch" (symbol resolves and carries sorryAx while
    the matrix claims proven — a machine-verified false claim) or
    "unresolved-symbol" (the kernel reports an unknown constant — advisory:
    this conflates rename drift with genuine absence). ``blocking`` is decided
    by the CALLER (review.py) under the causal-responsibility rule; it starts
    False here.
    """
    kind: str
    row: MatrixRow
    evidence: str
    blocking: bool = False


def check_coverage_matrix(
    rows: List[MatrixRow],
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
    extractor: Callable = extract_axioms,
) -> Tuple[List[CoverageFinding], List[str], List[str]]:
    """Resolve every validated row against `#print axioms`.

    Returns ``(findings, infra_warnings, stale_notes)``:
    - findings: status-mismatch / unresolved-symbol (see CoverageFinding);
    - infra_warnings: import failures, timeouts, budget truncation — NEVER
      findings (an unbuilt module must not fabricate a false claim);
    - stale_notes: rows claiming `sorry` whose declaration no longer carries
      sorryAx — informational (rows ready to flip to proven); without these a
      fully stale matrix would pass silently as "clean".
    """
    findings: List[CoverageFinding] = []
    infra_warnings: List[str] = []
    stale_notes: List[str] = []

    by_module: Dict[str, List[MatrixRow]] = {}
    for row in rows:
        by_module.setdefault(row.module, []).append(row)

    for module, module_rows in sorted(by_module.items()):
        if deadline is not None and clock() >= deadline:
            remaining = sum(len(r) for m, r in by_module.items() if m >= module)
            infra_warnings.append(
                f"Coverage-matrix time budget exhausted: {remaining} row(s) not checked."
            )
            break
        symbols = [row.symbol for row in module_rows]
        axiom_map, errors = extractor(module, symbols, deadline=deadline)

        for row in module_rows:
            if row.symbol in axiom_map:
                has_sorry = "sorryAx" in axiom_map[row.symbol]
                if row.claimed == "proven" and has_sorry:
                    findings.append(CoverageFinding(
                        kind="status-mismatch",
                        row=row,
                        evidence=(
                            f"`#print axioms {row.symbol}` reports `sorryAx`, but the "
                            f"matrix (line {row.line_no}) claims it is proven."
                        ),
                    ))
                elif row.claimed == "admitted" and not has_sorry:
                    stale_notes.append(
                        f"Matrix line {row.line_no}: `{row.symbol}` is claimed admitted "
                        "but no longer depends on `sorryAx` — row may be ready to flip to proven."
                    )
                continue

            # Not in the axiom map: classify via the extractor's error record.
            prefix = f"`#print axioms {row.symbol}`"
            row_errors = [e for e in errors if e.startswith(prefix)]
            # Case-insensitive: Lean emits "Unknown constant `X`" (current) or
            # "unknown constant 'X'" (older toolchains).
            if any("unknown constant" in e.lower() for e in row_errors):
                findings.append(CoverageFinding(
                    kind="unresolved-symbol",
                    row=row,
                    evidence=(
                        f"`#print axioms {row.symbol}` reports an unknown constant "
                        f"(matrix line {row.line_no}) — renamed, removed, or never existed."
                    ),
                ))
            elif row_errors:
                first = row_errors[0][:300]
                infra_warnings.append(
                    f"Coverage check for `{row.symbol}` (matrix line {row.line_no}) "
                    f"did not complete: {first}"
                )
            else:
                infra_warnings.append(
                    f"Coverage check for `{row.symbol}` (matrix line {row.line_no}) "
                    "returned no result (module import or budget issue)."
                )

    return findings, infra_warnings, stale_notes
