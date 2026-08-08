"""OCR with disk caching, keyed by content hash + page index.

Deliberately not called for every document: classification (documents/classify.py)
works off the cheap raw-text anchors alone (PLAN.md finding: Latin tokens
survive without OCR). OCR is reserved for the small set of effective
contracts (and any financial statements a covenant references) that actually
need their Cyrillic prose read -- see PLAN.md "Performance & Cost".
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from covenant_agent.common.config import OCR_CACHE_DIR, TESSERACT_LANGS
from covenant_agent.common.ocr_env import ocr_image
from covenant_agent.ingestion.discover import DiscoveredDocument
from covenant_agent.ingestion.pdf import page_count, render_page


def ocr_page(pdf_path: Path, page_index: int, content_hash: str) -> str:
    cache_dir = OCR_CACHE_DIR / content_hash
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"page_{page_index:03d}.txt"

    if cache_path.is_file():
        return cache_path.read_text(encoding="utf-8")

    image = render_page(pdf_path, page_index, content_hash)
    text = ocr_image(image, lang=TESSERACT_LANGS)

    # Publish the cache entry atomically. Concurrent workers may render the
    # same page, and a reader must never observe a half-written file.
    temporary = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, cache_path)
    return text


def ocr_document(pdf_path: Path, content_hash: str) -> str:
    """OCR every page of a document and concatenate, each page cached
    independently so a partially-processed document resumes cheaply.
    """
    n_pages = page_count(pdf_path)
    return "\n".join(ocr_page(pdf_path, i, content_hash) for i in range(n_pages))


def default_workers() -> int:
    """Worker count for corpus-wide OCR.

    Page rendering and Tesseract both spend their time outside the
    interpreter -- PyMuPDF in C, Tesseract in a child process -- so threads
    scale nearly linearly here without the memory cost of processes.
    """
    return max(1, min(16, (os.cpu_count() or 4)))


def prewarm(
    documents: list[DiscoveredDocument], workers: int | None = None
) -> tuple[int, int]:
    """Populate the page cache for a whole corpus in parallel.

    A cold corpus is the single slowest part of a run -- OCR at 300 dpi
    costs seconds per page, and a few hundred documents run to tens of
    minutes if done one at a time. Filling the cache up front turns the
    rest of the pipeline into cache reads.

    Failures are counted, not raised: one unreadable document must not stop
    the corpus, and the stage that needs it will report it unresolved.
    Returns ``(succeeded, failed)``.
    """
    succeeded = failed = 0

    def work(document: DiscoveredDocument) -> bool:
        try:
            ocr_document(document.path, document.content_hash)
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=workers or default_workers()) as pool:
        for ok in pool.map(work, documents):
            if ok:
                succeeded += 1
            else:
                failed += 1
    return succeeded, failed
