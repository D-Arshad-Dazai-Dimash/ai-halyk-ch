"""Document type classification.

Two-tier, matching PLAN.md's dirty-PDF finding:

Tier 1 (fast, always run): classify from raw-text anchors alone -- no OCR
needed, because the signals that matter here (the "6.1"/"6.2"/"6.3" covenant
markers, the "KYC" keyword) are Latin tokens that survive PyMuPDF's raw text
extraction even though the surrounding Cyrillic prose is garbled.

Tier 2 (OCR-backed refinement, run only on demand): for documents that
Tier 1 leaves as OTHER, callers that specifically need to distinguish
financial statements / audit notes / compliance / correspondence / policy
documents (e.g. the covenant DSL compiler looking for a borrower's financial
statement) can call `refine_other_with_ocr`, which reads the OCR'd Russian
text and applies keyword rules. This keeps the default classification pass
over all ~200 documents cheap, and reserves OCR for the minority of
documents whose finer type actually matters downstream.
"""

from __future__ import annotations

from pathlib import Path

from covenant_agent.common.enums import DocumentType
from covenant_agent.ingestion.anchors import DocumentAnchors, extract_anchors
from covenant_agent.ingestion.ocr_cache import ocr_document
from covenant_agent.ingestion.pdf import raw_text

# Russian keyword signals for Tier 2 refinement. Kept as a small, principled
# lexicon derived from standard credit-file document taxonomy (financial
# reporting / audit / compliance / correspondence / policy vocabulary) --
# not fit to any specific document's wording.
_FINANCIAL_STATEMENT_KEYWORDS = (
    "финансовая отчётность",
    "финансовой отчётности",
    "баланс",
    "отчёт о прибылях",
    "отчет о прибылях",
    "выручка",
)
_AUDIT_KEYWORDS = ("аудит", "аудитор", "аудиторск")
_COMPLIANCE_KEYWORDS = ("комплаенс", "kyc", "due diligence", "должная осмотрительность")
_CORRESPONDENCE_KEYWORDS = ("уважаемый", "с уважением", "письмо", "correspondence")
_POLICY_KEYWORDS = ("политика", "регламент", "протокол")


def classify_from_anchors(anchors: DocumentAnchors) -> DocumentType:
    if anchors.has_all_six_point_clauses:
        return DocumentType.CREDIT_AGREEMENT
    if anchors.has_kyc_keyword:
        return DocumentType.KYC
    return DocumentType.OTHER


def classify_document(pdf_path: Path) -> tuple[DocumentType, DocumentAnchors]:
    """Tier-1 classification: anchors only, no OCR."""
    text = raw_text(pdf_path)
    anchors = extract_anchors(text)
    return classify_from_anchors(anchors), anchors


def refine_other_with_ocr(pdf_path: Path, content_hash: str) -> DocumentType:
    """Tier-2 refinement for a document already classified OTHER. Runs OCR
    (cached) and applies keyword rules to distinguish financial_statement /
    audit / compliance / correspondence / policy. Falls back to OTHER if no
    keyword group matches clearly.
    """
    text = ocr_document(pdf_path, content_hash).lower()

    scores = {
        DocumentType.FINANCIAL_STATEMENT: sum(k in text for k in _FINANCIAL_STATEMENT_KEYWORDS),
        DocumentType.AUDIT: sum(k in text for k in _AUDIT_KEYWORDS),
        DocumentType.COMPLIANCE: sum(k in text for k in _COMPLIANCE_KEYWORDS),
        DocumentType.CORRESPONDENCE: sum(k in text for k in _CORRESPONDENCE_KEYWORDS),
        DocumentType.POLICY: sum(k in text for k in _POLICY_KEYWORDS),
    }
    best_type, best_score = max(scores.items(), key=lambda kv: kv[1])
    return best_type if best_score > 0 else DocumentType.OTHER
