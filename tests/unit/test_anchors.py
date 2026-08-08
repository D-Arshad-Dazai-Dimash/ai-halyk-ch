from __future__ import annotations

from covenant_agent.ingestion.anchors import count_date_occurrences, extract_anchors


def test_extract_account_ids():
    a = extract_anchors("Заёмщик ACC-7805 обязуется... счёт ACC-7805 и ACC-1234")
    assert a.account_ids == ("ACC-1234", "ACC-7805")


def test_extract_iso_dates():
    a = extract_anchors("период с 2025-01-01 по 2025-12-31, подписано 2024-11-30")
    assert set(a.dates) == {"2025-01-01", "2025-12-31", "2024-11-30"}


def test_extract_dollar_amounts():
    a = extract_anchors("не более $7,500,000.00 либо $260,000.00 в отдельном платеже")
    assert set(a.dollar_amounts) == {7_500_000.00, 260_000.00}


def test_extract_ratio_multiples():
    a = extract_anchors("не превышало 9.00x, ранее было 4.5X")
    assert set(a.ratio_multiples) == {9.00, 4.5}


def test_clause_markers_exclude_larger_numbers():
    a = extract_anchors("Статья 6.1, Статья 6.2, Статья 6.3, но также пункт 16.12 и 6.10")
    assert {"6.1", "6.2", "6.3"}.issubset(set(a.clause_markers))
    assert "16.12" not in a.clause_markers  # excluded by digit-boundary lookaround
    assert "6.10" not in a.clause_markers


def test_has_all_six_point_clauses_true_only_when_all_three_present():
    full = extract_anchors("6.1 ... 6.2 ... 6.3 ...")
    partial = extract_anchors("6.1 ... 6.2 ...")
    assert full.has_all_six_point_clauses is True
    assert partial.has_all_six_point_clauses is False


def test_ebitda_and_kyc_keyword_flags():
    a = extract_anchors("расчёт EBITDA заёмщика")
    assert a.has_ebitda_keyword is True
    assert a.has_kyc_keyword is False

    b = extract_anchors("Досье KYC клиента")
    assert b.has_kyc_keyword is True
    assert b.has_ebitda_keyword is False


def test_count_date_occurrences_repeats():
    text = "с 2025-01-01 по 2025-12-31; снова 2025-01-01 по 2025-12-31; подписано 2025-03-15"
    counts = count_date_occurrences(text)
    assert counts["2025-01-01"] == 2
    assert counts["2025-12-31"] == 2
    assert counts["2025-03-15"] == 1
