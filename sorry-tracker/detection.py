"""Detection of Lean `sorry`/`admit` proof obligations.

Walks a Lean source tree and yields one :class:`Sorry` per declaration that
contains an incompleteness marker. Detection is comment- and string-aware (via
:func:`lean_utils.scrub_line`), so a marker inside a comment or a string literal
is never reported, and ``sorryAx`` / primed identifiers like ``sorry'`` are not
mistaken for the keyword. Each obligation carries a stable id used for
cross-run duplicate detection.
"""

import os
import re
import sys
from dataclasses import dataclass

from leanrepo_common.lean_utils import scrub_line

# Declaration keywords a `sorry` is attributed to. Kept in sync with _NAME_RE.
_DECL_KEYWORDS = (
    "theorem|lemma|def|instance|example|opaque|abbrev|inductive|structure|class"
)
# A declaration line may be indented (e.g. inside a `mutual` block), prefixed by
# inline attributes (`@[simp]`), and carry any combination of modifier keywords
# (`partial`, `noncomputable`, `private`, …) before the declaration keyword.
# Missing any of these drops the declaration's name, which both produces a vague
# issue title and breaks cross-run dedup (the stable id falls back to a line
# number that moves whenever code above the proof changes).
_DECL_MODIFIERS = (
    "private|protected|scoped|local|noncomputable|partial|unsafe|nonrec|mutual"
)
_DECL_RE = re.compile(
    rf"^\s*(?:@\[[^\]]*\]\s*)*(?:(?:{_DECL_MODIFIERS})\s+)*(?:{_DECL_KEYWORDS})\b"
)
_NAME_RE = re.compile(rf".*?(?:{_DECL_KEYWORDS})\s+([^\s\(\{{\[:]+)")
# Incompleteness keywords. The lookarounds exclude `\w` *and* the prime `'` on
# either side, so `sorryAx` (the underlying axiom) and identifiers like `sorry'`
# are not mistaken for a proof obligation.
_SORRY_RE = re.compile(r"(?<![\w'])(?:sorry|admit)(?![\w'])")

# Directories never scanned: build output and vendored dependencies.
_SKIP_DIRS = {".lake", "build"}


@dataclass
class Sorry:
    """A single `sorry`/`admit` proof obligation found in a Lean file.

    `stable_id` is the cross-run identity (see :func:`_stable_id`); `snippet` is
    the declaration header through the marker line; `full_content` is the whole
    source file, carried for the analysis prompt.
    """
    file_path: str
    line_num: int
    decl_name: str
    snippet: str
    full_content: str
    stable_id: str


def _stable_id(decl_name: str, file_path: str, line_num: int) -> str:
    """Identity used for cross-run duplicate detection.

    Named declarations dedupe by name (stable across edits that move the line);
    an unnamed obligation falls back to its line so multiple unnamed sorries in
    one file don't collapse onto a single `@file` id.
    """
    handle = decl_name or f"L{line_num}"
    return f"{handle}@{file_path}"


def _scan_file(file_path: str, seen_ids: set) -> "list[Sorry]":
    """Scan one Lean file, returning its (deduplicated) obligations."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"⚠️  Could not read {file_path}: {e}", file=sys.stderr)
        return []

    full_content = "".join(lines)
    found: "list[Sorry]" = []
    current_decl_header = ""
    current_decl_linenum = 0
    comment_depth = 0
    in_string = False

    for i, line in enumerate(lines):
        line_num = i + 1
        code, comment_depth, in_string = scrub_line(line, comment_depth, in_string)

        if _DECL_RE.search(code):
            current_decl_header = code.strip()
            current_decl_linenum = line_num

        if not _SORRY_RE.search(code):
            continue

        name_match = _NAME_RE.match(current_decl_header)
        decl_name = name_match.group(1) if name_match else ""

        stable_id = _stable_id(decl_name, file_path, current_decl_linenum or line_num)
        if stable_id in seen_ids:
            continue
        seen_ids.add(stable_id)

        start_line = current_decl_linenum if current_decl_linenum > 0 else line_num
        found.append(Sorry(
            file_path=file_path,
            line_num=line_num,
            decl_name=decl_name,
            snippet="".join(lines[start_line - 1 : line_num]),
            full_content=full_content,
            stable_id=stable_id,
        ))
    return found


def find_all_sorries(search_path: str) -> "list[Sorry]":
    """Walk `search_path` and return every `sorry`/`admit` obligation found.

    A declaration with several markers yields a single obligation (deduped by
    stable id). Paths are normalized to drop a leading ``./`` so the same proof
    gets the same stable id whether scanned via ``.`` or an explicit sub-path.
    """
    sorries: "list[Sorry]" = []
    seen_ids: set = set()

    for root, dirs, files in os.walk(search_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for file in sorted(files):
            if file.endswith(".lean"):
                file_path = os.path.normpath(os.path.join(root, file))
                sorries.extend(_scan_file(file_path, seen_ids))
    return sorries
