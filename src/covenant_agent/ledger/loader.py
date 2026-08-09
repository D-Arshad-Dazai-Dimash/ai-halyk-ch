"""Deterministic ledger CSV loading + account<->scenario map derivation.

Everything here is pure function-of-the-ledger: no scenario IDs, account IDs,
or filenames are hardcoded (PLAN.md anti-overfitting rule). The account<->
scenario relationship and the ledger's reporting year are *computed* from
whatever ledger is handed in, so this generalizes unchanged to a holdout
ledger with different scenario names or a different year.
"""

from __future__ import annotations

import csv
import collections
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from covenant_agent.common.exceptions import CovenantAgentError
from covenant_agent.ledger.model import RawTransaction


def _parse_amount(raw: str) -> Decimal | None:
    """Blank -> None (unresolved, to be supplied by an authoritative
    document); malformed non-blank -> ValueError, which drops the row.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"malformed amount {raw!r}") from exc


def load_ledger(
    csv_path: Path, skipped_out: list[str] | None = None
) -> list[RawTransaction]:
    """Parse the ledger CSV into validated RawTransaction rows.

    A blank amount is preserved as an unresolved row (``amount=None``), not
    dropped: the corpus omits amounts that are stated only in a treasury
    memo or the audited notes, and dropping such a row would understate the
    covenant metric that those documents require it to be part of.

    Rows that fail validation for any other reason (malformed txn_id,
    non-numeric amount) are skipped rather than crashing the whole load --
    a single dirty row must not take down the pipeline (ARCHITECTURE.md
    failure boundaries). Skipping is reported through ``skipped_out``, not
    swallowed: a whole borrower can share one unexpected id shape, and
    silently dropping their rows understates every metric they appear in
    while the run still looks clean.
    """
    if not csv_path.is_file():
        raise CovenantAgentError(f"ledger CSV not found: {csv_path}")

    transactions: list[RawTransaction] = []
    skipped: list[tuple[int, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):  # header is row 1
            try:
                transactions.append(
                    RawTransaction(
                        txn_id=row["txn_id"].strip(),
                        date=row["date"].strip(),
                        account_id=row["account_id"].strip(),
                        counterparty=row["counterparty"],
                        description=row["description"],
                        amount=_parse_amount(row["amount"]),
                        currency=row["currency"].strip(),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                skipped.append((i, str(exc)))

    if skipped_out is not None:
        skipped_out.extend(f"row {line}: {reason}" for line, reason in skipped)

    if not transactions:
        raise CovenantAgentError(f"no valid transactions parsed from {csv_path}")

    return transactions


def account_prefixes(transactions: list[RawTransaction]) -> tuple[str, ...]:
    """The identifier prefixes the ledger's accounts actually use.

    Documents name accounts in the ledger's own vocabulary, so the ledger --
    not a guess at the shape -- decides what counts as an account reference.
    """
    return tuple(
        sorted({txn.account_id.split("-")[0] for txn in transactions if "-" in txn.account_id})
    )


def build_account_scenario_map(transactions: list[RawTransaction]) -> dict[str, str]:
    """account_id -> scenario_id, derived purely from txn_id prefixes.

    The dataset guarantees each account_id maps to exactly one scenario_id
    (CASE.md section 3). We verify that invariant rather than assume it --
    a violation is a data-quality signal worth surfacing, not silently
    ignoring.
    """
    candidates: dict[str, set[str]] = collections.defaultdict(set)
    for txn in transactions:
        candidates[txn.account_id].add(txn.scenario_id)

    mapping: dict[str, str] = {}
    conflicts: dict[str, set[str]] = {}
    for account_id, scenario_ids in candidates.items():
        if len(scenario_ids) == 1:
            mapping[account_id] = next(iter(scenario_ids))
        else:
            conflicts[account_id] = scenario_ids

    if conflicts:
        raise CovenantAgentError(
            f"account_id -> scenario_id is not 1:1 for {len(conflicts)} accounts: {conflicts}"
        )
    return mapping


def group_by_scenario(transactions: list[RawTransaction]) -> dict[str, list[RawTransaction]]:
    grouped: dict[str, list[RawTransaction]] = collections.defaultdict(list)
    for txn in transactions:
        grouped[txn.scenario_id].append(txn)
    return dict(grouped)


def ledger_reporting_year(transactions: list[RawTransaction]) -> int:
    """The ledger's reporting year, as the modal year among transaction
    dates -- robust to a handful of stray/boundary-date rows, and never
    hardcoded, so a holdout ledger from a different year resolves correctly.
    """
    years = Counter(int(txn.date[:4]) for txn in transactions if len(txn.date) >= 4)
    if not years:
        raise CovenantAgentError("could not determine ledger reporting year: no valid dates")
    return years.most_common(1)[0][0]
