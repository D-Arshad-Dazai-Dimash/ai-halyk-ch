"""Verifier B: an independently-implemented recomputation of `actual`,
using pandas vectorized operations rather than Engine A's Python loops
(ARCHITECTURE.md `verify/` -- "must not share code with Engine A"). Its
sole purpose is to catch bugs in Engine A through disagreement; callers
(calc/reconcile.py) treat any material A/B disagreement as LOW_CONFIDENCE
rather than picking a winner.
"""

from __future__ import annotations

import pandas as pd

from covenant_agent.common.enums import MetricKind, Scope, Sign
from covenant_agent.covenants.dsl import CovenantRule, LedgerAggregateSource, StatementFigureSource
from covenant_agent.covenants.financial_statement import FinancialStatementExtract
from covenant_agent.ledger.categories import OPERATING_EXPENSE_CATEGORIES, REVENUE_CATEGORIES
from covenant_agent.ledger.normalize import NormalizedTransaction

_STATEMENT_FIELDS = {
    "revenue": lambda s: s.revenue,
    "operating_expenses": lambda s: s.operating_expenses,
    "capex": lambda s: s.capex,
    "group_capex": lambda s: s.group_capex if s.group_capex is not None else s.capex,
    "ebitda": lambda s: s.ebitda_adjusted if s.ebitda_adjusted is not None else s.ebitda_base,
}

# Mirrors calc/engine_a.py's ledger-aggregate fallback for scenarios with no
# financial-statement document -- kept as an independently-expressed rule
# (category set + sign, executed via pandas below) so Verifier B stays a
# genuinely separate implementation rather than calling into Engine A.
_STATEMENT_FALLBACK = {
    "revenue": (frozenset(c.value for c in REVENUE_CATEGORIES), Sign.INFLOW),
    "operating_expenses": (frozenset(c.value for c in OPERATING_EXPENSE_CATEGORIES), Sign.OUTFLOW),
}


def _transactions_to_frame(transactions: list[NormalizedTransaction]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "txn_id": [t.txn_id for t in transactions],
            "date": [t.date for t in transactions],
            "category": [t.category.value for t in transactions],
            "is_outflow": [t.is_outflow for t in transactions],
            "amount_usd_abs": [t.amount_usd_abs for t in transactions],
        }
    )


def _aggregate_ledger_source_pandas(
    source: LedgerAggregateSource, frame: pd.DataFrame, period_start: str, period_end: str
) -> float:
    if frame.empty:
        return 0.0

    mask = (frame["date"] >= period_start) & (frame["date"] <= period_end)

    if source.include_categories:
        mask &= frame["category"].isin([c.value for c in source.include_categories])
    if source.exclude_categories:
        mask &= ~frame["category"].isin([c.value for c in source.exclude_categories])

    if source.sign == Sign.OUTFLOW:
        mask &= frame["is_outflow"]
    elif source.sign == Sign.INFLOW:
        mask &= ~frame["is_outflow"]

    subset = frame.loc[mask, "amount_usd_abs"]
    if subset.empty:
        return 0.0
    if source.scope == Scope.SINGLE_TXN:
        return float(subset.max())
    return float(subset.sum())


def _statement_fallback_pandas(
    field_name: str, frame: pd.DataFrame, period_start: str, period_end: str
) -> float | None:
    fallback = _STATEMENT_FALLBACK.get(field_name)
    if fallback is None or frame.empty:
        return None
    categories, sign = fallback
    mask = (frame["date"] >= period_start) & (frame["date"] <= period_end) & frame["category"].isin(categories)
    mask &= frame["is_outflow"] if sign == Sign.OUTFLOW else ~frame["is_outflow"]
    return float(frame.loc[mask, "amount_usd_abs"].sum())


def _resolve_source_pandas(
    source: LedgerAggregateSource | StatementFigureSource | None,
    frame: pd.DataFrame,
    period_start: str,
    period_end: str,
    statement: FinancialStatementExtract | None,
) -> float | None:
    if source is None:
        return None
    if isinstance(source, LedgerAggregateSource):
        return _aggregate_ledger_source_pandas(source, frame, period_start, period_end)
    if isinstance(source, StatementFigureSource):
        getter = _STATEMENT_FIELDS.get(source.field_name)
        value = getter(statement) if (statement is not None and getter is not None) else None
        if value is not None:
            return value
        return _statement_fallback_pandas(source.field_name, frame, period_start, period_end)
    return None


def compute_actual_independent(
    rule: CovenantRule,
    transactions: list[NormalizedTransaction],
    statement: FinancialStatementExtract | None,
) -> float | None:
    frame = _transactions_to_frame(transactions)

    if rule.metric_kind == MetricKind.AMOUNT:
        value = _resolve_source_pandas(rule.amount_source, frame, rule.period_start, rule.period_end, statement)
        return abs(value) if value is not None else None

    if rule.metric_kind == MetricKind.RATIO:
        numerator = _resolve_source_pandas(rule.numerator, frame, rule.period_start, rule.period_end, statement)
        denominator = _resolve_source_pandas(rule.denominator, frame, rule.period_start, rule.period_end, statement)
        if numerator is None or denominator is None or denominator == 0:
            return None
        return abs(numerator / denominator)

    return None
