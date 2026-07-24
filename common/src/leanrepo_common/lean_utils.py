"""Shared Lean 4 source utilities.

Provides the comment-aware line scanners used to detect keywords like
`sorry`/`admit` without false positives, mapping between dotted Lean module
names and files on disk (in both directions), and a small file cache.
Used by the summary and review actions and the sorry-tracker CLI.
"""

import os
import re
import stat
from typing import Dict, Iterable, List, Optional, Tuple


def resolve_confined_path(
    file_path: str,
    root: Optional[str] = None,
    expect: Optional[str] = None,
) -> Optional[str]:
    """Resolve an existing path only when it stays inside ``root``.

    Repository paths can come from a checked-out PR.  A lexical ``..`` check is
    not enough there: a committed symlink such as ``Leak.lean`` can point at
    ``/proc/self/environ`` and make a later ordinary ``open`` read runner
    secrets.  This helper rejects a symlink in the final path component, checks
    the fully resolved path against the repository root, and optionally
    requires a regular file (``expect="file"``) or directory
    (``expect="dir"``).

    Absolute paths are accepted only when they resolve inside ``root``.  The
    returned value is absolute, so callers do not accidentally validate one
    path and open another after changing working directory.
    """
    if not file_path or expect not in (None, "file", "dir"):
        return None
    root_real = os.path.realpath(root or os.getcwd())
    candidate = os.path.abspath(file_path)
    try:
        # Reject a PR-controlled final-component symlink even when it happens to
        # point elsewhere inside the checkout.  Git symlinks are data, not files
        # whose targets the review pipeline should dereference.
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode):
            return None
        resolved = os.path.realpath(candidate)
        if os.path.commonpath((root_real, resolved)) != root_real:
            return None
    except (OSError, ValueError):
        return None

    if expect == "file" and not stat.S_ISREG(metadata.st_mode):
        return None
    if expect == "dir" and not stat.S_ISDIR(metadata.st_mode):
        return None
    return resolved


