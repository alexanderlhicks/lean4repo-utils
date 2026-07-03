"""Fetching external reference URLs (papers, web pages) as LLM content parts.

PDFs are passed through as raw bytes for OpenRouter's file-parser plugin to
extract; everything else is fetched as text with HTML lightly stripped. All
downloads are byte-capped so a single huge reference can't exhaust memory.
"""

import html
import re
import sys
from urllib.parse import urlparse

import requests

from leanrepo_common.llm_provider import ContentPart

MAX_REFERENCE_BYTES = 10_000_000  # Max bytes to download per reference URL (skip larger).
HTTP_TIMEOUT = 30

# Exclude '<' from the class so a failed match can't scan to end-of-string at
# every '<' (which makes the unanchored `<[^>]+>` quadratic on `<`-heavy input).
_HTML_TAG_RE = re.compile(r"<[^<>]+>")
_HTML_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_CHARSET_RE = re.compile(rb"""charset=["']?([\w-]+)""", re.IGNORECASE)


def _download_capped(url: str) -> "tuple[bytes, str] | None":
    """GET a URL, streaming with a byte cap.

    Returns (body, content_type), or None if it exceeds MAX_REFERENCE_BYTES.
    """
    with requests.get(url, timeout=HTTP_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_REFERENCE_BYTES:
                print(f"⚠️  Skipping oversized reference (> {MAX_REFERENCE_BYTES} bytes): {url}", file=sys.stderr)
                return None
            chunks.append(chunk)
        return b"".join(chunks), content_type


def _decode_text(body: bytes, content_type: str) -> str:
    """Decode bytes to text using the declared charset, then an HTML <meta>
    charset, then UTF-8 — never the Latin-1 fallback that mangles Unicode."""
    m = re.search(r"charset=([\w-]+)", content_type)
    encoding = m.group(1) if m else None
    if not encoding:
        meta = _CHARSET_RE.search(body[:2048])
        encoding = meta.group(1).decode("ascii", "ignore") if meta else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch_reference_parts(urls: "list[str]") -> "list[ContentPart]":
    """Fetch reference URLs and return them as provider content parts."""
    parts: "list[ContentPart]" = []
    if not urls:
        return parts

    print(f"📚 Fetching content from {len(urls)} reference URL(s)...")
    for url in urls:
        try:
            downloaded = _download_capped(url)
            if downloaded is None:
                continue
            body, content_type = downloaded
            is_pdf = "application/pdf" in content_type or urlparse(url).path.lower().endswith(".pdf")
            if is_pdf:
                parts.append(ContentPart(type="pdf", data=body, mime_type="application/pdf"))
            else:
                text = _decode_text(body, content_type)
                if "html" in content_type or "<html" in text[:512].lower():
                    text = html.unescape(_HTML_TAG_RE.sub("", _HTML_BLOCK_RE.sub("", text)))
                parts.append(ContentPart(type="text", data=f"--- Reference: {url} ---\n{text.strip()}\n"))
        except Exception as e:
            print(f"❌ Could not fetch reference URL {url}: {e}", file=sys.stderr)
    return parts
