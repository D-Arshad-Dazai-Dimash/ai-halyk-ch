"""Calculation Engine A: deterministic actual/status computation from a
validated CovenantRule + normalized ledger + (optionally) a financial
statement extract. No LLM involvement anywhere in this module -- pure
arithmetic over already-validated, already-categorized data (PLAN.md
computation boundary).
"""

from __future__ import annotations

from covenant_agent.common.enums import CovenantStatus, MetricKind, Operator, Scope, Sign
from covenant_agent.covenants.dsl import CovenantRule, LedgerAggregateSource, StatementFigureSource
from covenant_agent.covenants.financial_statement import FinancialStatementExtract
from covenant_agent.ledger.categories import OPERATING_EXPENSE_CATEGORIES, REVENUE_CATEGORIES
from covenant_agent.ledger.fx import as_decimal
from covenant_agent.ledger.normalize import NormalizedTransaction


def select_in_scope_transactions(
    source: LedgerAggregateSource,
    transactions: list[NormalizedTransaction],
    period_start: str,
    period_end: str,
) -> list[NormalizedTransaction]:
    filtered = [t for t in transactions if period_start <= t.date <= period_end]

    if source.include_categories:
        include = set(source.include_categories)
        filtered = [t for t in filtered if t.category in include]
    if source.exclude_categories:
        exclude = set(source.exclude_categories)
        filtered = [t for t in filtered if t.category not in exclude]

    if source.sign == Sign.OUTFLOW:
        filtered = [t for t in filtered if t.is_outflow]
    elif source.sign == Sign.INFLOW:
        filtered = [t for t in filtered if not t.is_outflow]

    return filtered


def aggregate_ledger_source(
    source: LedgerAggregateSource,
    transactions: list[NormalizedTransaction],
    period_start: str,
    period_end: str,
) -> float:
    in_scope = select_in_scope_transactions(source, transactions, period_start, period_end)
    if source.scope == Scope.SINGLE_TXN:
        return max((t.amount_usd_abs for t in in_scope), default=0.0)
    return sum(t.amount_usd_abs for t in in_scope)


_STATEMENT_FIELD_GETTERS = {
    "revenue": lambda s: s.revenue,
    "operating_expenses": lambda s: s.operating_expenses,
    "capex": lambda s: s.capex,
    "group_capex": lambda s: s.group_capex if s.group_capex is not None else s.capex,
    "ebitda": lambda s: s.ebitda_adjusted if s.ebitda_adjusted is not None else s.ebitda_base,
}


_STATEMENT_FALLBACK_LEDGER_SOURCE = {
    "revenue": LedgerAggregateSource(include_categories=tuple(REVENUE_CATEGORIES), sign=Sign.INFLOW),
    "operating_expenses": LedgerAggregateSource(
        include_categories=tuple(OPERATING_EXPENSE_CATEGORIES), sign=Sign.OUTFLOW
    ),
}


def resolve_statement_source(
    source: StatementFigureSource,
    statement: FinancialStatementExtract | None,
    transactions: list[NormalizedTransaction],
    period_start: str,
    period_end: str,
) -> float | None:
    getter = _STATEMENT_FIELD_GETTERS.get(source.field_name)
    value = getter(statement) if (statement is not None and getter is not None) else None
    if value is not None:
        return value

    # No statement document for this scenario, or the field wasn't stated
    # in it -- fall back to summing the corresponding ledger line items
    # (only defined for revenue/operating_expenses; capex/ebitda/group_capex
    # have no well-defined ledger-only equivalent and stay unresolved).
    fallback_source = _STATEMENT_FALLBACK_LEDGER_SOURCE.get(source.field_name)
    if fallback_source is None:
        return None
    return aggregate_ledger_source(fallback_source, transactions, period_start, period_end)


def resolve_metric_source(
    source: LedgerAggregateSource | StatementFigureSource | None,
    transactions: list[NormalizedTransaction],
    period_start: str,
    period_end: str,
    statement: FinancialStatementExtract | None,
) -> float | None:
    if source is None:
        return None
    if isinstance(source, LedgerAggregateSource):
        return aggregate_ledger_source(source, transactions, period_start, period_end)
    if isinstance(source, StatementFigureSource):
        return resolve_statement_source(source, statement, transactions, period_start, period_end)
    return None


def compute_actual(
    rule: CovenantRule,
    transactions: list[NormalizedTransaction],
    statement: FinancialStatementExtract | None,
) -> float | None:
    """Returns the covenant's `actual` metric value (always non-negative),
    or None if it cannot be computed (missing statement data, zero
    denominator, etc.) -- callers must treat None as LOW_CONFIDENCE/
    UNRESOLVED, never substitute a default.
    """
    if rule.metric_kind == MetricKind.AMOUNT:
        value = resolve_metric_source(rule.amount_source, transactions, rule.period_start, rule.period_end, statement)
        return abs(as_decimal(value)) if value is not None else None

    if rule.metric_kind == MetricKind.RATIO:
        numerator = resolve_metric_source(rule.numerator, transactions, rule.period_start, rule.period_end, statement)
        denominator = resolve_metric_source(rule.denominator, transactions, rule.period_start, rule.period_end, statement)
        if numerator is None or denominator is None or denominator == 0:
            return None
        # Statement figures and ledger aggregates can arrive as plain
        # numbers; divide in exact decimal arithmetic either way.
        return abs(as_decimal(numerator) / as_decimal(denominator))

    return None


_COMPARATORS = {
    Operator.LE: lambda actual, threshold: actual <= threshold,
    Operator.GE: lambda actual, threshold: actual >= threshold,
    Operator.LT: lambda actual, threshold: actual < threshold,
    Operator.GT: lambda actual, threshold: actual > threshold,
    Operator.EQ: lambda actual, threshold: actual == threshold,
}


def evaluate_status(rule: CovenantRule, actual: float) -> CovenantStatus:
    comparator = _COMPARATORS[rule.operator]
    return CovenantStatus.COMPLIANT if comparator(actual, rule.threshold) else CovenantStatus.BREACH
