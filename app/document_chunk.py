"""Text cleaning and chunking for imported documents (phase 3, iteration 3.2).

Chunks are produced from parser Page objects while preserving provenance
(document id is attached at storage time; page_hint / heading live here).
No network, no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.document_parse import Page

TARGET_CHARS = 900
MAX_CHARS = 1200
OVERLAP_CHARS = 100

# Zero-width / BOM characters and the PDF "mojibake-friendly" control chars.
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")
_MULTI_SPACE = re.compile(r"[ \t\u3000]+")
_MULTI_BLANK = re.compile(r"\n{3,}")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def clean_text(text: str) -> str:
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Normalize spaces but keep newlines as paragraph boundaries.
    text = _MULTI_SPACE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


@dataclass(frozen=True)
class Chunk:
    content: str
    chunk_index: int
    page_hint: int | None = None
    heading: str | None = None
    char_count: int = 0


def _split_long_paragraph(paragraph: str) -> list[str]:
    """Split an over-long paragraph on sentence boundaries (CJK + latin)."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    sentences = re.split(r"(?<=[。！？!?\.])\s+|(?<=[。！？!?])(?=[^\d])", paragraph)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 <= MAX_CHARS:
            current = (current + " " + sentence).strip()
        else:
            if current:
                pieces.append(current)
            if len(sentence) <= MAX_CHARS:
                current = sentence
            else:
                # Extremely long sentence with no boundary: hard cut.
                for i in range(0, len(sentence), MAX_CHARS):
                    pieces.append(sentence[i : i + MAX_CHARS])
                current = ""
    if current:
        pieces.append(current)
    return pieces


def _pack_units(units: list[tuple[str, int | None, str | None]]) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    buf_page: int | None = None
    buf_heading: str | None = None

    def flush() -> None:
        nonlocal buf, buf_len, buf_page, buf_heading
        if buf:
            content = "\n\n".join(buf).strip()
            if content:
                chunks.append(Chunk(
                    content=content, chunk_index=len(chunks),
                    page_hint=buf_page, heading=buf_heading, char_count=len(content),
                ))
        buf, buf_len = [], 0

    for text, page_hint, heading in units:
        # A page/heading boundary or size limit flushes the current buffer.
        boundary = (page_hint is not None and buf_page is not None and page_hint != buf_page)
        if boundary or buf_len + len(text) + 2 > TARGET_CHARS:
            flush()
        if not buf:
            buf_page, buf_heading = page_hint, heading
        buf.append(text)
        buf_len += len(text) + 2
        if buf_len >= TARGET_CHARS:
            flush()
    flush()
    return _add_overlap(chunks)


def _add_overlap(chunks: list[Chunk]) -> list[Chunk]:
    """Add a small trailing->leading overlap between adjacent same-source chunks."""
    if len(chunks) <= 1:
        return chunks
    result: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        content = chunk.content
        if index > 0:
            prev = chunks[index - 1]
            if prev.page_hint == chunk.page_hint:
                tail = prev.content[-OVERLAP_CHARS:]
                cut = tail.find("。")
                tail = tail[cut + 1 :] if 0 <= cut < OVERLAP_CHARS - 20 else tail
                if tail.strip() and tail.strip() not in content:
                    content = tail.strip() + "\n" + content
        result.append(Chunk(
            content=content.strip(), chunk_index=index,
            page_hint=chunk.page_hint, heading=chunk.heading,
            char_count=len(content),
        ))
    return result


def chunk_pages(pages: list[Page], ext: str = ".txt") -> list[Chunk]:
    """Turn parsed pages into provenance-tagged chunks."""
    units: list[tuple[str, int | None, str | None]] = []
    for page in pages:
        text = clean_text(page.text)
        if not text:
            continue
        heading = page.heading
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if ext in {".md", ".markdown"}:
                match = _MD_HEADING.match(line)
                if match:
                    heading = match.group(1).strip()[:200]
                    continue  # headings are metadata, not body content
            paragraph = line if not _BULLET_PREFIX.match(line) else _BULLET_PREFIX.sub("", line)
            for piece in _split_long_paragraph(paragraph):
                units.append((piece, page.page_hint, heading or None))

    chunks = _pack_units(units)
    # Re-index after overlap adjustments (indices are already contiguous).
    return [Chunk(content=c.content, chunk_index=i, page_hint=c.page_hint,
                  heading=c.heading, char_count=len(c.content)) for i, c in enumerate(chunks)]
