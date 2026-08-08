"""Locate a scenario's financial-statement document among its non-contract
supporting documents (PLAN.md finding: ratio covenants may source figures
from audited financial statements rather than the ledger).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from covenant_agent.common.enums import DocumentType
from covenant_agent.documents.classify import classify_from_anchors, refine_other_with_ocr
from covenant_agent.ingestion.anchors import DocumentAnchors, extract_anchors
from covenant_agent.ingestion.discover import discover_documents
from covenant_agent.ingestion.pdf import raw_text

# A genuine "Notes to Financial Statements" document is structured as a
# numbered sequence of notes ("Примечание 1", "Примечание 2", ...) -- this
# holds even when a given note is pure narrative/policy/adjustment text
# with no stated dollar figures (e.g. a revenue cutoff correction for one
# transaction), which a dollar-anchor-only filter would wrongly reject.
_NOTE_HEADING_PATTERN = re.compile(r"Примечание\s+\d+", re.IGNORECASE)
_MIN_NOTE_HEADINGS = 3


@dataclass(frozen=True)
class StatementCandidate:
    path: Path
    content_hash: str
    anchors: DocumentAnchors


def _substantiveness_score(text: str, anchors: DocumentAnchors) -> tuple[int, int]:
    """(note_heading_count, dollar_anchor_count) -- both are independent
    signals of a genuine statement (vs. a passing mention in a KYC/
    compliance document); a document need only satisfy one to qualify.
    """
    return len(_NOTE_HEADING_PATTERN.findall(text)), len(anchors.dollar_amounts)


def find_financial_statement(documents_dir: Path, account_id: str) -> StatementCandidate | None:
    """Scans documents referencing `account_id` that are NOT credit
    agreements, keyword-classifies each (Tier-2, OCR-backed) as candidate
    FINANCIAL_STATEMENT documents, and returns the most substantive one.
    A document that merely *mentions* financial statements in passing
    (e.g. a KYC dossier referencing them in a due-diligence checklist) is
    filtered out: a genuine statement either states figures with a "$"
    prefix, or has the numbered "Примечание N" note structure (some notes
    are pure narrative/policy/correction text with no dollar figure of
    their own). Returns None if no qualifying candidate is found --
    callers must treat this as "no statement data available"
    (LOW_CONFIDENCE/UNRESOLVED), never invent figures.
    """
    candidates: list[tuple[StatementCandidate, tuple[int, int]]] = []
    for doc in discover_documents(documents_dir):
        text = raw_text(doc.path)
        anchors = extract_anchors(text)
        if account_id not in anchors.account_ids:
            continue
        tier1 = classify_from_anchors(anchors)
        if tier1 == DocumentType.CREDIT_AGREEMENT:
            continue

        score = _substantiveness_score(text, anchors)
        if score[0] < _MIN_NOTE_HEADINGS and score[1] == 0:
            continue  # passing mention only -- not a genuine statement

        refined = refine_other_with_ocr(doc.path, doc.content_hash)
        if refined == DocumentType.FINANCIAL_STATEMENT:
            candidates.append(
                (StatementCandidate(path=doc.path, content_hash=doc.content_hash, anchors=anchors), score)
            )

    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[1])[0]
