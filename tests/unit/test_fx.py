from __future__ import annotations

import pytest

from covenant_agent.ledger.fx import (
    convert_to_usd,
    find_explicit_rate_in_text,
    resolve_eur_to_usd_rate,
)


def test_convert_usd_is_identity():
    assert convert_to_usd(100.0, "USD", 1.08) == 100.0


def test_convert_eur_applies_rate():
    assert convert_to_usd(100.0, "EUR", 1.10) == pytest.approx(110.0)


def test_convert_unsupported_currency_raises():
    with pytest.raises(ValueError):
        convert_to_usd(100.0, "KZT", 1.08)


def test_find_explicit_rate_pattern_1eur():
    assert find_explicit_rate_in_text("Курс на дату сделки: 1 EUR = 1.0950 USD") == pytest.approx(1.0950)


def test_find_explicit_rate_slash_pattern():
    assert find_explicit_rate_in_text("Reference rate EUR/USD 1.0800 applied") == pytest.approx(1.08)


def test_find_explicit_rate_returns_none_when_absent():
    assert find_explicit_rate_in_text("No exchange rate mentioned here at all.") is None


def test_resolve_prefers_document_rate_over_default():
    texts = ["irrelevant", "1 EUR = 1.2000 USD", "irrelevant again"]
    assert resolve_eur_to_usd_rate(texts, default=1.08) == pytest.approx(1.20)


def test_resolve_falls_back_to_default_when_no_document_states_a_rate():
    texts = ["nothing here", "nor here"]
    assert resolve_eur_to_usd_rate(texts, default=1.08) == pytest.approx(1.08)
