"""Deterministic extraction of "Latin anchors" from a PDF's raw text layer.

PLAN.md's dirty-PDF finding: PyMuPDF's raw text extraction garbles Cyrillic
body prose (broken font encoding) but Latin/numeric tokens -- account ids,
ISO dates, dollar amounts, ratio multiples, and a handful of English
keywords -- survive intact. These anchors are the *authoritative* source for
numeric values (COVENANT_DSL.md: threshold/date values are overridden by
anchors, never trusted from OCR/LLM alone) and are cheap to compute without
any OCR or LLM call, so they run over every document unconditionally.

This module performs no interpretation of covenant semantics -- only
mechanical pattern extraction reusable across any contract wording.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

ACCOUNT_ID_PATTERN = re.compile(r"\bACC-\d+\b")
ISO_DATE_PATTERN = re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b")
DOLLAR_AMOUNT_PATTERN = re.compile(r"\$\s?([\d,]+\.\d{2})")
RATIO_MULTIPLE_PATTERN = re.compile(r"\b(\d{1,3}(?:\.\d{1,2})?)[xX]\b")
# Clause markers like "6.1" as a standalone token -- excludes larger numbers
# such as "16.12" or "6.10" via negative lookaround on adjacent digits.
CLAUSE_MARKER_PATTERN = re.compile(r"(?<!\d)(\d{1,2}\.\d)(?!\d)")


@dataclass(frozen=True)
class DocumentAnchors:
    account_ids: tuple[str, ...]
    dates: tuple[str, ...]
    dollar_amounts: tuple[float, ...]
    ratio_multiples: tuple[float, ...]
    clause_markers: tuple[str, ...]
    has_ebitda_keyword: bool
    has_kyc_keyword: bool

    @property
    def has_all_six_point_clauses(self) -> bool:
        """True iff 6.1, 6.2 and 6.3 all appear as clause markers -- the
        signal used (in addition to real interpretation) to flag a document
        as a credit agreement containing the covenants section.
        """
        return {"6.1", "6.2", "6.3"}.issubset(set(self.clause_markers))


def extract_anchors(text: str) -> DocumentAnchors:
    account_ids = tuple(sorted(set(ACCOUNT_ID_PATTERN.findall(text))))
    dates = tuple(sorted(set(ISO_DATE_PATTERN.findall(text))))
    dollar_amounts = tuple(
        sorted({float(m.replace(",", "")) for m in DOLLAR_AMOUNT_PATTERN.findall(text)})
    )
    ratio_multiples = tuple(
        sorted({float(m) for m in RATIO_MULTIPLE_PATTERN.findall(text)})
    )
    clause_markers = tuple(sorted(set(CLAUSE_MARKER_PATTERN.findall(text))))
    return DocumentAnchors(
        account_ids=account_ids,
        dates=dates,
        dollar_amounts=dollar_amounts,
        ratio_multiples=ratio_multiples,
        clause_markers=clause_markers,
        has_ebitda_keyword="EBITDA" in text,
        has_kyc_keyword="KYC" in text,
    )


def count_date_occurrences(text: str) -> Counter[str]:
    """Frequency of each distinct ISO date literal in the raw text (not
    deduplicated). Used by resolve/ to find a contract's reporting-period
    boundaries: a credit agreement restates "period from X to Y" once per
    covenant clause, so the period's start/end dates are the most-repeated
    dates in the document, while one-off dates (e.g. a signature date)
    appear only once. This holds regardless of the specific calendar
    convention (full year, fiscal year, etc.), so it generalizes.
    """
    return Counter(ISO_DATE_PATTERN.findall(text))
