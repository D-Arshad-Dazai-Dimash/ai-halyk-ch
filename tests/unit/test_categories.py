from __future__ import annotations

from covenant_agent.ledger.categories import Category, classify_by_rules


def test_payroll():
    assert classify_by_rules("Payroll for administrative staff — Kostanay centre, June") == Category.PAYROLL


def test_interest():
    assert classify_by_rules("Interest on finance sublease — Karaganda yard, August") == Category.INTEREST


def test_taxes():
    assert classify_by_rules("Corporate income tax instalment — Kostanay centre") == Category.TAXES


def test_insurance():
    assert classify_by_rules("Environmental liability insurance") == Category.INSURANCE


def test_utilities():
    assert classify_by_rules("Electricity metering correction credit — Taraz site") in (
        Category.UTILITIES,
        Category.REFUNDS_CREDITS,
    )  # "credit" keyword also present; whichever priority wins, must not be UNCATEGORIZED


def test_capex():
    assert classify_by_rules("Purchase of switchyard equipment") == Category.CAPEX


def test_marketing():
    assert classify_by_rules("Digital media buy — display — Kyzylorda station") == Category.MARKETING


def test_rent_lease():
    assert classify_by_rules("Equipment yard lease — Ekibastuz block B, instalment 3") == Category.RENT_LEASE


def test_refunds_credits():
    assert classify_by_rules("Rent overpayment refunded — Karaganda yard") in (
        Category.REFUNDS_CREDITS,
        Category.RENT_LEASE,
    )


def test_unrecognized_falls_to_uncategorized():
    assert classify_by_rules("Xyzzy unrelated gibberish description qwqw") == Category.UNCATEGORIZED