def is_in_comment(line: str, nesting_depth: int) -> Tuple[bool, int]:
    """Determines if a line's code content is entirely within comments.

    Handles Lean 4's nested ``/- ... -/`` block comments and ``--`` line
    comments.

    Returns (line_has_no_code_outside_comments, new_nesting_depth).
    """
    # Reuse the string-aware comment stripper so `/-` and `--` inside a Lean
    # string cannot poison the nesting state used by summary's fallback scan.
    code, nesting_depth = strip_comments(line, nesting_depth)
    return not code.strip(), nesting_depth


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

    This is the single-line (no cross-line string state) form of
    :func:`strip_comments_preserve_strings`; it delegates to it with the string
    state reset each line, so the scanning logic lives in exactly one place.
    """
    code, nesting_depth, _ = strip_comments_preserve_strings(line, nesting_depth, False)
    return code, nesting_depth


def strip_comments_preserve_strings(
    line: str, nesting_depth: int, in_string: bool,
) -> Tuple[str, int, bool]:
    """Remove Lean comments while preserving string contents and open-string state.

    This is the stateful counterpart to :func:`strip_comments`. It is useful for
    bounded signature/context extraction: callers need the readable contents of
    strings, but a string can span lines and must not make ``--``, ``/-``, ``:=``
    or ``where`` on a continuation line look like Lean syntax.
    """
    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_string:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if ch == '"':
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
                break
            out.append(ch)
            i += 1
            continue
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


# ---------------------------------------------------------------------------
# Canonical escape-hatch / kernel-bypass keyword matcher (C5)
# ---------------------------------------------------------------------------
# One word-boundary convention for the whole toolkit. A keyword matches only as
# a standalone identifier token: not as a substring of a larger identifier, and
# not as the stem of a primed identifier. This is deliberately STRICTER than
# ``\b``, which treats a trailing prime as a word boundary and so matches the
# ``sorry`` inside the identifier ``sorry'`` — a false positive that, in
# review's mechanical pre-check, could wrongly force a "Changes Requested"
# verdict.
#
# Callers MUST pass comment/string-scrubbed code (see :func:`scrub_line`); the
# matcher does no comment handling itself.
#
# ``KERNEL_BYPASS_KEYWORDS`` is the full classification vocabulary shared by the
# soundness gates. Individual tools scan a subset appropriate to their policy
# (e.g. review's blocking set excludes ``decide``/``extern``); the point of C5
# is that they all share ONE boundary rule and ONE compiled-pattern cache — not
# that they all act on the same keywords.
#
# The vocabulary itself is standard Lean 4 kernel-bypass / trust-reducing
# constructs, per Lean's own ValidatingProofs manual
# (https://lean-lang.org/doc/reference/latest/ValidatingProofs/); the
# standalone-token boundary follows this repo's existing sorry-tracker scanner.
# The same forbidden-tactic concept appears in evm-asm's
# scripts/check-forbidden-tactics.sh (MIT, © ZkSecurity) — noted as a courtesy
# see-also; only the (uncopyrightable) keyword facts are shared, no code was
# copied, so this is not an Apache-2.0 §4(d) NOTICE obligation.
#
# NOTE: ``bv_decide`` is in the vocabulary (it is kernel-bypassing via
# ``Lean.ofReduceBool``, like ``native_decide``) but NO caller scans for it yet
# — review's ``ESCAPE_HATCHES`` and summary's quality signals do not include it.
# Closing that end-to-end detection gap is deferred to the M3 soundness gate
# (ROADMAP G3), which is where the cheating-tactic policy lives.

KERNEL_BYPASS_KEYWORDS: Tuple[str, ...] = (
    "sorry", "admit", "sorryAx", "native_decide", "bv_decide", "decide",
    "opaque", "implemented_by", "extern", "axiom",
)

_KEYWORD_PATTERN_CACHE: Dict[str, "re.Pattern[str]"] = {}
_KEYWORDS_PATTERN_CACHE: Dict[Tuple[str, ...], "re.Pattern[str]"] = {}


def keyword_pattern(keyword: str) -> "re.Pattern[str]":
    """Compiled canonical-boundary matcher for a single Lean keyword (cached).

    A match requires the keyword to be a standalone token — not preceded or
    followed by an identifier character or a prime (``'``). Pass already-scrubbed
    code (see :func:`scrub_line`)."""
    pattern = _KEYWORD_PATTERN_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(rf"(?<![\w']){re.escape(keyword)}(?![\w'])")
        _KEYWORD_PATTERN_CACHE[keyword] = pattern
    return pattern


def keywords_pattern(keywords: Iterable[str]) -> "re.Pattern[str]":
    """Compiled canonical-boundary matcher for several keywords as one regex.

    The single alternation matches any of ``keywords`` as a standalone token,
    with the same boundary rule as :func:`keyword_pattern`. ``match.group()`` is
    the matched keyword, so callers can classify via ``search``/``finditer``.
    Cached by keyword tuple. Pass already-scrubbed code."""
    key = tuple(keywords)
    pattern = _KEYWORDS_PATTERN_CACHE.get(key)
    if pattern is None:
        alternation = "|".join(re.escape(k) for k in key)
        pattern = re.compile(rf"(?<![\w'])(?:{alternation})(?![\w'])")
        _KEYWORDS_PATTERN_CACHE[key] = pattern
    return pattern


def find_keywords(code: str, keywords: Iterable[str]) -> List[str]:
    """Keywords present as standalone tokens in already-scrubbed ``code``.

    Preserves the order of ``keywords`` and reports each at most once. ``code``
    must already have comments and string contents removed (see
    :func:`scrub_line`)."""
    return [kw for kw in keywords if keyword_pattern(kw).search(code)]


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

    return file_path.removesuffix('.lean').replace('/', '.')


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
                    with open(
                        file_path,
                        'r',
                        encoding='utf-8-sig',
                        errors='replace',
                    ) as f:
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
