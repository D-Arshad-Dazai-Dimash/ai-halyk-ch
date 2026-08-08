from __future__ import annotations

from covenant_agent.common.enums import CovenantStatus, MetricKind, Operator, Scope, Sign
from covenant_agent.covenants.dsl import CovenantRule, LedgerAggregateSource, StatementFigureSource
from covenant_agent.covenants.financial_statement import FinancialStatementExtract
from covenant_agent.calc.engine_a import compute_actual, evaluate_status
from covenant_agent.evidence.counterfactual import find_evidence
from covenant_agent.ledger.categories import Category
from covenant_agent.ledger.model import RawTransaction
from covenant_agent.ledger.normalize import normalize_transaction


def _txn(txn_id, date, amount, description):
    raw = RawTransaction(
        txn_id=txn_id, date=date, account_id="ACC-1001", counterparty="Acme",
        description=description, amount=amount, currency="USD",
    )
    return normalize_transaction(raw, eur_to_usd_rate=1.08, use_llm_fallback=False)


def _run(rule, txns, statement=None):
    actual = compute_actual(rule, txns, statement)
    status = evaluate_status(rule, actual)
    evidence = find_evidence(rule, txns, statement, actual, status)
    return actual, status, evidence


def test_single_payment_cap_evidence_is_the_over_cap_payment():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -100_000, "Consulting advisory fee"),
        _txn("TXN-X1-0002", "2025-05-01", -273_418.66, "Consulting advisory fee"),  # the breach
        _txn("TXN-X1-0003", "2025-08-01", -150_000, "Consulting advisory fee"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.3", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=260_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(
            include_categories=(Category.PROFESSIONAL_SERVICES,), sign=Sign.OUTFLOW, scope=Scope.SINGLE_TXN
        ),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, txns)
    assert status == CovenantStatus.BREACH
    assert evidence == "TXN-X1-0002"


def test_single_payment_cap_no_evidence_when_compliant_max_below_cap():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -100_000, "Consulting advisory fee"),
        _txn("TXN-X1-0002", "2025-05-01", -150_000, "Consulting advisory fee"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.3", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=260_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(
            include_categories=(Category.PROFESSIONAL_SERVICES,), sign=Sign.OUTFLOW, scope=Scope.SINGLE_TXN
        ),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, txns)
    assert status == CovenantStatus.COMPLIANT
    assert evidence is None


def test_single_payment_tie_yields_no_evidence():
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -300_000, "Consulting advisory fee"),
        _txn("TXN-X1-0002", "2025-05-01", -300_000, "Consulting advisory fee"),  # exact tie for max
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.3", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=260_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(
            include_categories=(Category.PROFESSIONAL_SERVICES,), sign=Sign.OUTFLOW, scope=Scope.SINGLE_TXN
        ),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, txns)
    assert status == CovenantStatus.BREACH
    assert evidence is None  # neither txn alone is THE determinant -- removing either still leaves a breach


def test_aggregate_cap_evidence_is_the_unique_flipping_transaction():
    # Cap 1,000,000. Three small base transactions (50,000 each = 150,000,
    # each individually smaller than the eventual excess) plus one large
    # transaction that alone tips the aggregate over the cap. Removing any
    # single base transaction leaves the total still over cap (no flip);
    # removing the large one brings it back under cap (flip) -- so it is
    # the unique causal determinant, unlike the razor-thin-margin case
    # above where multiple transactions are each individually decisive.
    txns = [
        _txn("TXN-X1-0001", "2025-01-15", -50_000, "Purchase of equipment"),
        _txn("TXN-X1-0002", "2025-02-15", -50_000, "Purchase of equipment"),
        _txn("TXN-X1-0003", "2025-03-15", -50_000, "Purchase of equipment"),
        _txn("TXN-X1-0004", "2025-05-01", -900_001, "Purchase of equipment"),  # tips it over the cap
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=1_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, txns)
    assert actual == 1_050_001.0
    assert status == CovenantStatus.BREACH
    assert evidence == "TXN-X1-0004"


def test_aggregate_cap_no_evidence_when_far_from_threshold():
    """A transaction that merely *contributes* to a well-compliant (or
    well-breached) aggregate is not evidence -- removing it doesn't flip
    the verdict either way.
    """
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", -500_000, "Purchase of equipment"),
        _txn("TXN-X1-0002", "2025-05-01", -600_000, "Purchase of equipment"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=10_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, txns)
    assert status == CovenantStatus.COMPLIANT
    assert evidence is None


def test_evidence_applicable_false_short_circuits_to_none():
    txns = [_txn("TXN-X1-0001", "2025-02-01", -5_000_000, "Purchase of equipment")]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.AMOUNT, operator=Operator.LE,
        threshold=1.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=LedgerAggregateSource(include_categories=(Category.CAPEX,), sign=Sign.OUTFLOW),
        evidence_applicable=False,
    )
    actual, status, evidence = _run(rule, txns)
    assert status == CovenantStatus.BREACH
    assert evidence is None


def test_statement_sourced_amount_has_no_ledger_evidence():
    statement = FinancialStatementExtract(scenario_id="X1", revenue=1_000_000.0)
    rule = CovenantRule(
        scenario_id="X1", clause="6.2", metric_kind=MetricKind.AMOUNT, operator=Operator.GE,
        threshold=2_000_000.0, threshold_unit="USD", period_start="2025-01-01", period_end="2025-12-31",
        amount_source=StatementFigureSource(field_name="revenue"),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, [], statement)
    assert status == CovenantStatus.BREACH
    assert evidence is None


def test_ratio_covenant_evidence_from_ledger_denominator_side():
    # Threshold 9.00x; numerator fixed from statement (capex=36M), denominator
    # is a ledger-aggregate category summed from two transactions. Baseline
    # ratio 36M/4.5M=8.0x is compliant. Removing the 4.0M transaction leaves
    # 0.5M -> 72.0x, flipping to BREACH; removing the 0.5M transaction leaves
    # 4.0M -> exactly 9.0x, which is still <= 9.0x (no flip). So only the
    # 4.0M transaction is the unique causal determinant.
    statement = FinancialStatementExtract(scenario_id="X1", capex=36_000_000.0)
    txns = [
        _txn("TXN-X1-0001", "2025-02-01", 4_000_000, "Rebate received"),
        _txn("TXN-X1-0002", "2025-03-01", 500_000, "Rebate received"),
    ]
    rule = CovenantRule(
        scenario_id="X1", clause="6.1", metric_kind=MetricKind.RATIO, operator=Operator.LE,
        threshold=9.0, threshold_unit="x", period_start="2025-01-01", period_end="2025-12-31",
        numerator=StatementFigureSource(field_name="capex"),
        denominator=LedgerAggregateSource(
            include_categories=(Category.REBATES_RECEIVED,), sign=Sign.INFLOW, scope=Scope.AGGREGATE
        ),
        evidence_applicable=True,
    )
    actual, status, evidence = _run(rule, txns, statement)
    assert actual == 8.0  # 36M / 4.5M
    assert status == CovenantStatus.COMPLIANT
    assert evidence == "TXN-X1-0001"
