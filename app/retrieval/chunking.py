"""Heading-aware chunking.

Runbooks and KB articles are structured documents; splitting on a fixed
character window shreds numbered procedures in half. We split on headings
first, then pack sections up to the token budget, and only fall back to a
sliding window for oversized sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
APPROX_CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    ordinal: int
    content: str
    heading_path: str | None = None
    page_no: int | None = None
    token_count: int = 0
    metadata: dict = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // APPROX_CHARS_PER_TOKEN)


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    matches = list(HEADING_RE.finditer(markdown))
    if not matches:
        return [("", markdown)]

    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    if matches[0].start() > 0:
        preamble = markdown[: matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))

    for i, m in enumerate(matches):
        level, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " › ".join(t for _, t in stack)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[m.end():end].strip()
        if body:
            sections.append((path, body))
    return sections


def _window(text: str, size_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= size_chars:
        return [text]
    parts, start = [], 0
    while start < len(text):
        end = min(start + size_chars, len(text))
        # try not to cut mid sentence
        if end < len(text):
            boundary = text.rfind("\n", start + size_chars // 2, end)
            if boundary == -1:
                boundary = text.rfind(". ", start + size_chars // 2, end)
            if boundary != -1:
                end = boundary + 1
        parts.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return [p for p in parts if p]


def chunk_markdown(
    markdown: str, *, chunk_size_tokens: int = 900, overlap_tokens: int = 150
) -> list[Chunk]:
    size_chars = chunk_size_tokens * APPROX_CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * APPROX_CHARS_PER_TOKEN

    chunks: list[Chunk] = []
    buffer, buffer_path = "", ""

    def flush():
        nonlocal buffer, buffer_path
        if buffer.strip():
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    content=buffer.strip(),
                    heading_path=buffer_path or None,
                    token_count=estimate_tokens(buffer),
                )
            )
        buffer = ""

    for path, body in _split_sections(markdown):
        prefixed = f"{path}\n\n{body}" if path else body
        if estimate_tokens(prefixed) > chunk_size_tokens:
            flush()
            for piece in _window(body, size_chars, overlap_chars):
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        content=(f"{path}\n\n{piece}" if path else piece),
                        heading_path=path or None,
                        token_count=estimate_tokens(piece),
                    )
                )
            continue

        if estimate_tokens(buffer + prefixed) > chunk_size_tokens:
            flush()
        if not buffer:
            buffer_path = path
        buffer += ("\n\n" if buffer else "") + prefixed

    flush()
    for i, c in enumerate(chunks):
        c.ordinal = i
    return chunks
