"""Calibration-only scorer implementing CASE.md's official rubric exactly.
Reads submission.json (production output) + ground_truth.json (test-only
answer key). This is the ONE place in the whole project allowed to open
ground_truth.json -- it is never imported by src/covenant_agent/**
(enforced by tests/unit/test_no_gt_leakage.py), and its output (an accuracy
report) is never fed back into production logic; it only tells a developer
which general document-interpretation rule to go fix.

Usage: python calibration/score.py [submission.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from covenant_agent.common.config import GROUND_TRUTH_JSON  # noqa: E402


def score_actual(submitted: float | None, correct: float) -> float:
    if submitted is None or not isinstance(submitted, (int, float)) or isinstance(submitted, bool):
        return 0.0
    if correct == 0:
        e = abs(submitted - correct)
    else:
        e = abs(submitted - correct) / abs(correct)
    return 0.30 * max(0.0, 1.0 - e / 0.05)


def score_evidence(submitted_evidence, correct_evidence, actual_score_fraction: float) -> float:
    """If the key expects null, these 0.20 points decay with the SAME
    fraction as the actual score (CASE.md: "эти 0.20 не начисляются
    автоматически... они сбывают вместе с actual по той же шкале").
    Otherwise, evidence must match exactly for full 0.20, else 0.
    """
    if correct_evidence is None:
        return 0.20 * (actual_score_fraction)
    return 0.20 if submitted_evidence == correct_evidence else 0.0


def score_cell(submitted: dict, correct: dict) -> tuple[float, dict]:
    breakdown = {"status": 0.0, "actual": 0.0, "evidence": 0.0}

    if submitted.get("status") != correct["status"]:
        return 0.0, breakdown  # wrong status -> whole cell is 0

    breakdown["status"] = 0.50

    actual_pts = score_actual(submitted.get("actual"), correct["actual"])
    breakdown["actual"] = actual_pts
    actual_fraction = actual_pts / 0.30 if 0.30 else 0.0

    breakdown["evidence"] = score_evidence(submitted.get("evidence_txn_id"), correct["evidence_txn_id"], actual_fraction)

    total = breakdown["status"] + breakdown["actual"] + breakdown["evidence"]
    return total, breakdown


def main() -> None:
    submission_path = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "submission.json"
    submission = json.loads(submission_path.read_text(encoding="utf-8"))
    gt = json.loads(GROUND_TRUTH_JSON.read_text(encoding="utf-8"))

    scenarios = gt["scenarios"]
    total_score = 0.0
    total_cells = 0
    rows = []

    for scenario_id, scenario_gt in scenarios.items():
        sub_scenario = submission.get("answers", {}).get(scenario_id, {})
        for clause, correct in scenario_gt["covenants"].items():
            submitted = sub_scenario.get(clause, {})
            score, breakdown = score_cell(submitted, correct)
            total_score += score
            total_cells += 1
            rows.append((scenario_id, clause, score, breakdown, submitted, correct))

    rows.sort(key=lambda r: r[2])  # worst first

    print(f"{'scenario':6s} {'clause':6s} {'score':6s} {'status':5s} {'actual':6s} {'evid':5s}  submitted vs correct")
    for scenario_id, clause, score, breakdown, submitted, correct in rows:
        print(
            f"{scenario_id:6s} {clause:6s} {score:5.3f}  "
            f"{breakdown['status']:.2f}  {breakdown['actual']:.3f}  {breakdown['evidence']:.3f}   "
            f"[{submitted.get('status')!s:10s} {submitted.get('actual')!s:14s} {submitted.get('evidence_txn_id')!s:16s}] "
            f"vs [{correct['status']:10s} {correct['actual']!s:14s} {correct['evidence_txn_id']!s:16s}]"
        )

    max_score = total_cells * 1.0
    print(f"\nTOTAL: {total_score:.3f} / {max_score:.1f}  ({100*total_score/max_score:.1f}%)")


if __name__ == "__main__":
    main()
