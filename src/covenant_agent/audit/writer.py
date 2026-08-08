"""Audit trail writer (DATA_MODEL.md AuditRecord, THREAT_MODEL.md: verifiable
facts only, no chain-of-thought). One JSON file per scenario under
AUDIT_OUTPUT_DIR, so "why is this covenant BREACH/COMPLIANT?" is always
answerable from disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from covenant_agent.pipeline import CellOutput


def _rule_to_dict(rule) -> dict | None:
    if rule is None:
        return None
    return json.loads(rule.model_dump_json())


def write_audit(results: dict[tuple[str, str], CellOutput], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    by_scenario: dict[str, dict] = {}
    for (scenario_id, clause), result in results.items():
        by_scenario.setdefault(scenario_id, {})[clause] = {
            "status": result.status.value,
            "actual": result.actual,
            "evidence_txn_id": result.evidence_txn_id,
            "confidence": result.validation.confidence,
            "validation_status": result.validation.status.value
            if hasattr(result.validation.status, "value")
            else result.validation.status,
            "validation_checks": list(result.validation.checks),
            "covenant_rule": _rule_to_dict(result.rule),
        }

    for scenario_id, clauses in by_scenario.items():
        path = output_dir / f"{scenario_id}.json"
        path.write_text(json.dumps(clauses, indent=2, ensure_ascii=False), encoding="utf-8")
