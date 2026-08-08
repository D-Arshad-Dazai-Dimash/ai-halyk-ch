"""Unit tests for ledger/loader.py using small synthetic fixtures (never the
real dataset -- that's exercised separately by golden/integration tests).
"""

from __future__ import annotations

import pytest

from covenant_agent.common.exceptions import CovenantAgentError
from covenant_agent.ledger.loader import (
    build_account_scenario_map,
    group_by_scenario,
    ledger_reporting_year,
    load_ledger,
)

CSV_HEADER = "txn_id,date,account_id,counterparty,description,amount,currency\n"


def _write_csv(tmp_path, rows: str):
    path = tmp_path / "ledger.csv"
    path.write_text(CSV_HEADER + rows, encoding="utf-8")
    return path


def test_load_ledger_parses_valid_rows(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        "TXN-X1-0001,2025-01-01,ACC-1001,Acme,Widget purchase,-100.50,USD\n"
        "TXN-X1-0002,2025-02-01,ACC-1001,Acme,Refund,50.25,EUR\n",
    )
    txns = load_ledger(csv_path)
    assert len(txns) == 2
    assert txns[0].scenario_id == "X1"
    assert txns[0].amount == -100.50
    assert txns[1].currency == "EUR"


def test_load_ledger_skips_malformed_rows_without_crashing(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        "TXN-X1-0001,2025-01-01,ACC-1001,Acme,Widget purchase,-100.50,USD\n"
        "NOT-A-VALID-TXN-ID,2025-01-01,ACC-1001,Acme,Bad row,not-a-number,USD\n"
        "TXN-X1-0002,2025-02-01,ACC-1001,Acme,Refund,50.25,EUR\n",
    )
    txns = load_ledger(csv_path)
    assert len(txns) == 2  # malformed row silently dropped, not crashing


def test_load_ledger_raises_on_missing_file(tmp_path):
    with pytest.raises(CovenantAgentError):
        load_ledger(tmp_path / "does_not_exist.csv")


def test_build_account_scenario_map_is_1to1(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        "TXN-X1-0001,2025-01-01,ACC-1001,Acme,A,-100,USD\n"
        "TXN-X1-0002,2025-01-02,ACC-1001,Acme,B,-50,USD\n"
        "TXN-X2-0001,2025-01-03,ACC-2002,Beta,C,-10,USD\n",
    )
    txns = load_ledger(csv_path)
    mapping = build_account_scenario_map(txns)
    assert mapping == {"ACC-1001": "X1", "ACC-2002": "X2"}


def test_build_account_scenario_map_raises_on_conflict(tmp_path):
    # ACC-1001 appears under two different scenario prefixes -- violates the
    # dataset's documented 1:1 invariant; must be surfaced, not silently resolved.
    csv_path = _write_csv(
        tmp_path,
        "TXN-X1-0001,2025-01-01,ACC-1001,Acme,A,-100,USD\n"
        "TXN-X2-0001,2025-01-02,ACC-1001,Acme,B,-50,USD\n",
    )
    txns = load_ledger(csv_path)
    with pytest.raises(CovenantAgentError):
        build_account_scenario_map(txns)


def test_group_by_scenario(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        "TXN-X1-0001,2025-01-01,ACC-1001,Acme,A,-100,USD\n"
        "TXN-X2-0001,2025-01-02,ACC-2002,Beta,B,-50,USD\n"
        "TXN-X1-0002,2025-01-03,ACC-1001,Acme,C,-10,USD\n",
    )
    grouped = group_by_scenario(load_ledger(csv_path))
    assert set(grouped) == {"X1", "X2"}
    assert len(grouped["X1"]) == 2
    assert len(grouped["X2"]) == 1


def test_ledger_reporting_year_uses_mode(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        "TXN-X1-0001,2025-01-01,ACC-1001,Acme,A,-100,USD\n"
        "TXN-X1-0002,2025-06-01,ACC-1001,Acme,B,-50,USD\n"
        "TXN-X1-0003,2024-12-31,ACC-1001,Acme,C,-10,USD\n",  # stray boundary row
    )
    year = ledger_reporting_year(load_ledger(csv_path))
    assert year == 2025
