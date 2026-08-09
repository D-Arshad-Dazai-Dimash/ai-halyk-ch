"""Authority resolution.

Documents are not ranked globally. Each borrower selects a source
separately *per purpose*: the current loan agreement fixes covenant terms,
the current audited notes control disclosures, the KYC dossier defines
related parties, and a treasury memo supplies documented off-ledger
amounts.

The agreed-upon-procedures report is deliberately reference-gated. A report
marked final is not authoritative on its own: it controls reclassifications
only when the selected audited notes cite the same report number. The
corpus contains interim schedules that state preliminary positions and
explicitly disclaim being final, so accepting an unreferenced report would
apply a position the auditor withdrew.

Ambiguity is refused rather than resolved arbitrarily: zero or several
equally-valid candidates leave the purpose unresolved with a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from covenant_agent.documents.doctype import (
    DocType,
    Lifecycle,
    build_signals,
    classify_lifecycle,
    classify_type,
    DEFAULT_ACCOUNT_PREFIXES,
    extract_accounts,
    extract_audit_report_ids,
)


class Purpose(str, Enum):
    COVENANT_TERMS = "covenant_terms"
    FINANCIAL_DISCLOSURES = "financial_disclosures"
    RELATED_PARTIES = "related_parties"
    RECLASSIFICATIONS = "reclassifications"
    TREASURY_ADJUSTMENTS = "treasury_adjustments"


@dataclass(frozen=True)
class CatalogedDocument:
    path: Path
    content_hash: str
    doc_type: DocType
    lifecycle: Lifecycle
    accounts: tuple[str, ...]
    audit_report_ids: tuple[str, ...]
    reporting_year: int | None
    text: str


@dataclass
class AuthorityBundle:
    account_id: str
    selections: dict[Purpose, CatalogedDocument] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def get(self, purpose: Purpose) -> CatalogedDocument | None:
        return self.selections.get(purpose)


def catalog_document(
    path: Path,
    content_hash: str,
    text: str,
    year_hint: int | None = None,
    account_prefixes: tuple[str, ...] = DEFAULT_ACCOUNT_PREFIXES,
) -> CatalogedDocument:
    signals = build_signals(text)
    doc_type = classify_type(signals)
    return CatalogedDocument(
        path=path,
        content_hash=content_hash,
        doc_type=doc_type,
        lifecycle=classify_lifecycle(doc_type, signals),
        accounts=extract_accounts(text, account_prefixes),
        audit_report_ids=extract_audit_report_ids(text),
        reporting_year=year_hint,
        text=text,
    )


def _single(
    candidates: list[CatalogedDocument],
    purpose: Purpose,
    warnings: list[str],
    *,
    required: bool,
) -> CatalogedDocument | None:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        if required:
            warnings.append(f"missing_authority:{purpose.value}")
        return None
    warnings.append(f"ambiguous_authority:{purpose.value}:{len(candidates)}")
    return None


def resolve_authorities(
    account_id: str,
    documents: list[CatalogedDocument],
    reporting_year: int,
) -> AuthorityBundle:
    """Select one authority per purpose for a single borrower account."""
    bundle = AuthorityBundle(account_id=account_id)
    mine = [doc for doc in documents if account_id in doc.accounts]

    def of(doc_type: DocType, *lifecycles: Lifecycle) -> list[CatalogedDocument]:
        allowed = set(lifecycles)
        return [
            doc for doc in mine if doc.doc_type is doc_type and doc.lifecycle in allowed
        ]

    loan = _single(
        of(DocType.LOAN_AGREEMENT, Lifecycle.CURRENT),
        Purpose.COVENANT_TERMS,
        bundle.warnings,
        required=True,
    )
    if loan:
        bundle.selections[Purpose.COVENANT_TERMS] = loan

    notes = _single(
        of(DocType.FINANCIAL_STATEMENT_NOTES, Lifecycle.CURRENT),
        Purpose.FINANCIAL_DISCLOSURES,
        bundle.warnings,
        required=True,
    )
    if notes:
        bundle.selections[Purpose.FINANCIAL_DISCLOSURES] = notes

    kyc = _single(
        of(DocType.KYC_DOSSIER, Lifecycle.CURRENT),
        Purpose.RELATED_PARTIES,
        bundle.warnings,
        required=True,
    )
    if kyc:
        bundle.selections[Purpose.RELATED_PARTIES] = kyc

    treasury = _single(
        of(DocType.TREASURY_MEMO, Lifecycle.CURRENT),
        Purpose.TREASURY_ADJUSTMENTS,
        bundle.warnings,
        required=False,
    )
    if treasury:
        bundle.selections[Purpose.TREASURY_ADJUSTMENTS] = treasury

    reclassification = _resolve_reclassification_authority(
        of(DocType.AGREED_UPON_PROCEDURES, Lifecycle.FINAL), notes, bundle.warnings
    )
    if reclassification:
        bundle.selections[Purpose.RECLASSIFICATIONS] = reclassification

    _ = reporting_year
    return bundle


def _resolve_reclassification_authority(
    finals: list[CatalogedDocument],
    notes: CatalogedDocument | None,
    warnings: list[str],
) -> CatalogedDocument | None:
    """A final AUP report controls reclassifications only when the selected
    audited notes reference its report number.
    """
    if not finals:
        return None
    if notes is None:
        warnings.append("final_aup_without_selected_notes")
        return None

    referenced = [
        doc
        for doc in finals
        if set(doc.audit_report_ids) & set(notes.audit_report_ids)
    ]
    if len(referenced) == 1:
        return referenced[0]
    if not referenced:
        warnings.append("unreferenced_final_aup_ignored")
        return None
    warnings.append(f"ambiguous_referenced_final_aup:{len(referenced)}")
    return None
