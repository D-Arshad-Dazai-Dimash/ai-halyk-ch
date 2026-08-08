"""Entity + version resolution (PLAN.md stage 5 / ARCHITECTURE.md `resolve/`).

For each scenario (derived purely from the ledger, see ledger/loader.py),
find all candidate credit-agreement documents referencing its account_id,
and pick the *effective* one -- the edition whose reporting period matches
the ledger's reporting year. All logic here is general-purpose: nothing is
keyed off a specific scenario id, account id, or filename.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from covenant_agent.common.enums import DocumentType
from covenant_agent.documents.classify import classify_from_anchors
from covenant_agent.ingestion.anchors import DocumentAnchors, count_date_occurrences, extract_anchors
from covenant_agent.ingestion.discover import discover_documents
from covenant_agent.ingestion.pdf import raw_text


@dataclass(frozen=True)
class CreditAgreementCandidate:
    path: Path
    content_hash: str
    account_id: str | None  # None if ambiguous/absent -> needs name-match fallback (not yet resolved)
    period_start: str | None
    period_end: str | None
    anchors: DocumentAnchors


@dataclass(frozen=True)
class ResolvedContract:
    scenario_id: str
    account_id: str
    effective: CreditAgreementCandidate
    superseded: tuple[CreditAgreementCandidate, ...]  # all other candidates for this account, for audit


def _resolve_period(text: str) -> tuple[str | None, str | None]:
    """The two most-repeated distinct ISO dates in the document, chronologically
    ordered, are taken as (period_start, period_end). See
    ingestion/anchors.count_date_occurrences for the rationale.
    """
    counts = count_date_occurrences(text)
    if not counts:
        return None, None
    top = [date for date, _ in counts.most_common(2)]
    if len(top) == 1:
        return top[0], top[0]
    start, end = sorted(top)
    return start, end


def discover_credit_agreement_candidates(documents_dir: Path) -> list[CreditAgreementCandidate]:
    """Tier-1 scan of every PDF: keep only those classified CREDIT_AGREEMENT
    (via anchors alone -- no OCR needed, see documents/classify.py), with
    account_id + resolved reporting period attached.
    """
    candidates: list[CreditAgreementCandidate] = []
    for doc in discover_documents(documents_dir):
        text = raw_text(doc.path)
        anchors = extract_anchors(text)
        if classify_from_anchors(anchors) != DocumentType.CREDIT_AGREEMENT:
            continue
        account_id = anchors.account_ids[0] if len(anchors.account_ids) == 1 else None
        period_start, period_end = _resolve_period(text)
        candidates.append(
            CreditAgreementCandidate(
                path=doc.path,
                content_hash=doc.content_hash,
                account_id=account_id,
                period_start=period_start,
                period_end=period_end,
                anchors=anchors,
            )
        )
    return candidates


def resolve_effective_contracts(
    documents_dir: Path,
    account_to_scenario: dict[str, str],
    ledger_reporting_year: int,
) -> dict[str, ResolvedContract]:
    """For every scenario with a known account_id, pick the credit-agreement
    edition whose period covers (or ends in) the ledger's reporting year.

    Returns scenario_id -> ResolvedContract. Scenarios with zero or
    ambiguous (>1, equally-scored) candidates are omitted -- callers treat
    a missing scenario as UNRESOLVED rather than guessing (ARCHITECTURE.md
    failure boundaries).
    """
    candidates = discover_credit_agreement_candidates(documents_dir)

    by_account: dict[str, list[CreditAgreementCandidate]] = {}
    for c in candidates:
        if c.account_id is None:
            continue  # ambiguous account reference; not handled by this deterministic path
        by_account.setdefault(c.account_id, []).append(c)

    resolved: dict[str, ResolvedContract] = {}
    for account_id, cands in by_account.items():
        scenario_id = account_to_scenario.get(account_id)
        if scenario_id is None:
            continue  # document references an account not present in this ledger (decoy)

        def _period_end_year(c: CreditAgreementCandidate) -> int | None:
            return int(c.period_end[:4]) if c.period_end else None

        effective_matches = [c for c in cands if _period_end_year(c) == ledger_reporting_year]

        if len(effective_matches) == 1:
            effective = effective_matches[0]
        elif len(cands) == 1:
            # Only one candidate at all for this account -- use it even if
            # its period doesn't match exactly (better than UNRESOLVED).
            effective = cands[0]
        else:
            continue  # ambiguous: 0 or >1 equally-plausible effective editions -> UNRESOLVED

        superseded = tuple(c for c in cands if c is not effective)
        resolved[scenario_id] = ResolvedContract(
            scenario_id=scenario_id,
            account_id=account_id,
            effective=effective,
            superseded=superseded,
        )

    return resolved
