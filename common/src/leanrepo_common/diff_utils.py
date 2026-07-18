"""Git diff header parsing shared by the review and summary pipelines.

With git's default ``core.quotePath=true``, any path containing non-ASCII
bytes (or a double quote, backslash, or control character) is emitted in the
``diff --git`` header as a C-style quoted string with octal escapes::

    diff --git "a/M\303\266bius.lean" "b/M\303\266bius.lean"

A naive ``diff --git a/(.+) b/(.+)`` regex does not match that form, so such
files silently vanish from per-file splitting — they are neither analyzed nor
listed. Lean permits unicode in module names, so these paths occur on real
repositories. This module is the single place both pipelines parse the header.
"""
import re
from typing import Optional, Tuple

_QUOTED = r'"(?:\\.|[^"\\])*"'
_BOTH_QUOTED_RE = re.compile(r'^({q}) ({q})$'.format(q=_QUOTED))
_A_QUOTED_RE = re.compile(r'^({q}) (b/.*)$'.format(q=_QUOTED))
_B_QUOTED_RE = re.compile(r'^(a/.*?) ({q})$'.format(q=_QUOTED))
_UNQUOTED_RE = re.compile(r'^a/(.+) b/(.+)$')

_ESCAPES = {
    'n': b'\n', 't': b'\t', 'r': b'\r', 'a': b'\a', 'b': b'\b',
    'f': b'\f', 'v': b'\v', '\\': b'\\', '"': b'"',
}


def _unquote_c_style(quoted: str) -> str:
    """Decode one git C-style quoted string (surrounding quotes included).

    Octal escapes are raw *bytes* of the UTF-8 encoding, so they are collected
    into a byte buffer and decoded at the end (errors='replace': a path that
    is not valid UTF-8 still yields a usable, stable key rather than a crash).
    """
    body = quoted[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '\\' and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in '01234567':
                j = i + 1
                while j < min(i + 4, len(body)) and body[j] in '01234567':
                    j += 1
                out.append(int(body[i + 1:j], 8) & 0xFF)
                i = j
                continue
            out += _ESCAPES.get(nxt, nxt.encode('utf-8'))
            i += 2
            continue
        out += ch.encode('utf-8')
        i += 1
    return out.decode('utf-8', errors='replace')


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix):] if path.startswith(prefix) else path


def unquote_git_path(path: str) -> str:
    """Unquote a possibly-quoted path from a git header line.

    For the single-path headers (``rename to``, ``rename from``, ``+++ b/…``)
    git applies the same core.quotePath quoting as the ``diff --git`` line.
    A path that is not quoted is returned unchanged.
    """
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return _unquote_c_style(path)
    return path


def parse_git_diff_header(line: str) -> Optional[Tuple[str, str]]:
    """Parse a ``diff --git`` header line into ``(a_path, b_path)``.

    Handles all four quoting combinations git emits (each side is quoted
    independently). For the fully unquoted form, a path containing the
    literal substring `` b/`` is ambiguous; the non-rename case (a-path ==
    b-path) is disambiguated by preferring the split where the two sides
    mirror each other, falling back to the greedy match for renames.

    Returns None when the line is not a ``diff --git`` header.
    """
    prefix = 'diff --git '
    if not line.startswith(prefix):
        return None
    rest = line[len(prefix):].rstrip('\r\n')

    m = _BOTH_QUOTED_RE.match(rest)
    if m:
        a, b = _unquote_c_style(m.group(1)), _unquote_c_style(m.group(2))
        return _strip_prefix(a, 'a/'), _strip_prefix(b, 'b/')
    m = _A_QUOTED_RE.match(rest)
    if m:
        return (_strip_prefix(_unquote_c_style(m.group(1)), 'a/'),
                _strip_prefix(m.group(2), 'b/'))
    m = _B_QUOTED_RE.match(rest)
    if m:
        return (_strip_prefix(m.group(1), 'a/'),
                _strip_prefix(_unquote_c_style(m.group(2)), 'b/'))

    # Fully unquoted. Prefer the mirror split: rest == "a/P b/P" for the
    # common non-rename case, even when P itself contains " b/".
    if rest.startswith('a/'):
        body = rest[2:]
        if len(body) >= 3 and (len(body) - 3) % 2 == 0:
            half = (len(body) - 3) // 2
            if body[half:half + 3] == ' b/' and body[:half] == body[half + 3:]:
                return body[:half], body[half + 3:]
    m = _UNQUOTED_RE.match(rest)
    if m:
        return m.group(1), m.group(2)
    return None
