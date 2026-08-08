"""Per-cell validation + confidence (DATA_MODEL.md ValidationResult).

The competition rule (CASE.md) is unambiguous: an empty cell scores exactly
the same as a wrong one, so every template cell must receive a best-effort
status/actual/evidence_txn_id regardless of confidence. This module does
NOT withhold an answer on low confidence -- it computes one and *also*
records how much to trust it, so the audit trail stays honest even when the
submitted value is a best-effort fallback.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from covenant_agent.calc.reconcile import ReconciledActual
from covenant_agent.common.enums import CovenantStatus, ValidationStatus
from covenant_agent.covenants.dsl import CovenantRule


class CellValidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    clause: str
    checks: tuple[str, ...]
    confidence: float
    status: ValidationStatus


def validate_cell(
    rule: CovenantRule,
    reconciled: ReconciledActual,
    evidence_txn_id: str | None,
    known_txn_ids: frozenset[str],
) -> CellValidation:
    checks: list[str] = []
    confidence = rule.confidence

    if not rule.is_structurally_valid():
        checks.append("dsl_structurally_invalid")
        confidence *= 0.3

    if reconciled.status == ValidationStatus.OK:
        checks.append("ab_agreement_ok")
    elif reconciled.status == ValidationStatus.LOW_CONFIDENCE:
        checks.append("ab_disagreement")
        confidence *= 0.5
    else:
        checks.append("actual_unresolved")
        confidence *= 0.1

    if evidence_txn_id is not None and evidence_txn_id not in known_txn_ids:
        checks.append("evidence_txn_id_not_in_ledger")
        confidence *= 0.2

    overall = ValidationStatus.OK
    if confidence < 0.3:
        overall = ValidationStatus.LOW_CONFIDENCE
    if reconciled.actual is None:
        overall = ValidationStatus.UNRESOLVED

    return CellValidation(
        scenario_id=rule.scenario_id,
        clause=rule.clause,
        checks=tuple(checks),
        confidence=max(0.0, min(1.0, confidence)),
        status=overall,
    )


def fallback_status_for_unresolved() -> CovenantStatus:
    """When `actual` genuinely cannot be computed, the pipeline must still
    submit *some* status (blank scores the same as wrong). COMPLIANT is the
    documented, deliberately arbitrary choice: it lets `actual` also default
    to the threshold itself (a defensible "at the boundary" guess) rather
    than an unrelated number, minimizing expected error on the 0.30-point
    `actual` component in the worst case where we have zero information.
    """
    return CovenantStatus.COMPLIANT
