"""Evidence engine: formal counterfactual causal search (COVENANT_DSL.md
"Evidence model"). Implements the `exclude` operation, which covers the
dominant real-world patterns in this problem domain: a single-payment cap
(removing the one over-cap payment flips the verdict) and an aggregate
cap/floor or ratio crossed by exactly one transaction. `include`,
`reclassify`, and `correct` are richer counterfactual operations noted in
COVENANT_DSL.md as an extension point for future work; they are not
implemented here because our source data does not represent alternate
(not-yet-applied) classifications for a transaction to include/reclassify
into -- only what's already booked, which `exclude` fully covers.

Efficiency: aggregate deltas are O(1) per candidate (`total - txn`, no
re-scan of the ledger); the whole ledger is never reprocessed once per
candidate (COVENANT_DSL.md "Efficiency").
"""

from __future__ import annotations

from covenant_agent.calc.engine_a import (
    aggregate_ledger_source,
    evaluate_status,
    resolve_metric_source,
    select_in_scope_transactions,
)
from covenant_agent.common.enums import CovenantStatus, MetricKind, Scope
from covenant_agent.covenants.dsl import CovenantRule, LedgerAggregateSource
from covenant_agent.covenants.financial_statement import FinancialStatementExtract
from covenant_agent.ledger.fx import as_decimal
from covenant_agent.ledger.normalize import NormalizedTransaction


def _find_evidence_for_amount_covenant(
    rule: CovenantRule,
    transactions: list[NormalizedTransaction],
    baseline_status: CovenantStatus,
) -> str | None:
    source = rule.amount_source
    if not isinstance(source, LedgerAggregateSource):
        return None  # statement-sourced amount: no candidate ledger transaction

    in_scope = select_in_scope_transactions(source, transactions, rule.period_start, rule.period_end)
    if not in_scope:
        return None

    if source.scope == Scope.SINGLE_TXN:
        max_value = max(t.amount_usd_abs for t in in_scope)
        max_txns = [t for t in in_scope if t.amount_usd_abs == max_value]
        if len(max_txns) != 1:
            return None  # tie for the max -> not a unique determinant
        remaining = [t.amount_usd_abs for t in in_scope if t.txn_id != max_txns[0].txn_id]
        counterfactual_actual = max(remaining, default=0.0)
        if evaluate_status(rule, counterfactual_actual) != baseline_status:
            return max_txns[0].txn_id
        return None

    # AGGREGATE scope: O(1) delta per candidate via total - txn.
    total = sum(t.amount_usd_abs for t in in_scope)
    causal = [
        t.txn_id
        for t in in_scope
        if evaluate_status(rule, total - t.amount_usd_abs) != baseline_status
    ]
    return causal[0] if len(causal) == 1 else None


def _find_evidence_for_ratio_covenant(
    rule: CovenantRule,
    transactions: list[NormalizedTransaction],
    statement: FinancialStatementExtract | None,
    baseline_status: CovenantStatus,
) -> str | None:
    causal: set[str] = set()

    for side_name, other_name in (("numerator", "denominator"), ("denominator", "numerator")):
        source = getattr(rule, side_name)
        if not isinstance(source, LedgerAggregateSource) or source.scope != Scope.AGGREGATE:
            continue  # only AGGREGATE-scope ledger components have well-defined O(1) deltas here

        other_source = getattr(rule, other_name)
        other_value = resolve_metric_source(
            other_source, transactions, rule.period_start, rule.period_end, statement
        )
        if other_value is None or other_value == 0:
            continue

        in_scope = select_in_scope_transactions(source, transactions, rule.period_start, rule.period_end)
        base_component = aggregate_ledger_source(source, transactions, rule.period_start, rule.period_end)

        for t in in_scope:
            cf_component = as_decimal(base_component) - as_decimal(t.amount_usd_abs)
            other = as_decimal(other_value)
            if side_name == "numerator":
                cf_actual = abs(cf_component / other)
            else:
                if cf_component == 0:
                    continue
                cf_actual = abs(other / cf_component)
            if evaluate_status(rule, cf_actual) != baseline_status:
                causal.add(t.txn_id)

    return next(iter(causal)) if len(causal) == 1 else None


def find_evidence(
    rule: CovenantRule,
    transactions: list[NormalizedTransaction],
    statement: FinancialStatementExtract | None,
    baseline_actual: float,
    baseline_status: CovenantStatus,
) -> str | None:
    """Returns the unique causal txn_id, or None (aggregate/ratio with no
    single determinant, statement-only metric, or a non-unique candidate
    set). Never guesses among multiple plausible candidates.
    """
    if not rule.evidence_applicable:
        return None

    if rule.metric_kind == MetricKind.AMOUNT:
        return _find_evidence_for_amount_covenant(rule, transactions, baseline_status)
    if rule.metric_kind == MetricKind.RATIO:
        return _find_evidence_for_ratio_covenant(rule, transactions, statement, baseline_status)
    return None
