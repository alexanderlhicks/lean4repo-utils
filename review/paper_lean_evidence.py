"""Deterministic paper/Lean evidence extraction for PR review.

This is an indexing step, not a semantic judge. It produces source-preserving
paper anchors and Lean declaration records, plus bounded navigation hints kept
in the machine-readable artifact. Hints are never presented as semantic links
or confirmation of faithfulness. In particular, PDF text is explicitly marked
as lossy and requires visual confirmation against the original PDF.

Inputs are local Lean files and reference files (Lean, TeX, Markdown, plain
text, or PDF). The action writes the complete JSON artifact to a temporary file and
passes a bounded rendering to the review agent through ``GITHUB_OUTPUT``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from leanrepo_common.lean_utils import resolve_confined_path


DECL_RE = re.compile(
    r"^\s*(?:@\[[^\n]*\]\s*)*"
    r"(?:private\s+|protected\s+|noncomputable\s+|partial\s+|mutual\s+|unsafe\s+)*"
    r"(?P<kind>theorem|lemma|def|abbrev|structure|inductive|instance|class|opaque|axiom)\b"
    r"(?:\s+(?P<name>[A-Za-z_][\w.']*))?"
)
NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.']*)")
SECTION_RE = re.compile(r"^\s*section(?:\s+([A-Za-z_][\w.']*))?")
END_RE = re.compile(r"^\s*end(?:\s+([A-Za-z_][\w.']*))?\s*$")
PAPER_ENV_RE = re.compile(
    r"\\begin\{(?P<env>theorem|lemma|proposition|corollary|definition|claim|conjecture)\*?\}"
    r"(?:\[(?P<title>[^\]]*)\])?"
)
TEX_SECTION_RE = re.compile(
    r"\\(?P<kind>part|chapter|section|subsection|subsubsection)\*?\{(?P<title>[^}]*)\}"
)
MD_HEADING_RE = re.compile(r"^\s{0,3}(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
STATEMENT_LINE_RE = re.compile(
    r"^\s*(?P<kind>Theorem|Lemma|Proposition|Corollary|Definition|Claim|Conjecture)\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)
STATEMENT_LABEL_RE = re.compile(
    r"^(?P<label>[A-Za-z0-9_][A-Za-z0-9_.-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?)"
    r"\s*[:.)-]\s*"
)
_GENERIC_TOKENS = {
    "theorem", "lemma", "proposition", "corollary", "definition",
    "claim", "conjecture", "section", "subsection", "result",
    "main", "construction", "proof", "bound", "statement",
}
MAX_NAVIGATION_HINTS = 5000
MAX_NAVIGATION_HINTS_PER_PAPER = 20
MAX_FORMATTED_DECLARATIONS = 200
MAX_FORMATTED_PAPER_STATEMENTS = 80


def _strip_lean_comments(source: str) -> str:
    """Remove Lean comments and string contents while preserving line count."""
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(source):
        if depth == 0 and source.startswith("--", i):
            end = source.find("\n", i)
            end = len(source) if end < 0 else end
            out.extend(" " * (end - i))
            i = end
        elif source.startswith("/-", i):
            depth += 1
            out.extend("  ")
            i += 2
        elif depth and source.startswith("-/", i):
            depth -= 1
            out.extend("  ")
            i += 2
        elif depth:
            out.append("\n" if source[i] == "\n" else " ")
            i += 1
        elif source[i] == '"':
            start = i
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            out.extend("\n" if c == "\n" else " " for c in source[start:i])
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def _namespace_name(stack: list[tuple[str, str]]) -> str:
    return ".".join(name for kind, name in stack if kind == "namespace")


def _signature(lines: list[str], start: int, stripped_lines: list[str]) -> str:
    """Capture the declaration header without attempting to parse Lean syntax."""
    collected: list[str] = []
    for index in range(start, min(len(lines), start + 16)):
        if index > start and DECL_RE.match(stripped_lines[index]):
            break
        collected.append(lines[index].strip())
        if ":=" in stripped_lines[index] or stripped_lines[index].strip() == "where":
            break
    return " ".join(part for part in collected if part)[:1600]


def extract_lean_declarations(path: Path) -> list[dict]:
    """Extract declaration records with source spans and signatures."""
    source = path.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    stripped = _strip_lean_comments(source).splitlines()
    stack: list[tuple[str, str]] = []
    declarations: list[dict] = []

    for index, line in enumerate(stripped):
        namespace = NAMESPACE_RE.match(line)
        section = SECTION_RE.match(line)
        ending = END_RE.match(line)
        if namespace:
            stack.append(("namespace", namespace.group(1)))
            continue
        if section:
            stack.append(("section", section.group(1) or ""))
            continue
        if ending:
            label = ending.group(1)
            if label:
                while stack and stack[-1][1] != label:
                    stack.pop()
                if stack:
                    stack.pop()
            elif stack:
                stack.pop()
            continue

        match = DECL_RE.match(line)
        if not match:
            continue
        kind = match.group("kind")
        short_name = match.group("name") or f"_anonymous_{kind}_{index + 1}"
        ns = _namespace_name(stack)
        fqn = f"{ns}.{short_name}" if ns else short_name
        next_decl = len(lines)
        for later in range(index + 1, len(stripped)):
            if DECL_RE.match(stripped[later]):
                next_decl = later
                break
        body = "\n".join(stripped[index:next_decl])
        declarations.append({
            "source_kind": "lean",
            "file": str(path),
            "line_start": index + 1,
            "line_end": next_decl,
            "kind": kind,
            "short_name": short_name,
            "fqn": fqn,
            "namespace": ns,
            "signature": _signature(lines, index, stripped),
            # This is metadata only.  Review prompts explicitly prohibit using
            # docstrings as correctness evidence.
            "docstring_present": index > 0 and "/--" in lines[index - 1],
            "contains_sorry_or_admit": bool(re.search(r"\b(?:sorry|admit)\b", body)),
        })
    return declarations


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _clean_statement(text: str, limit: Optional[int] = None) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned if limit is None else cleaned[:limit]


def _strip_tex_comments(source: str) -> str:
    """Remove TeX comments while preserving newlines and escaped percent signs."""
    lines = []
    for line in source.splitlines(keepends=True):
        cut = None
        for index, char in enumerate(line):
            if char != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                cut = index
                break
        lines.append(line if cut is None else line[:cut] + ("\n" if line.endswith("\n") else ""))
    return "".join(lines)


def _tex_statements(path: Path, source: str) -> list[dict]:
    statements: list[dict] = []
    for match in TEX_SECTION_RE.finditer(source):
        statements.append({
            "source_kind": "paper",
            "medium": "tex",
            "file": str(path),
            "kind": "section",
            "anchor": match.group("title").strip(),
            "label": "",
            "line_start": _line_number(source, match.start()),
            "line_end": _line_number(source, match.end()),
            "statement_text": match.group("title").strip(),
            "extraction_quality": "source_preserving",
            "requires_visual_confirmation": False,
        })
    for match in PAPER_ENV_RE.finditer(source):
        end_marker = re.search(rf"\\end\{{{match.group('env')}\*?\}}", source[match.end():])
        end = match.end() + end_marker.end() if end_marker else match.end()
        body = source[match.end():end_marker.start() + match.end()] if end_marker else ""
        label_match = re.search(r"\\label\{([^}]+)\}", body)
        label = label_match.group(1) if label_match else ""
        anchor = label or match.group("title") or match.group("env")
        statements.append({
            "source_kind": "paper",
            "medium": "tex",
            "file": str(path),
            "kind": match.group("env"),
            "anchor": anchor.strip(),
            "label": label,
            "line_start": _line_number(source, match.start()),
            "line_end": _line_number(source, end),
            "statement_text": _clean_statement(body),
            "extraction_quality": "source_preserving",
            "requires_visual_confirmation": False,
        })
    return statements


def _plain_statements(path: Path, source: str, medium: str) -> list[dict]:
    lines = source.splitlines()
    statements: list[dict] = []
    for index, line in enumerate(lines):
        heading = MD_HEADING_RE.match(line) if medium == "markdown" else None
        statement = STATEMENT_LINE_RE.match(line)
        if heading:
            statements.append({
                "source_kind": "paper",
                "medium": medium,
                "file": str(path),
                "kind": "section",
                "anchor": heading.group("title"),
                "label": "",
                "line_start": index + 1,
                "line_end": index + 1,
                "statement_text": heading.group("title"),
                "extraction_quality": "source_preserving",
                "requires_visual_confirmation": False,
            })
        if statement:
            block = [line.strip()]
            for later in lines[index + 1:index + 20]:
                if not later.strip() or MD_HEADING_RE.match(later):
                    break
                block.append(later.strip())
            rest = statement.group("rest").strip()
            label_match = STATEMENT_LABEL_RE.match(rest)
            label = label_match.group("label") if label_match else ""
            statements.append({
                "source_kind": "paper",
                "medium": medium,
                "file": str(path),
                "kind": statement.group("kind").lower(),
                "anchor": label or statement.group("kind"),
                "label": label,
                "line_start": index + 1,
                "line_end": index + len(block),
                "statement_text": _clean_statement(" ".join(block)),
                "extraction_quality": "source_preserving",
                "requires_visual_confirmation": False,
            })
    return statements


def _pdf_statements(path: Path, warnings: list[str]) -> list[dict]:
    """Extract candidate anchors only; never treats PDF text as ground truth."""
    tool = shutil.which("pdftotext")
    if not tool:
        warnings.append(f"No pdftotext available for {path}; PDF requires visual inspection.")
        return []
    try:
        result = subprocess.run(
            [tool, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=90,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"PDF text candidate extraction failed for {path}: {type(exc).__name__}.")
        return []
    statements: list[dict] = []
    for page_number, page in enumerate(result.stdout.split("\f"), start=1):
        lines = page.splitlines()
        for index, line in enumerate(lines):
            if not re.match(r"^\s*(?:\d+(?:\.\d+)*\s+)?(?:Theorem|Lemma|Proposition|Corollary|Definition|Claim|Construction)\b", line, re.IGNORECASE):
                continue
            block = [line.strip()]
            for later in lines[index + 1:index + 16]:
                if not later.strip():
                    break
                block.append(later.strip())
            statements.append({
                "source_kind": "paper",
                "medium": "pdf",
                "file": str(path),
                "kind": "pdf_candidate",
                "anchor": line.strip(),
                "label": "",
                "page": page_number,
                "line_start": index + 1,
                "line_end": index + len(block),
                "statement_text": _clean_statement(" ".join(block)),
                "extraction_quality": "lossy_candidate",
                "requires_visual_confirmation": True,
            })
    return statements


def extract_paper_statements(path: Path, warnings: Optional[list[str]] = None) -> list[dict]:
    warnings = warnings if warnings is not None else []
    suffix = path.suffix.lower()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if suffix == ".tex":
        source = path.read_text(encoding="utf-8", errors="replace")
        statements = _tex_statements(path, _strip_tex_comments(source))
    elif suffix in {".md", ".markdown", ".txt"}:
        statements = _plain_statements(path, path.read_text(encoding="utf-8", errors="replace"), "markdown" if suffix != ".txt" else "plain_text")
    elif suffix == ".pdf":
        statements = _pdf_statements(path, warnings)
    else:
        return []
    for statement in statements:
        statement["sha256"] = digest
    return statements


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9']{3,}", value)
        if token.lower() not in _GENERIC_TOKENS
    }


def build_navigation_hints(paper_statements: Iterable[dict], declarations: Iterable[dict]) -> list[dict]:
    """Create bounded navigation hints; these are not semantic matches."""
    links: list[dict] = []
    for paper in paper_statements:
        # Prefer explicit labels. If there is no label, use only meaningful
        # anchor words; a bare "theorem" must not match every Lean theorem.
        paper_tokens = _tokens(" ".join((paper.get("label", ""), paper.get("anchor", ""))))
        if not paper_tokens:
            continue
        candidates: list[tuple[int, dict, list[str]]] = []
        for declaration in declarations:
            lean_tokens = _tokens(" ".join((declaration.get("fqn", ""), declaration.get("signature", ""))))
            overlap = sorted(paper_tokens & lean_tokens)
            if not overlap:
                continue
            score = len(overlap) + (2 if paper.get("label") else 0)
            candidates.append((score, declaration, overlap))
        candidates.sort(key=lambda item: (-item[0], item[1].get("fqn", ""), item[1].get("file", ""), item[1].get("line_start", 0)))
        for score, declaration, overlap in candidates[:MAX_NAVIGATION_HINTS_PER_PAPER]:
            links.append({
                "paper_file": paper["file"],
                "paper_anchor": paper["anchor"],
                "paper_line": paper["line_start"],
                "lean_file": declaration["file"],
                "lean_fqn": declaration["fqn"],
                "lean_line": declaration["line_start"],
                "overlap_tokens": overlap,
                "candidate_score": score,
                "status": "candidate",
                "requires_agent_validation": True,
                "requires_visual_confirmation": bool(paper.get("requires_visual_confirmation")),
            })
            if len(links) >= MAX_NAVIGATION_HINTS:
                return links
    return links


def build_evidence(lean_files: Iterable[Path], reference_files: Iterable[Path]) -> dict:
    warnings: list[str] = []
    declarations: list[dict] = []
    for path in lean_files:
        try:
            if path.exists():
                declarations.extend(extract_lean_declarations(path))
        except (OSError, UnicodeError) as exc:
            warnings.append(f"Lean declaration extraction failed for {path}: {type(exc).__name__}.")

    statements: list[dict] = []
    for path in reference_files:
        try:
            if path.exists():
                if path.suffix.lower() == ".lean":
                    declarations.extend(extract_lean_declarations(path))
                else:
                    statements.extend(extract_paper_statements(path, warnings))
        except (OSError, UnicodeError) as exc:
            warnings.append(f"Paper extraction failed for {path}: {type(exc).__name__}.")
    return {
        "schema_version": 1,
        "kind": "paper_lean_index",
        "deterministic_extraction": True,
        "trust_policy": "Records locate source material; navigation hints, docstrings, and lossy PDF text never establish correctness.",
        "lean_declarations": declarations,
        "paper_statements": statements,
        "navigation_hints": build_navigation_hints(statements, declarations),
        "warnings": warnings,
    }


def format_evidence(payload: dict) -> str:
    """Render a bounded, prompt-safe view of the source index.

    The JSON artifact retains the source records; this compact view is inserted
    into LLM prompts and therefore treats every path, signature, statement, and
    warning as untrusted data. A text fence keeps source text from becoming
    prompt structure, while fence-like runs and control line breaks in
    single-line metadata are neutralized.
    """
    def safe_line(value: object) -> str:
        return re.sub(r"`{3,}", "``", str(value)).replace("\r", " ").replace("\n", " ").replace("\x00", "")

    def safe_block(value: object) -> str:
        return re.sub(r"`{3,}", "``", str(value)).replace("\r", " ").replace("\x00", "")

    lines = [
        "**Paper/Lean Source Index (navigation only; not ground truth):**",
        "```text",
        f"- Lean declarations indexed: {len(payload['lean_declarations'])}",
        f"- Paper anchors indexed: {len(payload['paper_statements'])}",
        "- Source-preserving records and lossy candidates must be checked against the original source.",
    ]
    lines.append("**Lean declarations (source-preserving):**")
    for declaration in payload["lean_declarations"][:MAX_FORMATTED_DECLARATIONS]:
        signature = safe_block(_clean_statement(declaration.get("signature", ""), limit=700))
        detail = f": {signature}" if signature else ""
        lines.append(f"- {safe_line(declaration['file'])}:{declaration['line_start']} {safe_line(declaration['fqn'])} ({safe_line(declaration['kind'])}){detail}")
    if len(payload["lean_declarations"]) > MAX_FORMATTED_DECLARATIONS:
        lines.append(f"- … {len(payload['lean_declarations']) - MAX_FORMATTED_DECLARATIONS} more Lean declarations in the JSON artifact.")
    lines.append("**Paper anchors (source-preserving or explicitly lossy candidates):**")
    for statement in payload["paper_statements"][:MAX_FORMATTED_PAPER_STATEMENTS]:
        confirmation = "; visual confirmation required" if statement.get("requires_visual_confirmation") else ""
        text = safe_line(_clean_statement(statement.get("statement_text", ""), limit=500))
        detail = f": {text}" if text else ""
        lines.append(f"- {safe_line(statement['file'])}:{statement['line_start']} {safe_line(statement['anchor'])} ({safe_line(statement['medium'])}{confirmation}){detail}")
    for warning in payload["warnings"]:
        lines.append(f"- Warning: {safe_line(warning)}")
    lines.append("```")
    return "\n".join(lines)


def _paths(value: str) -> list[Path]:
    paths: list[Path] = []
    for item in value.split(","):
        if not item.strip():
            continue
        path = Path(item.strip())
        resolved_dir = resolve_confined_path(str(path), os.getcwd(), "dir")
        resolved_file = resolve_confined_path(str(path), os.getcwd(), "file")
        if resolved_dir is not None:
            paths.extend(sorted(
                p for p in path.rglob("*")
                if p.suffix.lower() in {".lean", ".pdf", ".tex", ".md", ".markdown", ".txt"}
                and resolve_confined_path(str(p), os.getcwd(), "file") is not None
            ))
        elif resolved_file is not None:
            paths.append(path)
    return paths


def _instruction_references(text: str) -> tuple[list[str], list[str]]:
    """Return safe local spec paths and remote URLs mentioned in review text."""
    if not text:
        return [], []
    urls = []
    for raw in re.findall(r'https?://[^\s<>"\')\]]+', text):
        url = raw.rstrip('.,;:!?\'"`')
        if url not in urls:
            urls.append(url)
    local_paths = []
    for token in re.sub(r'https?://[^\s<>"\')\]]+', ' ', text).split():
        token = token.strip('`"\',;:!?()[]{}<>*').rstrip('.')
        path = Path(token)
        if (
            not token
            or path.is_absolute()
            or ".." in path.parts
            or resolve_confined_path(token, os.getcwd(), "file") is None
            or path.suffix.lower() not in {".lean", ".pdf", ".tex", ".md", ".markdown", ".txt"}
        ):
            continue
        if token not in local_paths:
            local_paths.append(token)
    return local_paths, urls


def _remote_reference_records(urls: Iterable[str]) -> list[dict]:
    records = []
    for url in urls:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix != ".pdf":
            continue
        records.append({
            "source_kind": "paper",
            "medium": "pdf",
            "file": url,
            "kind": "remote_pdf",
            "anchor": Path(urlparse(url).path).name or url,
            "label": "",
            "line_start": 0,
            "line_end": 0,
            "statement_text": "",
            "extraction_quality": "url_only",
            "requires_visual_confirmation": True,
            "sha256": "",
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lean-files", default=os.environ.get("CHANGED_FILES", ""))
    parser.add_argument("--reference-files", default=os.environ.get("SPEC_REFS", ""))
    parser.add_argument("--instructions", default=os.environ.get("ADDITIONAL_COMMENTS", ""))
    parser.add_argument("--out", default=os.environ.get("PAPER_LEAN_EVIDENCE_OUT", ""))
    args = parser.parse_args()
    local_from_instructions, remote_urls = _instruction_references(args.instructions)
    payload = build_evidence(
        _paths(args.lean_files),
        _paths(args.reference_files) + _paths(",".join(local_from_instructions)),
    )
    payload["paper_statements"].extend(_remote_reference_records(remote_urls))
    payload["navigation_hints"] = build_navigation_hints(payload["paper_statements"], payload["lean_declarations"])
    formatted = format_evidence(payload)
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        # Random heredoc marker: `formatted` embeds excerpts from PR-changed
        # files and spec docs (untrusted), so a fixed marker would let a line
        # in that content terminate the block early and inject step outputs.
        # Matches the randomized markers used everywhere else in this repo.
        marker = f"EOF_PAPER_LEAN_EVIDENCE_{secrets.token_hex(16)}"
        with open(output, "a", encoding="utf-8") as handle:
            if args.out:
                handle.write(f"paper_lean_evidence_path={args.out}\n")
            handle.write(f"paper_lean_evidence_formatted<<{marker}\n{formatted}\n{marker}\n")
    print(formatted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
