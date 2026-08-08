from __future__ import annotations

from covenant_agent.ledger.categories import Category
from covenant_agent.ledger.model import RawTransaction
from covenant_agent.ledger.normalize import normalize_transaction


def _raw(amount: float, currency: str, description: str) -> RawTransaction:
    return RawTransaction(
        txn_id="TXN-X1-0001",
        date="2025-01-01",
        account_id="ACC-1001",
        counterparty="Acme",
        description=description,
        amount=amount,
        currency=currency,
    )


def test_outflow_negative_amount_is_flagged_and_abs_computed():
    n = normalize_transaction(_raw(-1500.0, "USD", "Payroll for staff"), eur_to_usd_rate=1.08)
    assert n.is_outflow is True
    assert n.amount_usd == -1500.0
    assert n.amount_usd_abs == 1500.0
    assert n.category == Category.PAYROLL
    assert n.category_from_llm is False


def test_inflow_positive_amount_is_flagged():
    n = normalize_transaction(_raw(2000.0, "USD", "Rebate received"), eur_to_usd_rate=1.08)
    assert n.is_outflow is False
    assert n.amount_usd == 2000.0
    assert n.amount_usd_abs == 2000.0


def test_eur_amount_is_converted_before_abs():
    n = normalize_transaction(_raw(-1000.0, "EUR", "Interest payment"), eur_to_usd_rate=1.10)
    assert n.amount_usd == -1100.0
    assert n.amount_usd_abs == 1100.0
    assert n.currency == "EUR"  # original currency preserved for audit
    assert n.amount_raw == -1000.0  # original amount preserved for audit


def test_uncategorized_without_llm_fallback_stays_uncategorized():
    n = normalize_transaction(
        _raw(-100.0, "USD", "Xyzzy unrelated gibberish"),
        eur_to_usd_rate=1.08,
        use_llm_fallback=False,
    )
    assert n.category == Category.UNCATEGORIZED
    assert n.category_from_llm is False
