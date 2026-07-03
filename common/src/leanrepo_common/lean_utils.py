"""Shared Lean 4 source utilities.

Provides the comment-aware line scanners used to detect keywords like
`sorry`/`admit` without false positives, mapping between dotted Lean module
names and files on disk (in both directions), and a small file cache.
Used by the summary and review actions and the sorry-tracker CLI.
"""

import os
import re
from typing import Dict, List, Optional, Tuple


def is_in_comment(line: str, nesting_depth: int) -> Tuple[bool, int]:
    """Determines if a line's code content is entirely within comments.

    Handles Lean 4's nested ``/- ... -/`` block comments and ``--`` line
    comments.

    Returns (line_has_no_code_outside_comments, new_nesting_depth).
    """
    stripped = line.strip()

    if nesting_depth == 0 and stripped.startswith('--'):
        return True, 0

    has_code = False
    i = 0
    while i < len(stripped):
        if i + 1 < len(stripped):
            pair = stripped[i:i + 2]
            if pair == '/-':
                nesting_depth += 1
                i += 2
                continue
            if pair == '-/' and nesting_depth > 0:
                nesting_depth -= 1
                i += 2
                continue
            if pair == '--' and nesting_depth == 0:
                break  # rest of line is a single-line comment

        if nesting_depth == 0 and not stripped[i].isspace():
            has_code = True
        i += 1

    return not has_code, nesting_depth


def strip_comments(line: str, nesting_depth: int) -> Tuple[str, int]:
    """Return the code portion of a line, with Lean 4 comments removed.

    Strips nested ``/- ... -/`` block comments (honoring the incoming depth)
    and a trailing ``--`` line comment, while leaving string-literal contents
    intact (so callers can still keyword-scan strings if they wish). String
    awareness also prevents a ``--`` or ``/-`` *inside* a double-quoted string
    from being mistaken for a comment delimiter.

    Use this before scanning a line for keywords (e.g. escape hatches): unlike
    :func:`is_in_comment`, which only reports whether a line is *entirely*
    comment, this removes the comment text so a keyword mentioned in a trailing
    comment is not matched against real code. Returns
    (code_only_text, new_nesting_depth).
    """
    out = []
    i = 0
    n = len(line)
    in_string = False
    while i < n:
        ch = line[i]
        if in_string:
            out.append(ch)
            if ch == '"' and (i == 0 or line[i - 1] != '\\'):
                in_string = False
            i += 1
            continue
        if nesting_depth == 0:
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue
            pair = line[i:i + 2]
            if pair == '/-':
                nesting_depth += 1
                i += 2
                continue
            if pair == '--':
                break  # rest of the line is a single-line comment
            out.append(ch)
            i += 1
            continue
        # Inside a block comment (nesting_depth > 0): consume until it closes.
        pair = line[i:i + 2]
        if pair == '/-':
            nesting_depth += 1
            i += 2
            continue
        if pair == '-/':
            nesting_depth -= 1
            i += 2
            continue
        i += 1
    return ''.join(out), nesting_depth


def scrub_line(line: str, nesting_depth: int, in_string: bool) -> Tuple[str, int, bool]:
    """Return the scannable code of a line, with Lean comments **and string
    literal contents** removed, threading both block-comment nesting and
    open-string state across lines.

    ``strip_comments`` deliberately preserves string contents (so a ``--`` or
    ``/-`` inside a string is not mistaken for a comment). For *keyword*
    scanning that is unsound: a ``"... sorry ..."`` string literal, or a
    declaration keyword inside one, would be matched as real code. This scanner
    drops the string body entirely, so only genuine code reaches the keyword and
    declaration regexes. It also threads ``in_string`` so a string literal that
    spans several lines stays suppressed on every line it covers.

    Returns ``(code_only_text, new_nesting_depth, new_in_string)``.
    """
    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_string:
            # Inside a string literal: drop the content. A backslash escapes the
            # next char (so ``\"`` does not close the string); a bare ``"`` does.
            if ch == '\\' and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if nesting_depth == 0:
            if ch == '"':
                in_string = True
                i += 1
                continue
            pair = line[i:i + 2]
            if pair == '/-':
                nesting_depth += 1
                i += 2
                continue
            if pair == '--':
                break  # rest of the line is a single-line comment
            out.append(ch)
            i += 1
            continue
        # Inside a block comment (nesting_depth > 0): consume until it closes.
        pair = line[i:i + 2]
        if pair == '/-':
            nesting_depth += 1
            i += 2
            continue
        if pair == '-/':
            nesting_depth -= 1
            i += 2
            continue
        i += 1
    return ''.join(out), nesting_depth, in_string


