"""Submission generation: fills submission_template.json's existing keys
exactly, never adding/renaming/removing any (CASE.md: a mismatched key
makes that cell unscoreable). Every cell always gets a value -- an empty
cell scores the same as a wrong one, so there's never a reason to omit one.
"""

from __future__ import annotations

from covenant_agent.pipeline import CellOutput


def generate_submission(
    template: dict, results: dict[tuple[str, str], CellOutput], *, team: str, contact_email: str, model: str
) -> dict:
    answers: dict[str, dict] = {}
    for scenario_id, clauses in template.get("answers", {}).items():
        answers[scenario_id] = {}
        for clause in clauses:
            result = results.get((scenario_id, clause))
            if result is None:
                answers[scenario_id][clause] = {
                    "status": "COMPLIANT",
                    "actual": 0.0,
                    "evidence_txn_id": None,
                }
            else:
                answers[scenario_id][clause] = {
                    "status": result.status.value,
                    "actual": round(result.actual, 2),
                    "evidence_txn_id": result.evidence_txn_id,
                }
    return {"team": team, "contact_email": contact_email, "model": model, "answers": answers}


def validate_submission_schema(submission: dict, template: dict) -> list[str]:
    """Structural check: every scenario/clause key present in the template
    must be present (and only those) in the submission, with correctly
    typed status/actual/evidence_txn_id fields.

    ``team`` and ``contact_email`` must be filled in. ``model`` names the
    AI model used and is allowed to be ``null`` when none was: the answers
    here come from parsing and arithmetic, and claiming a model that did
    not produce them would be false.
    """
    errors: list[str] = []

    for field in ("team", "contact_email"):
        value = submission.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be a non-empty string, got {value!r}")
    model = submission.get("model")
    if model is not None and not isinstance(model, str):
        errors.append(f"model must be a string or null, got {model!r}")

    template_answers = template.get("answers", {})
    submission_answers = submission.get("answers", {})

    if set(template_answers) != set(submission_answers):
        errors.append(
            f"scenario key mismatch: template={sorted(template_answers)} "
            f"submission={sorted(submission_answers)}"
        )

    for scenario_id, clauses in template_answers.items():
        sub_clauses = submission_answers.get(scenario_id, {})
        if set(clauses) != set(sub_clauses):
            errors.append(
                f"{scenario_id}: clause key mismatch: template={sorted(clauses)} "
                f"submission={sorted(sub_clauses)}"
            )
            continue
        for clause in clauses:
            cell = sub_clauses[clause]
            if cell.get("status") not in ("COMPLIANT", "BREACH"):
                errors.append(f"{scenario_id}.{clause}: invalid status {cell.get('status')!r}")
            actual = cell.get("actual")
            if not isinstance(actual, (int, float)) or isinstance(actual, bool) or actual < 0:
                errors.append(f"{scenario_id}.{clause}: invalid actual {actual!r}")
            evidence = cell.get("evidence_txn_id")
            if evidence is not None and not isinstance(evidence, str):
                errors.append(f"{scenario_id}.{clause}: invalid evidence_txn_id {evidence!r}")

    return errors
