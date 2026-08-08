"""Tesseract OCR environment wiring.

Resolves the tesseract binary and the project-local tessdata directory
(rus+eng language data) across platforms, and exposes a single ``ocr_image``
entry point used by the ingestion layer (P2). OCR is an *extraction
mechanism, not a source of truth* (DATA_MODEL.md hierarchy, level 4) -- its
output is untrusted document content, never treated as instructions.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

import pytesseract
from PIL import Image

from covenant_agent.common.config import TESSDATA_DIR, TESSERACT_LANGS
from covenant_agent.common.exceptions import DocumentExtractionError

_CONFIGURED = False

_WINDOWS_DEFAULT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def _resolve_tesseract_cmd() -> str:
    env_cmd = os.environ.get("TESSERACT_CMD")
    if env_cmd and Path(env_cmd).is_file():
        return env_cmd

    found = shutil.which("tesseract")
    if found:
        return found

    if platform.system() == "Windows":
        for candidate in _WINDOWS_DEFAULT_PATHS:
            if Path(candidate).is_file():
                return candidate

    raise DocumentExtractionError(
        "tesseract binary not found. Install it (see README.md setup) or set "
        "TESSERACT_CMD to its full path."
    )


def configure_pytesseract() -> None:
    """Idempotently wire pytesseract to the resolved binary + local tessdata."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    pytesseract.pytesseract.tesseract_cmd = _resolve_tesseract_cmd()

    if TESSDATA_DIR.is_dir() and any(TESSDATA_DIR.glob("*.traineddata")):
        os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
    # else: fall through to tesseract's own bundled tessdata (may lack `rus`).

    _CONFIGURED = True


def available_languages() -> set[str]:
    configure_pytesseract()
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception as exc:  # pytesseract raises various backend errors
        raise DocumentExtractionError(f"could not query tesseract languages: {exc}") from exc


def ocr_image(image: Image.Image, lang: str = TESSERACT_LANGS) -> str:
    """OCR a single rendered page image. Returns raw (untrusted) text."""
    configure_pytesseract()
    try:
        return pytesseract.image_to_string(image, lang=lang)
    except Exception as exc:
        raise DocumentExtractionError(f"OCR failed: {exc}") from exc
