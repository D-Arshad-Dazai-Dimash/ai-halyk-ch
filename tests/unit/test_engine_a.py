from __future__ import annotations

import pytest

from covenant_agent.common.enums import CovenantStatus, MetricKind, Operator, Scope, Sign
from covenant_agent.covenants.dsl import CovenantRule, LedgerAggregateSource, StatementFigureSource
from covenant_agent.covenants.financial_statement import FinancialStatementExtract, OneOffItem
from covenant_agent.calc.engine_a import compute_actual, evaluate_status
from covenant_agent.ledger.categories import Category
from covenant_agent.ledger.model import RawTransaction
from covenant_agent.ledger.normalize import normalize_transaction


def _txn(txn_id, date, amount, description, currency="USD"):
    raw = RawTransaction(
        txn_id=txn_id,
        date=date,
        account_id="ACC-1001",
        counterparty="Acme",
        description=description,
        amount=amount,
        currency=currency,
    )
    return normalize_transaction(raw, eur_to_usd_rate=1.10, use_llm_fallback=False)


def test_amount_aggregate_covenant_breach():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -1_000_000, "Purchase of equipment"),  # capex
        _txn("TXN-X1-0002", "2025-03-01", -2_500_000, "Purchase of equipment"),  # capex
        _txn("TXN-X1-0003", "2025-04-01", -500_000, "Payroll for staff"),  # not capex
    ]
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.1",
        metric_kind=MetricKind.AMOUNT,
        operator=Operator.LE,
        threshold=3_000_000.0,
        threshold_unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        amount_source=LedgerAggregateSource(
            include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW, scope=Scope.AGGREGATE
        ),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(3_500_000.0)
    assert evaluate_status(rule, actual) == CovenantStatus.BREACH


def test_amount_aggregate_covenant_compliant():
    txns = [_txn("TXN-X1-0001", "2025-02-01", -1_000_000, "Purchase of equipment")]
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.1",
        metric_kind=MetricKind.AMOUNT,
        operator=Operator.LE,
        threshold=3_000_000.0,
        threshold_unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(1_000_000.0)
    assert evaluate_status(rule, actual) == CovenantStatus.COMPLIANT


def test_single_txn_scope_takes_max_not_sum():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -100_000, "Consulting advisory fee"),
        _txn("TXN-X1-0002", "2025-03-01", -260_001, "Consulting advisory fee"),
    ]
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.3",
        metric_kind=MetricKind.AMOUNT,
        operator=Operator.LE,
        threshold=260_000.0,
        threshold_unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        amount_source=LedgerAggregateSource(
            include_categories=(Category.PROFESSIONAL_SERVICES,),
            sign=Sign.OUTFLOW,
            scope=Scope.SINGLE_TXN,
        ),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(260_001.0)  # max, not sum (310,001)
    assert evaluate_status(rule, actual) == CovenantStatus.BREACH


def test_period_boundary_excludes_out_of_range_transactions():
    txns = [
        _txn("TXN-X1-0001", "2024-12-31", -5_000_000, "Purchase of equipment"),  # before period
        _txn("TXN-X1-0002", "2025-06-01", -1_000_000, "Purchase of equipment"),  # in period
    ]
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.1",
        metric_kind=MetricKind.AMOUNT,
        operator=Operator.LE,
        threshold=3_000_000.0,
        threshold_unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(1_000_000.0)


def test_ratio_covenant_from_statement_figures():
    statement = FinancialStatementExtract(
        scenario_id="X1", revenue=10_000_000.0, operating_expenses=6_000_000.0, capex=36_000_000.0
    )
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.1",
        metric_kind=MetricKind.RATIO,
        operator=Operator.LE,
        threshold=9.0,
        threshold_unit="x",
        period_start="2025-01-01",
        period_end="2025-12-31",
        numerator=StatementFigureSource(field_name="capex"),
        denominator=StatementFigureSource(field_name="ebitda"),
    )
    actual = compute_actual(rule, transactions=[], statement=statement)
    assert actual == pytest.approx(9.0)  # 36M / (10M-6M) = 9.0x
    assert evaluate_status(rule, actual) == CovenantStatus.COMPLIANT  # <= 9.00x


