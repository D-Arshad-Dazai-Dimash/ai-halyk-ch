from __future__ import annotations

from covenant_agent.common.enums import MetricKind, Operator, Scope, Sign, ValidationStatus
from covenant_agent.calc.reconcile import reconcile_actual
from covenant_agent.covenants.dsl import CovenantRule, LedgerAggregateSource, StatementFigureSource
from covenant_agent.ledger.categories import Category
from covenant_agent.ledger.model import RawTransaction
from covenant_agent.ledger.normalize import normalize_transaction


def _txn(txn_id, date, amount, description):
    raw = RawTransaction(
        txn_id=txn_id, date=date, account_id="ACC-1001", counterparty="Acme",
        description=description, amount=amount, currency="USD",
    )
    return normalize_transaction(raw, eur_to_usd_rate=1.08, use_llm_fallback=False)


def test_engine_a_and_b_agree_on_simple_aggregate():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -1_000_000, "Purchase of equipment"),
        _txn("TXN-X1-0002", "2025-03-01", -2_500_000, "Purchase of equipment"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=3_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
    )
    result = reconcile_actual(rule, txns, statement=None)
    assert result.status == ValidationStatus.OK
    assert result.agrees is True
    assert result.actual == 3_500_000.0
    assert result.engine_a_value == result.engine_b_value


def test_engine_a_and_b_agree_on_single_txn_scope():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -100_000, "Consulting advisory fee"),
        _txn("TXN-X1-0002", "2025-03-01", -260_001, "Consulting advisory fee"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.3", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=260_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(
            include_categories=(Category.PROFESSIONAL_SERVICES,), sign=Sign.OUTFLOW, scope=Scope.SINGLE_TXN
        ),
    )
    result = reconcile_actual(rule, txns, statement=None)
    assert result.status == ValidationStatus.OK
    assert result.actual == 260_001.0


def test_both_none_is_unresolved():
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.RATIO, operator=Operator.LE,
        threshold=9.0, threshold_unit="x", period_start="2025-01-01", period_end="2025-12-31",
        numerator=None, denominator=None,
    )
    result = reconcile_actual(rule, [], statement=None)
    assert result.status == ValidationStatus.UNRESOLVED
    assert result.actual is None


def test_engine_a_and_b_agree_on_revenue_ledger_fallback():
    """Engine A and Verifier B each independently implement the
    no-statement-document ledger fallback for revenue/operating_expenses
    (calc/engine_a.py, verify/engine_b.py) -- they must still agree.
    """
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", 5_000_000, "Volume rebate received"),
        _txn("TXN-X1-0002", "2025-03-01", -300_000, "Payroll for staff"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.2", metric_kind=MetricKind.AMOUNT, operator=Operator.GE,
        threshold=1_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=StatementFigureSource(field_name="revenue"),
    )
    result = reconcile_actual(rule, txns, statement=None)
    assert result.status == ValidationStatus.OK
    assert result.actual == 5_000_000.0
