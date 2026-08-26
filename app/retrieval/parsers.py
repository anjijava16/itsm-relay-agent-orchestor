"""Document → markdown. Docling first, cheap fallbacks after."""

from __future__ import annotations

import io
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

DOCLING_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/html",
}


def parse(data: bytes, content_type: str, filename: str = "") -> tuple[str, dict[str, Any]]:
    """Return (markdown, metadata). Never raises - a failed parse becomes an empty doc."""
    ct = (content_type or "").split(";")[0].strip().lower()

    if ct in ("text/plain", "text/markdown", "application/json", "text/csv"):
        return data.decode("utf-8", errors="replace"), {"parser": "plaintext"}

    if ct in DOCLING_TYPES:
        try:
            return _parse_with_docling(data, filename or "document"), {"parser": "docling"}
        except Exception as exc:
            log.warning("docling_failed_falling_back", error=str(exc), content_type=ct)

    if ct == "application/pdf":
        return _parse_pdf_fallback(data), {"parser": "pypdf"}

    if ct == "text/html":
        return _parse_html_fallback(data), {"parser": "markdownify"}

    return data.decode("utf-8", errors="replace"), {"parser": "raw-decode"}


def _parse_with_docling(data: bytes, filename: str) -> str:
    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(DocumentStream(name=filename, stream=io.BytesIO(data)))
    return result.document.export_to_markdown()


def _parse_pdf_fallback(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"## Page {i}\n\n{text}")
    return "\n\n".join(pages)


def _parse_html_fallback(data: bytes) -> str:
    from markdownify import markdownify

    return markdownify(data.decode("utf-8", errors="replace"), heading_style="ATX")