def test_ebitda_adjusted_includes_material_one_off_addbacks():
    statement = FinancialStatementExtract(
        scenario_id="X1",
        revenue=10_000_000.0,
        operating_expenses=6_000_000.0,
        one_off_materiality_threshold=300_000.0,
        one_off_items=(
            OneOffItem(description="flood damage", amount=500_000.0),
            OneOffItem(description="minor item", amount=50_000.0),  # below threshold, excluded
        ),
    )
    assert statement.ebitda_base == pytest.approx(4_000_000.0)
    assert statement.ebitda_adjusted == pytest.approx(4_500_000.0)  # only the >=300k item added back


def test_ratio_covenant_returns_none_when_statement_missing():
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.1",
        metric_kind=MetricKind.RATIO,
        operator=Operator.LE,
        threshold=9.0,
        threshold_unit="x",
        period_start="2025-01-01",
        period_end="2025-12-31",
        numerator=StatementFigureSource(field_name="capex"),
        denominator=StatementFigureSource(field_name="ebitda"),
    )
    assert compute_actual(rule, transactions=[], statement=None) is None


def test_eur_transaction_converted_before_aggregation():
    txns = [_txn("TXN-X1-0001", "2025-02-01", -1_000_000, "Purchase of equipment", currency="EUR")]
    rule = CovenantRule(
        scenario_id="X1",
        clause="6.1",
        metric_kind=MetricKind.AMOUNT,
        operator=Operator.LE,
        threshold=3_000_000.0,
        threshold_unit="USD",
        period_start="2025-01-01",
        period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(1_100_000.0)  # 1,000,000 * 1.10 rate used in _txn helper


def test_revenue_statement_field_falls_back_to_ledger_when_no_statement():
    txns = [
        _txn("TXN-X1-0001", "2025-03-01", 2_000_000, "Generating capacity availability sales settlement"),
        _txn("TXN-X1-0002", "2025-04-01", 500_000, "Volume rebate received"),
        _txn("TXN-X1-0003", "2025-05-01", -100_000, "Payroll for staff"),  # not revenue, must be excluded
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.2", metric_kind=MetricKind.AMOUNT, operator=Operator.GE,
        threshold=2_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=StatementFigureSource(field_name="revenue"),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(2_500_000.0)
    assert evaluate_status(rule, actual) == CovenantStatus.COMPLIANT


def test_statement_value_takes_priority_over_ledger_fallback_when_present():
    statement = FinancialStatementExtract(scenario_id="X1", revenue=9_000_000.0)
    txns = [_txn("TXN-X1-0001", "2025-03-01", 2_000_000, "Volume rebate received")]
    rule = CovenantRule(
        scenario_id="X1", clause="6.2", metric_kind=MetricKind.AMOUNT, operator=Operator.GE,
        threshold=2_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=StatementFigureSource(field_name="revenue"),
    )
    actual = compute_actual(rule, txns, statement=statement)
    assert actual == pytest.approx(9_000_000.0)  # statement wins, ledger fallback not used


def test_operating_expenses_statement_field_falls_back_to_ledger():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", 5_000_000, "Volume rebate received"),  # revenue
        _txn("TXN-X1-0002", "2025-03-01", -300_000, "Payroll for staff"),  # opex
        _txn("TXN-X1-0003", "2025-04-01", -200_000, "Corporate income tax instalment"),  # opex
        _txn("TXN-X1-0004", "2025-05-01", -1_000_000, "Purchase of equipment"),  # capex, excluded from opex
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.RATIO, operator=Operator.LE,
        threshold=10.0, threshold_unit="x", period_start="2025-01-01", period_end="2025-12-31",
        numerator=StatementFigureSource(field_name="revenue"),
        denominator=StatementFigureSource(field_name="operating_expenses"),
    )
    actual = compute_actual(rule, txns, statement=None)
    assert actual == pytest.approx(10.0)  # 5,000,000 / (300,000 + 200,000), capex excluded from opex


def test_capex_and_ebitda_statement_fields_have_no_ledger_fallback():
    """Unlike revenue/operating_expenses, capex/ebitda have no well-defined
    ledger-only equivalent (capex needs Group-consolidated figures; EBITDA
    needs the base + materiality-gated addbacks) -- must stay unresolved
    rather than silently approximate.
    """
    txns = [_txn("TXN-X1-0001", "2025-03-01", -1_000_000, "Purchase of equipment")]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=5_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=StatementFigureSource(field_name="capex"),
    )
    assert compute_actual(rule, txns, statement=None) is None
