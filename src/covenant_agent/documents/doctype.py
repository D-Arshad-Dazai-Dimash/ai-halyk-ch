"""Compound-signal document classification.

Opaque filenames carry no meaning, so type is decided from content -- and
from *combinations* of structural markers rather than single keywords,
because the corpus deliberately includes decoys that mention a type without
being one (a "compliance procedure for periodic KYC refresh" names KYC but
is not a KYC dossier; a weekly operations status names the borrower but
discloses nothing).

Everything matches on the OCR-canonical form (documents/ocr_text.py). This
matters for the registration IDs as much as for prose: OCR frequently
renders "KYC-ACC-…" with Cyrillic lookalikes ("КУС-АСС-…"), and glues the
number sign onto the account ("№" read as "Ne", giving "NeACC-…"), so
script-sensitive or word-boundary-anchored patterns silently fail to link
a document to its borrower.

Lifecycle is separate from type: an agreement may be current or superseded,
and an agreed-upon-procedures report may be an interim draft or the final
position. A draft never becomes calculation authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from covenant_agent.documents.ocr_text import canon, ru

# Registration identifiers, matched on canonical text. The account pattern
# deliberately omits a leading word boundary so a glued prefix ("NeACC-",
# "KYC-ACC-", "TRE-ACC-") still yields the account it names.
ACCOUNT_RE = re.compile(r"acc-(\d{3,6})(?!\d)")
KYC_REGISTRATION_RE = re.compile(r"kyc-acc-\d+-\d+")
AUDIT_REPORT_RE = re.compile(r"ar-(\d+-\d+)")
TREASURY_MEMO_RE = re.compile(r"tre-acc-\d+-\d+")


class DocType(str, Enum):
    LOAN_AGREEMENT = "loan_agreement"
    KYC_DOSSIER = "kyc_dossier"
    FINANCIAL_STATEMENT_NOTES = "financial_statement_notes"
    AGREED_UPON_PROCEDURES = "agreed_upon_procedures"
    TREASURY_MEMO = "treasury_memo"
    OTHER = "other"


class Lifecycle(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    DRAFT = "draft"
    FINAL = "final"
    UNKNOWN = "unknown"


def _tight(text: str) -> str:
    """Alphanumeric-only canonical form, immune to the letter and word
    spacing OCR introduces in headings.
    """
    return "".join(ch for ch in canon(text) if ch.isalnum())


@dataclass(frozen=True)
class DocSignals:
    flat: str  # canonical, whitespace-collapsed
    tight: str  # canonical, alphanumeric only

    def has(self, phrase: str) -> bool:
        """True when the phrase appears, spacing-insensitively."""
        return _tight(phrase) in self.tight


def build_signals(text: str) -> DocSignals:
    return DocSignals(flat=" ".join(canon(text).split()), tight=_tight(text))


def classify_type(signals: DocSignals) -> DocType:
    """Each type requires a *combination* of markers, so a document that
    merely mentions another type's vocabulary is not misfiled as it.
    Ambiguity (several types fully matched) resolves to OTHER rather than
    an arbitrary pick.
    """
    candidates: list[DocType] = []

    # A loan agreement is titled as one AND carries the covenants article.
    if signals.has("договор банковского займа") and signals.has("статья 6"):
        candidates.append(DocType.LOAN_AGREEMENT)

    # A KYC dossier carries a KYC registration ID; a compliance *procedure*
    # about KYC refresh does not. Registration IDs keep their hyphens, so
    # they are matched on the flat form rather than the alphanumeric one.
    if KYC_REGISTRATION_RE.search(signals.flat):
        candidates.append(DocType.KYC_DOSSIER)

    # Statement notes are titled as notes AND filed under an audit engagement.
    if signals.has("примечания к финансовой отчетности") and signals.has("аудиторское дело"):
        candidates.append(DocType.FINANCIAL_STATEMENT_NOTES)

    # An AUP document reviews transaction classification, as either an
    # interim schedule or the final agreed-upon-procedures report.
    if signals.has("классификации операций") and (
        signals.has("отчет о выполнении согласованных процедур")
        or signals.has("промежуточная ведомость")
    ):
        candidates.append(DocType.AGREED_UPON_PROCEDURES)

    if TREASURY_MEMO_RE.search(signals.flat) or (
        signals.has("служебная записка казначейства") and signals.has("операционное досье")
    ):
        candidates.append(DocType.TREASURY_MEMO)

    return candidates[0] if len(candidates) == 1 else DocType.OTHER


def classify_lifecycle(doc_type: DocType, signals: DocSignals) -> Lifecycle:
    if doc_type is DocType.LOAN_AGREEMENT:
        superseded = signals.has("недействующая редакция") or signals.has("не применяется")
        return Lifecycle.SUPERSEDED if superseded else Lifecycle.CURRENT

    if doc_type is DocType.AGREED_UPON_PROCEDURES:
        # An interim schedule explicitly disclaims being the final position.
        if signals.has("промежуточная ведомость") and signals.has("не является окончательной"):
            return Lifecycle.DRAFT
        if signals.has("отчет о выполнении согласованных процедур") and signals.has(
            "окончательной позицией"
        ):
            return Lifecycle.FINAL
        return Lifecycle.UNKNOWN

    if doc_type in {
        DocType.KYC_DOSSIER,
        DocType.FINANCIAL_STATEMENT_NOTES,
        DocType.TREASURY_MEMO,
    }:
        return Lifecycle.CURRENT
    return Lifecycle.UNKNOWN


def extract_accounts(text: str) -> tuple[str, ...]:
    """Account identifiers named anywhere in the document, normalized to the
    canonical ``ACC-nnnn`` surface form used by the ledger.
    """
    folded = canon(text)
    return tuple(sorted({f"ACC-{m.group(1)}" for m in ACCOUNT_RE.finditer(folded)}))


def extract_audit_report_ids(text: str) -> tuple[str, ...]:
    folded = canon(text)
    return tuple(sorted({f"AR-{m.group(1)}" for m in AUDIT_REPORT_RE.finditer(folded)}))


_ = ru  # re-exported vocabulary helper kept available to callers
