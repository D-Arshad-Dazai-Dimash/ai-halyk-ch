"""Production pipeline orchestrator (PLAN.md "Production pipeline stages").

Consumes only `documents_dir`, `ledger_csv`, and the submission template's
structure (scenario/clause keys -- itself not an answer, just the required
output shape). Never imports or reads the test-only answer key (see
tests/unit/test_no_gt_leakage.py).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from covenant_agent.calc.engine_a import evaluate_status
from covenant_agent.calc.reconcile import reconcile_actual
from covenant_agent.common.enums import CovenantStatus
from covenant_agent.common.exceptions import CovenantAgentError
from covenant_agent.covenants.compiler import compile_clause
from covenant_agent.covenants.dsl import CovenantRule, StatementFigureSource
from covenant_agent.covenants.financial_statement import FinancialStatementExtract, extract_financial_statement
from covenant_agent.covenants.section_locator import extract_covenant_section_text
from covenant_agent.covenants.semantic_extract import extract_clauses
from covenant_agent.evidence.counterfactual import find_evidence
from covenant_agent.ingestion.ocr_cache import ocr_document
from covenant_agent.ingestion.pdf import raw_text
from covenant_agent.ledger.fx import resolve_eur_to_usd_rate
from covenant_agent.ledger.loader import (
    build_account_scenario_map,
    group_by_scenario,
    ledger_reporting_year,
    load_ledger,
)
from covenant_agent.ledger.normalize import normalize_transactions
from covenant_agent.resolve.contract_resolver import resolve_effective_contracts
from covenant_agent.resolve.statement_finder import find_financial_statement
from covenant_agent.validation.cell import CellValidation, fallback_status_for_unresolved, validate_cell


class CellOutput(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    scenario_id: str
    clause: str
    status: CovenantStatus
    actual: float
    evidence_txn_id: str | None
    validation: CellValidation
    rule: CovenantRule | None = None


def _unresolved_cell(scenario_id: str, clause: str) -> CellOutput:
    return CellOutput(
        scenario_id=scenario_id,
        clause=clause,
        status=fallback_status_for_unresolved(),
        actual=0.0,
        evidence_txn_id=None,
        validation=CellValidation(
            scenario_id=scenario_id,
            clause=clause,
            checks=("scenario_or_clause_unresolved",),
            confidence=0.0,
            status="UNRESOLVED",
        ),
        rule=None,
    )


def _uses_statement_source(rule: CovenantRule) -> bool:
    return any(
        isinstance(s, StatementFigureSource) for s in (rule.amount_source, rule.numerator, rule.denominator)
    )


def run_pipeline(documents_dir: Path, ledger_csv: Path, template_answers: dict) -> dict[tuple[str, str], CellOutput]:
    transactions = load_ledger(ledger_csv)
    account_to_scenario = build_account_scenario_map(transactions)
    year = ledger_reporting_year(transactions)
    resolved_contracts = resolve_effective_contracts(documents_dir, account_to_scenario, year)
    grouped = group_by_scenario(transactions)
    known_txn_ids = frozenset(t.txn_id for t in transactions)

    results: dict[tuple[str, str], CellOutput] = {}

    for scenario_id, clauses in template_answers.items():
        print(f"[pipeline] scenario {scenario_id}: starting", flush=True)
        contract = resolved_contracts.get(scenario_id)
        if contract is None:
            for clause in clauses:
                results[(scenario_id, clause)] = _unresolved_cell(scenario_id, clause)
            continue

        # A single scenario's OCR/LLM failure must degrade that scenario's
        # cells to UNRESOLVED, never abort the whole run (ARCHITECTURE.md
        # failure boundaries) -- the local LLM occasionally returns
        # malformed/truncated JSON or ignores the requested schema.
        try:
            full_text = ocr_document(contract.effective.path, contract.effective.content_hash)
            section6_text = extract_covenant_section_text(full_text)
            raw_clauses = extract_clauses(section6_text)
        except CovenantAgentError:
            for clause in clauses:
                results[(scenario_id, clause)] = _unresolved_cell(scenario_id, clause)
            continue

        eur_rate = resolve_eur_to_usd_rate([section6_text, raw_text(contract.effective.path)])
        raw_txns = grouped.get(scenario_id, [])
        normalized_txns = normalize_transactions(raw_txns, eur_rate)

        statement: FinancialStatementExtract | None = None
        statement_lookup_done = False

        for clause in clauses:
            raw_clause = raw_clauses.get(clause)
            if raw_clause is None:
                results[(scenario_id, clause)] = _unresolved_cell(scenario_id, clause)
                continue

            rule = compile_clause(
                scenario_id=scenario_id,
                clause_number=clause,
                raw_clause=raw_clause,
                anchors=contract.effective.anchors,
                fallback_period_start=contract.effective.period_start or "",
                fallback_period_end=contract.effective.period_end or "",
                contract_hash=contract.effective.content_hash,
            )

            if _uses_statement_source(rule) and not statement_lookup_done:
                try:
                    candidate = find_financial_statement(documents_dir, contract.account_id)
                    if candidate is not None:
                        stmt_text = ocr_document(candidate.path, candidate.content_hash)
                        statement = extract_financial_statement(
                            scenario_id=scenario_id,
                            statement_text=stmt_text,
                            anchors=candidate.anchors,
                            source_document_hash=candidate.content_hash,
                        )
                except CovenantAgentError:
                    statement = None  # degrade to LOW_CONFIDENCE/UNRESOLVED for this clause, not a crash
                statement_lookup_done = True

            reconciled = reconcile_actual(rule, normalized_txns, statement)

            if reconciled.actual is not None:
                status = evaluate_status(rule, reconciled.actual)
                evidence = find_evidence(rule, normalized_txns, statement, reconciled.actual, status)
                actual_out = reconciled.actual
            else:
                status = fallback_status_for_unresolved()
                actual_out = rule.threshold
                evidence = None

            validation = validate_cell(rule, reconciled, evidence, known_txn_ids)
            results[(scenario_id, clause)] = CellOutput(
                scenario_id=scenario_id,
                clause=clause,
                status=status,
                actual=actual_out,
                evidence_txn_id=evidence,
                validation=validation,
                rule=rule,
            )
            print(
                f"[pipeline] scenario {scenario_id} clause {clause}: "
                f"status={status.value} actual={actual_out:.2f} evidence={evidence} "
                f"confidence={validation.confidence:.2f}",
                flush=True,
            )

    return results
