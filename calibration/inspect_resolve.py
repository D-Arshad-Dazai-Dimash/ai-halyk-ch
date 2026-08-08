"""Developer-time inspection harness (P1). Runs the real production resolver
(ledger loader + contract resolver -- both GT-independent) over the actual
dataset and prints/saves the resulting scenario -> effective-contract map.

This script does NOT read ground_truth.json. Its output is used to populate
tests/golden/scenario_map.json, a fixture that pins concrete public-dataset
values for regression testing -- never imported by src/.

Usage: python calibration/inspect_resolve.py [--write-fixture]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from covenant_agent.common.config import DOCUMENTS_DIR, LEDGER_CSV  # noqa: E402
from covenant_agent.ledger.loader import (  # noqa: E402
    build_account_scenario_map,
    ledger_reporting_year,
    load_ledger,
)
from covenant_agent.resolve.contract_resolver import resolve_effective_contracts  # noqa: E402


def main() -> None:
    transactions = load_ledger(LEDGER_CSV)
    account_to_scenario = build_account_scenario_map(transactions)
    year = ledger_reporting_year(transactions)
    print(f"Ledger reporting year: {year}")
    print(f"Accounts in ledger: {len(account_to_scenario)}")

    resolved = resolve_effective_contracts(DOCUMENTS_DIR, account_to_scenario, year)
    print(f"Resolved effective contracts: {len(resolved)}\n")

    fixture: dict[str, dict] = {}
    for scenario_id in sorted(resolved):
        r = resolved[scenario_id]
        print(
            f"{scenario_id:5s} account={r.account_id:10s} "
            f"effective={r.effective.path.name:20s} "
            f"period={r.effective.period_start}..{r.effective.period_end} "
            f"superseded={[c.path.name for c in r.superseded]}"
        )
        fixture[scenario_id] = {
            "account_id": r.account_id,
            "effective_contract_filename": r.effective.path.name,
            "effective_contract_hash": r.effective.content_hash,
            "period_start": r.effective.period_start,
            "period_end": r.effective.period_end,
            "superseded_filenames": [c.path.name for c in r.superseded],
        }

    # scenarios present in ledger but NOT resolved (worth knowing about)
    unresolved = sorted(set(account_to_scenario.values()) - set(resolved))
    if unresolved:
        print(f"\nUNRESOLVED scenarios (no unambiguous effective contract found): {unresolved}")

    if "--write-fixture" in sys.argv:
        out_path = Path(__file__).resolve().parents[1] / "tests" / "golden" / "scenario_map.json"
        out_path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote fixture: {out_path}")


if __name__ == "__main__":
    main()
