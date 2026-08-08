"""Golden regression test: production resolver output vs. a fixture pinned
to the real public dataset (concrete IDs/filenames belong here, never in
src/ -- see tests/unit/test_no_gt_leakage.py). Regenerate the fixture with
`python calibration/inspect_resolve.py --write-fixture` if the resolver's
logic changes intentionally; a diff here should otherwise mean a regression.

Skips cleanly if the dataset isn't present on this machine (e.g. CI).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from covenant_agent.common.config import DOCUMENTS_DIR, LEDGER_CSV
from covenant_agent.ledger.loader import (
    build_account_scenario_map,
    ledger_reporting_year,
    load_ledger,
)
from covenant_agent.resolve.contract_resolver import resolve_effective_contracts

FIXTURE_PATH = Path(__file__).parent / "scenario_map.json"


@pytest.fixture(scope="module")
def resolved():
    if not (DOCUMENTS_DIR.is_dir() and LEDGER_CSV.is_file()):
        pytest.skip(f"dataset not found (expected under {DOCUMENTS_DIR.parent})")
    transactions = load_ledger(LEDGER_CSV)
    account_to_scenario = build_account_scenario_map(transactions)
    year = ledger_reporting_year(transactions)
    return resolve_effective_contracts(DOCUMENTS_DIR, account_to_scenario, year)


def test_fixture_exists():
    assert FIXTURE_PATH.is_file(), "run `python calibration/inspect_resolve.py --write-fixture`"


def test_resolves_exactly_the_expected_scenarios(resolved):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert set(resolved) == set(fixture), (
        f"resolved scenarios {sorted(resolved)} != fixture scenarios {sorted(fixture)}"
    )


@pytest.mark.parametrize(
    "scenario_id",
    json.loads(FIXTURE_PATH.read_text(encoding="utf-8")).keys() if FIXTURE_PATH.is_file() else [],
)
def test_scenario_matches_fixture(resolved, scenario_id):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    expected = fixture[scenario_id]
    actual = resolved[scenario_id]

    assert actual.account_id == expected["account_id"]
    assert actual.effective.path.name == expected["effective_contract_filename"]
    assert actual.effective.content_hash == expected["effective_contract_hash"]
    assert actual.effective.period_start == expected["period_start"]
    assert actual.effective.period_end == expected["period_end"]
    assert sorted(c.path.name for c in actual.superseded) == sorted(
        expected["superseded_filenames"]
    )


def test_no_decoy_9xxx_accounts_resolved(resolved):
    """The ~549 decoy borrowers use numeric-only scenario prefixes (9xxx);
    none should ever resolve to an effective contract, since they have no
    corresponding credit-agreement account_id relationship intended by the
    challenge design. This guards against the resolver becoming too lax.
    """
    decoy_like = [s for s in resolved if s.isdigit()]
    assert decoy_like == []
