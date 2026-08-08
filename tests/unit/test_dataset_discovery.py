"""Content-based dataset discovery: inputs are found by what they contain,
so an arbitrary corpus with different names and nesting still resolves.
"""

from __future__ import annotations

import json

import pytest

from covenant_agent.common.exceptions import CovenantAgentError
from covenant_agent.ingestion.dataset import (
    discover_dataset,
    looks_like_ledger,
    looks_like_template,
)

LEDGER_HEADER = "txn_id,date,account_id,counterparty,description,amount,currency\n"
LEDGER_ROW = "TXN-X1-0001,2025-01-01,ACC-1001,Acme,Widget,-100.00,USD\n"


def _template(filled: bool = False) -> str:
    cell = (
        {"status": "COMPLIANT", "actual": 1.0, "evidence_txn_id": None}
        if filled
        else {"status": None, "actual": None, "evidence_txn_id": None}
    )
    return json.dumps(
        {"team": "", "contact_email": "", "model": "", "answers": {"X1": {"6.1": cell}}}
    )


def _corpus(root, *, ledger_name="anything.csv", template_name="whatever.json"):
    (root / "nested" / "deep").mkdir(parents=True)
    (root / ledger_name).write_text(LEDGER_HEADER + LEDGER_ROW, encoding="utf-8")
    (root / template_name).write_text(_template(), encoding="utf-8")
    (root / "nested" / "deep" / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (root / "nested" / "b.pdf").write_bytes(b"%PDF-1.4 b")
    return root


def test_finds_inputs_regardless_of_filename_and_nesting(tmp_path):
    _corpus(tmp_path)
    dataset = discover_dataset(tmp_path)
    assert dataset.ledger.name == "anything.csv"
    assert dataset.template.name == "whatever.json"
    assert len(dataset.documents) == 2  # found recursively


def test_ledger_detected_by_columns_not_extension_alone(tmp_path):
    _corpus(tmp_path)
    # A CSV without the transaction columns is not a ledger.
    (tmp_path / "server-log.csv").write_text("timestamp,srcip,status\n1,2,3\n", encoding="utf-8")
    dataset = discover_dataset(tmp_path)
    assert dataset.ledger.name == "anything.csv"
    assert any(p.name == "server-log.csv" for p in dataset.ignored)


def test_answer_key_is_ignored_not_selected(tmp_path):
    """A public answer key sitting in the input directory must never be
    selected as an input -- it is reported as ignored.
    """
    _corpus(tmp_path)
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"scenarios": {"X1": {"covenants": {}}}}), encoding="utf-8"
    )
    dataset = discover_dataset(tmp_path)
    assert dataset.template.name == "whatever.json"
    assert any(p.name == "ground_truth.json" for p in dataset.ignored)


def test_filled_submission_is_not_mistaken_for_the_template(tmp_path):
    """A previous run's output has the same shape but non-empty cells."""
    _corpus(tmp_path)
    (tmp_path / "previous-submission.json").write_text(_template(filled=True), encoding="utf-8")
    dataset = discover_dataset(tmp_path)
    assert dataset.template.name == "whatever.json"
    assert any(p.name == "previous-submission.json" for p in dataset.ignored)


def test_two_candidate_ledgers_is_a_hard_error(tmp_path):
    _corpus(tmp_path)
    (tmp_path / "second.csv").write_text(LEDGER_HEADER + LEDGER_ROW, encoding="utf-8")
    with pytest.raises(CovenantAgentError, match="exactly one"):
        discover_dataset(tmp_path)


def test_missing_template_is_a_hard_error(tmp_path):
    (tmp_path / "x.csv").write_text(LEDGER_HEADER + LEDGER_ROW, encoding="utf-8")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(CovenantAgentError, match="exactly one"):
        discover_dataset(tmp_path)


def test_no_documents_is_a_hard_error(tmp_path):
    (tmp_path / "x.csv").write_text(LEDGER_HEADER + LEDGER_ROW, encoding="utf-8")
    (tmp_path / "t.json").write_text(_template(), encoding="utf-8")
    with pytest.raises(CovenantAgentError, match="no PDF"):
        discover_dataset(tmp_path)


def test_explicit_paths_override_discovery(tmp_path):
    _corpus(tmp_path)
    (tmp_path / "other.csv").write_text(LEDGER_HEADER + LEDGER_ROW, encoding="utf-8")
    # Two ledger candidates would normally be ambiguous; naming one resolves it.
    dataset = discover_dataset(tmp_path, ledger=tmp_path / "other.csv")
    assert dataset.ledger.name == "other.csv"


def test_missing_root_is_a_hard_error(tmp_path):
    with pytest.raises(CovenantAgentError, match="not found"):
        discover_dataset(tmp_path / "nope")


def test_predicates_reject_unrelated_files(tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    assert not looks_like_ledger(text)
    assert not looks_like_template(text)
