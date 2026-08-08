"""Arming a model must not change an answer the parser already produced.

The fallback's whole safety argument rests on one claim: it is consulted
only where ``parse_clause`` returned ``None``. This test enforces that
claim against the real corpus rather than a fixture -- it runs the pipeline
with a provider wired to a stub that *fails loudly if it is ever called*,
and compares every cell against a run with no fallback at all.

If someone later widens the fallback's reach -- offering it a clause the
parser did compile, or letting it second-guess a parsed result -- this test
fails on the very first such clause.

Opt-in and slow (OCR over the whole corpus when the cache is cold), like
the other integration tests: set RUN_SLOW_INTEGRATION=1.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SLOW_INTEGRATION") != "1",
    reason="full pipeline run is slow (OCR over ~200 docs); set RUN_SLOW_INTEGRATION=1 to run it",
)


def test_an_armed_model_is_never_consulted_while_every_clause_parses(monkeypatch):
    from covenant_agent.common.config import DATASET_DIR
    from covenant_agent.covenants import llm_fallback
    from covenant_agent.deterministic_pipeline import run
    from covenant_agent.ingestion.dataset import discover_dataset
    from covenant_agent.llm.cloud import Provider

    if not DATASET_DIR.is_dir():
        pytest.skip(f"dataset not found at {DATASET_DIR}")

    dataset = discover_dataset(DATASET_DIR)
    template = json.loads(dataset.template.read_text(encoding="utf-8"))["answers"]

    offline = run(dataset.documents, dataset.ledger, template)

    def must_not_be_called(*_args, **_kwargs):
        raise AssertionError(
            "the model fallback was consulted for a clause the deterministic "
            "parser compiled -- it must only ever see clauses that failed"
        )

    monkeypatch.setattr(llm_fallback, "complete_json", must_not_be_called)
    monkeypatch.setattr(
        llm_fallback, "detect_providers", lambda: [Provider("stub", "stub-model", "key")]
    )
    compiler = llm_fallback.FallbackCompiler.create()
    assert compiler.available, "the stub provider should look configured"

    armed = run(dataset.documents, dataset.ledger, template, fallback=compiler)

    assert compiler.calls_made == 0
    assert compiler.models_used == set()

    assert set(armed) == set(offline)
    for key, cell in armed.items():
        reference = offline[key]
        assert (cell.status, cell.actual, cell.evidence_txn_id) == (
            reference.status,
            reference.actual,
            reference.evidence_txn_id,
        ), f"{key} changed when a model was armed"
