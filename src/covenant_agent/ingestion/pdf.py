"""PyMuPDF wrappers: raw text extraction (for anchors) and page rendering
(for OCR), with content-hash-keyed render caching.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import fitz
from PIL import Image

from covenant_agent.common.config import OCR_RENDER_DPI, RENDERS_CACHE_DIR
from covenant_agent.common.exceptions import DocumentExtractionError

# Defensive limits (THREAT_MODEL.md: malformed/zip-bomb PDF).
MAX_PAGES = 60
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50MB


def _open(pdf_path: Path) -> fitz.Document:
    if pdf_path.stat().st_size > MAX_FILE_BYTES:
        raise DocumentExtractionError(f"{pdf_path.name} exceeds size limit; refusing to process")
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        raise DocumentExtractionError(f"could not open {pdf_path.name}: {exc}") from exc
    if doc.page_count > MAX_PAGES:
        doc.close()
        raise DocumentExtractionError(
            f"{pdf_path.name} has {doc.page_count} pages, exceeds limit; refusing to process"
        )
    return doc


def raw_text(pdf_path: Path) -> str:
    """Concatenated raw text layer across all pages (Cyrillic garbled, Latin
    anchors intact -- see ingestion/anchors.py).
    """
    with _open(pdf_path) as doc:
        return "".join(page.get_text() for page in doc)


def page_count(pdf_path: Path) -> int:
    with _open(pdf_path) as doc:
        return doc.page_count


def render_page(
    pdf_path: Path, page_index: int, content_hash: str, dpi: int = OCR_RENDER_DPI
) -> Image.Image:
    """Render one page to a PIL Image, cached on disk by content hash so
    re-runs never re-render an unchanged document.
    """
    cache_dir = RENDERS_CACHE_DIR / content_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"page_{page_index:03d}.png"

    if cache_path.is_file():
        return Image.open(cache_path).convert("RGB")

    with _open(pdf_path) as doc:
        if page_index >= doc.page_count:
            raise DocumentExtractionError(
                f"page {page_index} out of range for {pdf_path.name} ({doc.page_count} pages)"
            )
        pix = doc[page_index].get_pixmap(dpi=dpi)
        png_bytes = pix.tobytes("png")

    # Publish atomically: concurrent workers may render the same page, and
    # a reader must never open a half-written image.
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(png_bytes)
    os.replace(temporary, cache_path)
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")