def detect_src_dir(root: str = '.') -> Optional[str]:
    """Detect a Lean package's source directory by parsing its lakefile."""
    for lakefile, pattern in [
        ('lakefile.toml', r'srcDir\s*=\s*"([^"]+)"'),
        ('lakefile.lean', r'srcDir\s*:=\s*"([^"]+)"'),
    ]:
        path = os.path.join(root, lakefile)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    m = re.search(pattern, f.read())
                if m:
                    return m.group(1)
            except Exception:
                pass
    return None


def file_path_to_module_name(file_path: str, src_dir: Optional[str] = None) -> str:
    """Converts a file path to a Lean module name.

    Resolution order:
    1. Explicitly provided src_dir parameter
    2. LEAN_SRC_DIR environment variable
    3. Lakefile parsing (toml, then lean) in the current directory
    4. Heuristic prefixes: src/, lib/, Mathlib/
    """
    if src_dir is None:
        src_dir = os.environ.get('LEAN_SRC_DIR')
    if src_dir is None:
        src_dir = detect_src_dir()

    if src_dir and file_path.startswith(f"{src_dir}/"):
        file_path = file_path[len(src_dir) + 1:]
    else:
        for prefix in ['src/', 'lib/', 'Mathlib/']:
            if file_path.startswith(prefix):
                file_path = file_path[len(prefix):]
                break

    return file_path.replace('/', '.').replace('.lean', '')


def import_search_dirs(repo_root: str) -> List[str]:
    """Directories under which a dotted Lean module may resolve to a file.

    Probing these directly avoids guessing each dependency's import root from
    its on-disk package directory name (the old ``str.capitalize()`` heuristic
    silently failed for packages like ``ProofWidgets``). For a module
    ``A.B.C`` the file ``A/B/C.lean`` is searched under, in order: the repo
    root, the repo's lakefile ``srcDir``, a conventional ``src/``, and every
    ``.lake/packages/<pkg>`` directory plus that package's own ``srcDir``.
    """
    dirs = [repo_root]
    src = detect_src_dir(repo_root)
    if src:
        dirs.append(os.path.join(repo_root, src))
    dirs.append(os.path.join(repo_root, 'src'))

    packages = os.path.join(repo_root, '.lake', 'packages')
    if os.path.isdir(packages):
        for pkg in sorted(os.listdir(packages)):
            pkg_dir = os.path.join(packages, pkg)
            if not os.path.isdir(pkg_dir):
                continue
            dirs.append(pkg_dir)
            psrc = detect_src_dir(pkg_dir)
            if psrc:
                dirs.append(os.path.join(pkg_dir, psrc))

    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def resolve_import(import_path: str, search_dirs: List[str]) -> Optional[str]:
    """Resolve a dotted Lean module name to a file under one of search_dirs.

    Returns the first matching path, or None if the module can't be located.
    """
    parts = [p for p in import_path.split('.') if p]
    if not parts:
        return None
    rel = os.path.join(*parts) + '.lean'
    for base in search_dirs:
        candidate = os.path.join(base, rel)
        if os.path.isfile(candidate):
            return candidate
    return None


class FileCache:
    """In-memory cache for file contents.

    Avoids redundant disk reads when the same file is accessed
    by multiple stages of the review pipeline. Thread-safe for
    concurrent reads (Python dict get/set are atomic under the GIL,
    and duplicate reads of the same uncached file are harmless).
    """

    def __init__(self):
        self._cache: Dict[str, Optional[str]] = {}

    def read(self, file_path: str) -> Optional[str]:
        """Read file contents, returning cached content if available.
        Returns None if the file does not exist or cannot be read."""
        if file_path not in self._cache:
            if not os.path.exists(file_path):
                self._cache[file_path] = None
            else:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        self._cache[file_path] = f.read()
                except Exception:
                    self._cache[file_path] = None
        return self._cache[file_path]

    def readlines(self, file_path: str) -> Optional[List[str]]:
        """Read file as a list of lines (with line endings), using the cache."""
        content = self.read(file_path)
        if content is None:
            return None
        return content.splitlines(keepends=True)
