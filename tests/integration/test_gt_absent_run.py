"""P13 acceptance: the production pipeline runs end-to-end and produces a
schema-valid submission.json when ground_truth.json is physically absent.

This is the dynamic complement to tests/unit/test_no_gt_leakage.py's static
import-graph check: it proves independence by actually deleting the file
(in a throwaway copy of the dataset -- the real dataset is never touched)
and running the real `run.py` entrypoint as a subprocess.

Opt-in and slow (full OCR+LLM pipeline over the whole dataset), so it does
not run by default -- set RUN_SLOW_INTEGRATION=1 to include it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SLOW_INTEGRATION") != "1",
    reason="full pipeline run is slow (OCR+LLM over ~200 docs); set RUN_SLOW_INTEGRATION=1 to run it",
)


def test_pipeline_runs_end_to_end_with_ground_truth_physically_deleted(tmp_path):
    from covenant_agent.common.config import DATASET_DIR

    if not DATASET_DIR.is_dir():
        pytest.skip(f"dataset not found at {DATASET_DIR}")

    # Throwaway copy of the dataset, with ground_truth.json omitted entirely
    # -- the real dataset directory is never modified.
    shadow_dataset = tmp_path / "dataset_without_gt"
    shutil.copytree(
        DATASET_DIR,
        shadow_dataset,
        ignore=shutil.ignore_patterns("ground_truth.json"),
    )
    assert not (shadow_dataset / "ground_truth.json").exists()
    assert (shadow_dataset / "documents").is_dir()
    assert (shadow_dataset / "master_ledger_2025.csv").is_file()

    output_path = tmp_path / "submission.json"
    env = dict(os.environ)
    env["COVENANT_AGENT_DATASET_DIR"] = str(shadow_dataset)

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), "--output", str(output_path)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    assert result.returncode == 0, f"run.py failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert output_path.is_file()

    submission = json.loads(output_path.read_text(encoding="utf-8"))
    assert "answers" in submission
    for scenario_id, clauses in submission["answers"].items():
        for clause, cell in clauses.items():
            assert cell["status"] in ("COMPLIANT", "BREACH")
            assert isinstance(cell["actual"], (int, float))
            assert cell["actual"] >= 0
