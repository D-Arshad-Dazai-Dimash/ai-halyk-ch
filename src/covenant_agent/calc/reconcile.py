"""A/B reconciliation (ARCHITECTURE.md "Calc A vs Verifier B -> LOW_CONFIDENCE").
Never picks a winner on disagreement -- degrades to LOW_CONFIDENCE so the
validation layer can decide what to do, per the independent-verification
principle in PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from covenant_agent.calc.engine_a import compute_actual
from covenant_agent.common.config import AB_AGREEMENT_REL_TOL
from covenant_agent.common.enums import ValidationStatus
from covenant_agent.covenants.dsl import CovenantRule
from covenant_agent.covenants.financial_statement import FinancialStatementExtract
from covenant_agent.ledger.fx import as_decimal
from covenant_agent.ledger.normalize import NormalizedTransaction
from covenant_agent.verify.engine_b import compute_actual_independent


@dataclass(frozen=True)
class ReconciledActual:
    actual: float | None
    engine_a_value: float | None
    engine_b_value: float | None
    agrees: bool
    status: ValidationStatus


def reconcile_actual(
    rule: CovenantRule,
    transactions: list[NormalizedTransaction],
    statement: FinancialStatementExtract | None,
) -> ReconciledActual:
    a = compute_actual(rule, transactions, statement)
    b = compute_actual_independent(rule, transactions, statement)
    # The two engines may hold their results as Decimal or plain numbers;
    # compare them in exact decimal arithmetic so the agreement check is
    # not itself a source of floating-point drift.
    a = as_decimal(a) if a is not None else None
    b = as_decimal(b) if b is not None else None

    if a is None and b is None:
        return ReconciledActual(None, None, None, False, ValidationStatus.UNRESOLVED)
    if a is None or b is None:
        return ReconciledActual(a if a is not None else b, a, b, False, ValidationStatus.LOW_CONFIDENCE)

    rel_error = abs(a - b) / abs(a) if a != 0 else abs(a - b)
    if rel_error <= AB_AGREEMENT_REL_TOL:
        return ReconciledActual(a, a, b, True, ValidationStatus.OK)
    return ReconciledActual(a, a, b, False, ValidationStatus.LOW_CONFIDENCE)
