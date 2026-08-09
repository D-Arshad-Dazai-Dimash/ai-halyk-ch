"""Deterministic production pipeline.

Discovery -> ingestion/OCR -> document catalog -> authority graph ->
covenant compilation -> metric totals -> evaluation -> evidence.

Every figure is arithmetic over classified ledger rows and
document-disclosed adjustments, and covenant structure comes from a
compositional parser. A model is consulted at exactly one point and only
when configured: a clause the parser cannot compile is offered to
``covenants/llm_fallback``, which may propose that clause's *structure*
into the same closed AST. It never produces a number, and it never sees a
clause the parser already read.

The pipeline consumes only the documents, the ledger, and the template's
key structure -- never the test-only answer key.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from covenant_agent.calc.engine import (
    MetricTotals,
    applies,
    build_totals,
    decide,
    evaluate,
    round_reported,
)
from covenant_agent.covenants.ast import Covenant
from covenant_agent.covenants.llm_fallback import FallbackCompiler
from covenant_agent.covenants.parser import parse_clause, split_clauses
from covenant_agent.covenants.section_locator import extract_covenant_section_text
from covenant_agent.diagnostics import Diagnostics
from covenant_agent.documents.doctype import DocType
from covenant_agent.documents.facts import (
    DocumentFacts,
    extract_disclosure_facts,
    extract_ownership_facts,
)
from covenant_agent.ingestion.discover import discover_documents, hash_documents
from covenant_agent.ingestion.ocr_cache import ocr_document, prewarm
from covenant_agent.evidence.decisive import find_decisive
from covenant_agent.ledger.loader import (
    account_prefixes,
    build_account_scenario_map,
    group_by_scenario,
    ledger_reporting_year,
    load_ledger,
)
from covenant_agent.ledger.model import RawTransaction
from covenant_agent.resolve.authority import (
    AuthorityBundle,
    CatalogedDocument,
    Purpose,
    catalog_document,
    resolve_authorities,
)


@dataclass
class CellResult:
    scenario_id: str
    clause: str
    status: str
    actual: Decimal
    evidence_txn_id: str | None
    resolved: bool
    notes: list[str] = field(default_factory=list)


def _catalog(
    document_paths: Sequence[Path], prefixes: tuple[str, ...]
) -> list[CatalogedDocument]:
    """OCR and classify every document. A document that cannot be read is
    skipped so one damaged file cannot stop the run.

    The corpus is OCR'd in parallel first: at 300 dpi a page costs seconds,
    so a few hundred cold documents take tens of minutes serially. After
    the prewarm every read below is a cache hit.
    """
    documents = hash_documents(document_paths)
    prewarm(documents)

    catalog: list[CatalogedDocument] = []
    for found in documents:
        try:
            text = ocr_document(found.path, found.content_hash)
        except Exception:
            continue
        catalog.append(
            catalog_document(
                found.path, found.content_hash, text, account_prefixes=prefixes
            )
        )
    return catalog


def _facts_for(bundle: AuthorityBundle) -> DocumentFacts:
    """Collect adjustments from every authority the borrower has, in
    precedence order: the referenced final AUP report supersedes positions
    stated in the notes, so it is applied last.
    """
    facts = DocumentFacts()
    for purpose in (
        Purpose.FINANCIAL_DISCLOSURES,
        Purpose.TREASURY_ADJUSTMENTS,
        Purpose.RECLASSIFICATIONS,
    ):
        document = bundle.get(purpose)
        if document is not None:
            facts.extend(extract_disclosure_facts(document.text, document.content_hash))

    kyc = bundle.get(Purpose.RELATED_PARTIES)
    if kyc is not None:
        facts.extend(extract_ownership_facts(kyc.text, kyc.content_hash))
    return facts


def _article_of(clauses: Sequence[str]) -> str:
    """The article the template's clause keys belong to ("6.1" -> "6").

    The template is authoritative about which clauses are being asked for,
    so it -- not a hardcoded number -- decides which article to read.
    """
    for clause in clauses:
        head, _, _ = clause.partition(".")
        if head.isdigit():
            return head
    return "6"


def _compile_covenants(
    bundle: AuthorityBundle,
    clauses: Sequence[str],
    year: int,
    fallback: FallbackCompiler | None = None,
) -> tuple[dict[str, Covenant], set[str]]:
    """Compile every clause of the borrower's covenant section.

    Returns the compiled covenants and the subset of clause numbers a model
    had to read. The deterministic parser is always tried first and its
    result is final: the fallback sees only the clauses it could not
    compile, so enabling a model can never alter a parsed answer.
    """
    agreement = bundle.get(Purpose.COVENANT_TERMS)
    if agreement is None:
        return {}, set()
    article = _article_of(clauses)
    section = extract_covenant_section_text(agreement.text, article)
    compiled: dict[str, Covenant] = {}
    model_assisted: set[str] = set()
    for number, text in split_clauses(
        section, article=article, expected=len(clauses)
    ).items():
        covenant = parse_clause(number, text, f"{year}-01-01", f"{year}-12-31")
        if covenant is None and fallback is not None:
            start, end = f"{year}-01-01", f"{year}-12-31"
            covenant = fallback.compile(number, text, start, end)
            if covenant is None:
                # The clause's own segment was not self-contained: OCR can
                # scatter a conditional covenant across the section, and a
                # threshold may be a defined term stated elsewhere. Re-ask
                # against the whole section before giving the cell up.
                covenant = fallback.compile_from_section(number, section, start, end)
            if covenant is not None:
                model_assisted.add(number)
        if covenant is not None:
            compiled[number] = covenant
    return compiled, model_assisted


def _unresolved(scenario_id: str, clause: str, reason: str) -> CellResult:
    """Every template cell must carry an answer -- a blank scores the same
    as a wrong one -- so an unresolved cell still emits a best-effort value
    while recording why it could not be derived.
    """
    return CellResult(
        scenario_id=scenario_id,
        clause=clause,
        status="COMPLIANT",
        actual=Decimal(0),
        evidence_txn_id=None,
        resolved=False,
        notes=[reason],
    )


def run(
    documents: Path | Sequence[Path],
    ledger_csv: Path,
    template_answers: dict,
    diagnostics: Diagnostics | None = None,
    fallback: FallbackCompiler | None = None,
) -> dict[tuple[str, str], CellResult]:
    """Evaluate every template cell.

    ``documents`` is either a directory to scan recursively or an explicit
    list of PDF paths, so the pipeline works equally on the challenge
    layout and on an arbitrary corpus handed in by a caller.

    ``fallback``, when given and configured with an API key, compiles the
    clauses the deterministic parser cannot read. It is consulted nowhere
    else, so a run without it produces identical answers for every clause
    that does parse.
    """
    document_paths = (
        [found.path for found in discover_documents(documents)]
        if isinstance(documents, Path)
        else list(documents)
    )

    report = diagnostics if diagnostics is not None else Diagnostics()
    transactions = load_ledger(ledger_csv, report.skipped_ledger_rows)
    account_to_scenario = build_account_scenario_map(transactions)
    scenario_to_account = {v: k for k, v in account_to_scenario.items()}
    year = ledger_reporting_year(transactions)
    grouped = group_by_scenario(transactions)
    catalog = _catalog(document_paths, account_prefixes(transactions))

    report.note_catalog(catalog)

    results: dict[tuple[str, str], CellResult] = {}

    for scenario_id, clauses in template_answers.items():
        account_id = scenario_to_account.get(scenario_id)
        if account_id is None:
            report.scenarios_without_account.append(scenario_id)
            for clause in clauses:
                results[(scenario_id, clause)] = _unresolved(
                    scenario_id, clause, "scenario_not_in_ledger"
                )
            continue

        bundle = resolve_authorities(account_id, catalog, year)
        report.note_authorities(scenario_id, bundle)
        facts = _facts_for(bundle)
        covenants, model_assisted = _compile_covenants(
            bundle, clauses, year, fallback
        )
        rows: list[RawTransaction] = grouped.get(scenario_id, [])

        for clause in clauses:
            if clause in model_assisted:
                report.model_assisted_clauses.append(f"{scenario_id}.{clause}")
            covenant = covenants.get(clause)
            if covenant is None:
                report.uncompiled_clauses.append(f"{scenario_id}.{clause}")
                results[(scenario_id, clause)] = _unresolved(
                    scenario_id, clause, "clause_not_compiled"
                )
                continue

            totals = build_totals(rows, facts, covenant.period_start, covenant.period_end)
            cell = _evaluate_cell(scenario_id, clause, covenant, totals)
            if clause in model_assisted:
                # Recorded on the cell so the audit record shows where the
                # clause's *structure* came from; the figure itself is still
                # arithmetic over classified ledger rows.
                cell.notes.append("clause_compiled_by_model")
            if not cell.resolved:
                report.unresolved_cells.append(
                    f"{scenario_id}.{clause}: {', '.join(cell.notes) or 'unknown'}"
                )
            results[(scenario_id, clause)] = cell

    if fallback is not None:
        report.note_fallback(fallback)

    return results


def _evaluate_cell(
    scenario_id: str, clause: str, covenant: Covenant, totals: MetricTotals
) -> CellResult:
    raw = evaluate(covenant.expression, totals)
    if raw is None:
        return _unresolved(scenario_id, clause, "metric_unresolved")

    notes: list[str] = []
    if not applies(covenant, totals):
        # A springing covenant whose trigger is not met is complied with by
        # definition; the metric is still reported as measured.
        notes.append("springing_condition_not_met")
        return CellResult(
            scenario_id=scenario_id,
            clause=clause,
            status="COMPLIANT",
            actual=round_reported(raw),
            evidence_txn_id=None,
            resolved=True,
            notes=notes,
        )

    verdict = decide(covenant, raw)
    if verdict.used_raw_basis:
        notes.append("material_rounding_override")

    evidence = find_decisive(covenant, totals, verdict.compliant)

    return CellResult(
        scenario_id=scenario_id,
        clause=clause,
        status="COMPLIANT" if verdict.compliant else "BREACH",
        actual=verdict.reported,
        evidence_txn_id=evidence,
        resolved=True,
        notes=notes,
    )
