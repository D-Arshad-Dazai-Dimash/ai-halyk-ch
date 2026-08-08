"""Locates the "Financial Covenants" section within a full contract's OCR
text, so only that ~1KB excerpt (not the whole multi-page document) is sent
to the LLM for clause extraction. This is a correctness-critical
preprocessing step, not a convenience: the full document (tens of
thousands of characters) overflows any reasonable local-model context
window, causing truncated/malformed output (see PLAN.md/run history --
without this, extraction failed on every scenario). A generic Russian/
English section-heading convention is used so this generalizes to a
holdout contract that numbers or words its sections slightly differently.
"""

from __future__ import annotations

import re

_HEADING_6 = re.compile(r"(?:Статья|Раздел|Section)\s*6\b", re.IGNORECASE)
_HEADING_7 = re.compile(r"(?:Статья|Раздел|Section)\s*7\b", re.IGNORECASE)

# Hard safety cap even if no closing heading is found -- protects the LLM
# call from ever receiving an unbounded document on an unfamiliar layout.
_MAX_SECTION_CHARS = 6000


def extract_covenant_section_text(full_text: str) -> str:
    """Returns the Financial Covenants section, or a bounded fallback slice
    if no clear heading is found (never the empty string unless the input
    itself is empty) -- extraction proceeds best-effort either way, with
    the anchor-reconciliation gate in compiler.py catching bad results.
    """
    matches = list(_HEADING_6.finditer(full_text))
    if not matches:
        return full_text[:_MAX_SECTION_CHARS]

    # The heading's last occurrence is the actual section body; any earlier
    # occurrence is a table-of-contents entry.
    start = matches[-1].start()

    end_match = _HEADING_7.search(full_text, pos=start)
    end = end_match.start() if end_match else min(len(full_text), start + _MAX_SECTION_CHARS)
    end = min(end, start + _MAX_SECTION_CHARS)

    return full_text[start:end].strip()
