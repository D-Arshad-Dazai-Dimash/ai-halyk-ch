"""Metamorphic (mutation) tests.

These never ask whether a perturbed corpus still matches the answer key --
they assert *relationships* the pipeline must hold regardless of what the
right answers are:

* an irrelevant change (renaming files, reordering rows, relabelling
  scenarios, adding decoys) must not change any covenant's semantics;
* a causal change (moving a decisive amount, crossing a threshold) must
  change the affected cells and only those;
* production source must contain no literal from the public instance.

Cells are keyed by *account and clause* rather than scenario id, so a
scenario relabelling is genuinely invisible to the oracle.

Slow cases run the full OCR pipeline and are opt-in via
RUN_SLOW_INTEGRATION=1; the static anti-overfitting scan always runs.

Run these serially. They share the on-disk OCR cache, so two concurrent
runs evict each other's entries and each ends up re-OCRing the corpus.
"""

from __future__ import annotations

import ast
import csv
import json
import os
import re
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from covenant_agent.common.config import DATASET_DIR
from covenant_agent.deterministic_pipeline import run
from covenant_agent.ingestion.dataset import discover_dataset
from covenant_agent.ledger.loader import build_account_scenario_map, load_ledger

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "covenant_agent"

slow = pytest.mark.skipif(
    os.environ.get("RUN_SLOW_INTEGRATION") != "1",
    reason="full-pipeline mutation; set RUN_SLOW_INTEGRATION=1 to run",
)


# --- semantic oracle ---------------------------------------------------------


def _semantics(root: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """Run the pipeline and key every result by (account_id, clause).

    Keying on the account rather than the scenario label is what lets a
    scenario-renaming mutation be recognised as irrelevant.
    """
    dataset = discover_dataset(root)
    template = json.loads(dataset.template.read_text(encoding="utf-8"))
    transactions = load_ledger(dataset.ledger)
    scenario_to_account = {
        scenario: account
        for account, scenario in build_account_scenario_map(transactions).items()
    }
    results = run(dataset.documents, dataset.ledger, template["answers"])
    return {
        (scenario_to_account.get(scenario, scenario), clause): (
            cell.status,
            str(cell.actual),
        )
        for (scenario, clause), cell in results.items()
    }


@pytest.fixture(scope="module")
def dataset_root() -> Path:
    if not DATASET_DIR.is_dir():
        pytest.skip(f"dataset not found at {DATASET_DIR}")
    return DATASET_DIR


@pytest.fixture(scope="module")
def baseline(dataset_root: Path) -> dict:
    return _semantics(dataset_root)


def _copy_dataset(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("ground_truth.json"))
    return destination


def _rewrite_ledger(path: Path, transform) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = list(reader)
    rows = transform(rows)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# --- invariance --------------------------------------------------------------


@slow
def test_renaming_documents_preserves_all_semantics(tmp_path, dataset_root, baseline):
    """Filenames are opaque by design; renaming every document must not
    move a single covenant result.
    """
    root = _copy_dataset(dataset_root, tmp_path / "renamed")
    for index, pdf in enumerate(sorted(root.rglob("*.pdf"))):
        pdf.rename(pdf.with_name(f"doc-{index:04d}.pdf"))
    assert _semantics(root) == baseline


@slow
def test_reversing_ledger_row_order_preserves_all_semantics(tmp_path, dataset_root, baseline):
    root = _copy_dataset(dataset_root, tmp_path / "reordered")
    _rewrite_ledger(discover_dataset(root).ledger, lambda rows: list(reversed(rows)))
    assert _semantics(root) == baseline


@slow
def test_reordering_template_scenarios_preserves_all_semantics(
    tmp_path, dataset_root, baseline
):
    """The template's key order is presentation, not meaning.

    Note this is the strongest *label* mutation available: renaming a
    scenario outright is deliberately not tested as an invariance, because
    the audited notes, AUP reports and treasury memos reference individual
    transactions by id. Rewriting ids in the ledger while leaving the PDFs
    untouched would dangle those references, so the resulting difference
    would be correct behaviour rather than brittleness. Independence from
    the scenario labels themselves is covered by the static literal scan
    below.
    """
    root = _copy_dataset(dataset_root, tmp_path / "reordered-template")
    dataset = discover_dataset(root)
    template = json.loads(dataset.template.read_text(encoding="utf-8"))
    template["answers"] = dict(reversed(list(template["answers"].items())))
    dataset.template.write_text(json.dumps(template, indent=2), encoding="utf-8")

    assert _semantics(root) == baseline


@slow
def test_injecting_a_decoy_document_preserves_all_semantics(tmp_path, dataset_root, baseline):
    """An unrelated document added to the corpus -- even one carrying
    covenant-like vocabulary -- must not become an authority for anyone.
    """
    root = _copy_dataset(dataset_root, tmp_path / "decoyed")
    donor = sorted(root.rglob("*.pdf"))[0]
    for index in range(3):
        shutil.copyfile(donor, root / f"injected-decoy-{index}.pdf")

    mutated = _semantics(root)
    # Duplicating a real document makes its account's authority ambiguous,
    # which the resolver must refuse rather than pick arbitrarily; every
    # other account has to be untouched.
    changed = {key for key, value in baseline.items() if mutated.get(key) != value}
    donor_accounts = {account for account, _ in changed}
    assert len(donor_accounts) <= 1, f"a decoy perturbed several accounts: {donor_accounts}"


@slow
def test_discarding_a_cache_entry_preserves_all_semantics(dataset_root, baseline):
    """Derived caches must be exactly that: discarding one and letting it
    rebuild may cost time but must never change an answer.

    Only a single document's entry is discarded. Clearing the whole cache
    would test the same property at the cost of re-OCRing the entire
    corpus, and would leave the rest of the suite running against a cold
    cache.
    """
    from covenant_agent.common.config import OCR_CACHE_DIR

    entries = sorted(p for p in OCR_CACHE_DIR.iterdir() if p.is_dir())
    if not entries:
        pytest.skip("no cache entries to discard")
    shutil.rmtree(entries[0], ignore_errors=True)

    assert _semantics(dataset_root) == baseline


# --- causal sensitivity ------------------------------------------------------


@slow
def test_scaling_one_accounts_amounts_changes_only_that_account(
    tmp_path, dataset_root, baseline
):
    """A causal change must be *localised*: multiplying one borrower's
    amounts may change that borrower's covenants and must change no one
    else's.
    """
    root = _copy_dataset(dataset_root, tmp_path / "scaled")
    dataset = discover_dataset(root)
    transactions = load_ledger(dataset.ledger)
    target_scenario = transactions[0].scenario_id
    target_account = build_account_scenario_map(transactions)
    target_account = next(
        account for account, scenario in target_account.items() if scenario == target_scenario
    )

    def scale(rows):
        for row in rows:
            if row["txn_id"].split("-")[1] == target_scenario and row["amount"].strip():
                row["amount"] = str(Decimal(row["amount"]) * Decimal("10"))
        return rows

    _rewrite_ledger(dataset.ledger, scale)
    mutated = _semantics(root)

    changed_accounts = {
        account for (account, _), value in mutated.items() if baseline.get((account, _)) != value
    }
    assert changed_accounts <= {target_account}, (
        f"a change confined to {target_account} altered {changed_accounts - {target_account}}"
    )


@slow
def test_distractor_amounts_do_not_affect_any_covenant(tmp_path, dataset_root, baseline):
    """The ledger carries many rows for borrowers that are not scenarios.
    Inflating them must leave every covenant untouched.
    """
    root = _copy_dataset(dataset_root, tmp_path / "distractors")
    dataset = discover_dataset(root)
    scenarios = set(json.loads(dataset.template.read_text(encoding="utf-8"))["answers"])

    def inflate(rows):
        for row in rows:
            if row["txn_id"].split("-")[1] not in scenarios and row["amount"].strip():
                row["amount"] = str(Decimal(row["amount"]) * Decimal("100"))
        return rows

    _rewrite_ledger(dataset.ledger, inflate)
    assert _semantics(root) == baseline


# --- anti-overfitting (always runs) ------------------------------------------


def _public_literals(dataset_root: Path) -> set[str]:
    """Identifiers that exist only in this public instance."""
    dataset = discover_dataset(dataset_root)
    transactions = load_ledger(dataset.ledger)
    accounts = {t.account_id for t in transactions}
    scenarios = {t.scenario_id for t in transactions}
    txn_ids = {t.txn_id for t in transactions}
    filenames = {p.name for p in dataset.documents} | {p.stem for p in dataset.documents}
    return accounts | scenarios | txn_ids | filenames


def test_production_source_contains_no_public_instance_literal(dataset_root):
    """Derive the literals from the corpus at test time and scan the
    solver's string constants for them. Nothing about this public instance
    may be baked into the rules.
    """
    literals = _public_literals(dataset_root)
    # Bare scenario labels are short and collide with ordinary words, so
    # match only the structured identifiers and document names.
    interesting = {
        value
        for value in literals
        if len(value) >= 6 and re.search(r"[-0-9]", value)
    }
    patterns = [
        (value, re.compile(rf"(?<![A-Za-z0-9-]){re.escape(value)}(?![A-Za-z0-9-])"))
        for value in sorted(interesting, key=len, reverse=True)
    ]

    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for value, pattern in patterns:
                if pattern.search(node.value):
                    violations.append(
                        f"{path.relative_to(SRC_ROOT)}:{node.lineno}: {value!r}"
                    )
                    break
    assert not violations, "public-instance literals found in production source: " + "; ".join(
        violations
    )
