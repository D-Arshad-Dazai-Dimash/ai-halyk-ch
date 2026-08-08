"""DSL compiler: raw LLM clause proposals + deterministic anchors -> a
validated CovenantRule (COVENANT_DSL.md "Validation rules"). This is the
gate through which every LLM-proposed number must pass: thresholds are
overridden by anchor-parsed values when they disagree, and clauses that
can't be reconciled are marked low-confidence rather than trusted blindly.
"""

from __future__ import annotations

from covenant_agent.common.config import ANCHOR_RECONCILE_REL_TOL
from covenant_agent.common.enums import MetricKind, Operator, Scope, Sign
from covenant_agent.covenants.dsl import (
    CovenantRule,
    LedgerAggregateSource,
    Provenance,
    StatementFigureSource,
)
from covenant_agent.ingestion.anchors import DocumentAnchors
from covenant_agent.ledger.categories import Category

_OPERATOR_MAP = {op.value: op for op in Operator}
_METRIC_KIND_MAP = {mk.value: mk for mk in MetricKind}


def _closest_anchor_match(value: float, anchors: tuple[float, ...]) -> float | None:
    if not anchors:
        return None
    best = min(anchors, key=lambda a: abs(a - value))
    rel_error = abs(best - value) / abs(best) if best else abs(best - value)
    return best if rel_error <= ANCHOR_RECONCILE_REL_TOL else None


def _reconcile_threshold(
    llm_value: float, unit: str, anchors: DocumentAnchors
) -> tuple[float, float]:
    """Returns (reconciled_value, confidence_delta). If the LLM's threshold
    matches a deterministic anchor closely, that anchor value wins outright
    (it's authoritative); otherwise we keep the LLM value but apply a
    confidence penalty so downstream validation can flag the clause.
    """
    pool = anchors.dollar_amounts if unit == "USD" else anchors.ratio_multiples
    match = _closest_anchor_match(llm_value, pool)
    if match is not None:
        return match, 0.0
    return llm_value, -0.4


def _compile_metric_source(raw: dict | None) -> LedgerAggregateSource | StatementFigureSource | None:
    if not raw or not isinstance(raw, dict):
        return None
    source = raw.get("source")
    if source == "ledger":
        valid_categories = []
        for c in raw.get("categories", []) or []:
            try:
                valid_categories.append(Category(c))
            except ValueError:
                continue
        sign_raw = raw.get("sign", "any")
        try:
            sign = Sign(sign_raw)
        except ValueError:
            sign = Sign.ANY
        scope_raw = raw.get("scope", "aggregate")
        try:
            scope = Scope(scope_raw)
        except ValueError:
            scope = Scope.AGGREGATE
        return LedgerAggregateSource(
            include_categories=tuple(valid_categories), sign=sign, scope=scope
        )
    if source == "financial_statement":
        field = raw.get("field")
        if isinstance(field, str) and field:
            return StatementFigureSource(field_name=field)
    return None


def compile_clause(
    *,
    scenario_id: str,
    clause_number: str,
    raw_clause: dict,
    anchors: DocumentAnchors,
    fallback_period_start: str,
    fallback_period_end: str,
    contract_hash: str,
) -> CovenantRule:
    """Compiles one LLM-proposed clause dict into a validated CovenantRule.
    Never raises on malformed input -- returns a rule with confidence
    penalties instead, so a single bad clause degrades gracefully rather
    than aborting the whole scenario (ARCHITECTURE.md failure boundaries).
    """
    confidence = 1.0
    provenance: list[Provenance] = []

    metric_kind_raw = str(raw_clause.get("metric_kind", "")).strip()
    metric_kind = _METRIC_KIND_MAP.get(metric_kind_raw)
    if metric_kind is None:
        metric_kind = MetricKind.AMOUNT
        confidence -= 0.5

    operator_raw = str(raw_clause.get("operator", "")).strip()
    operator = _OPERATOR_MAP.get(operator_raw)
    if operator is None:
        operator = Operator.LE
        confidence -= 0.3

    threshold_unit = str(raw_clause.get("threshold_unit", "USD")).strip() or "USD"
    try:
        llm_threshold = float(raw_clause.get("threshold_value"))
    except (TypeError, ValueError):
        llm_threshold = 0.0
        confidence -= 0.5

    threshold, delta = _reconcile_threshold(llm_threshold, threshold_unit, anchors)
    confidence += delta
    provenance.append(
        Provenance(
            source_type="credit_agreement",
            source_document=contract_hash,
            extraction_method="llm" if delta < 0 else "text_anchor",
            validation_method="anchor_reconciled" if delta == 0 else "none",
        )
    )

    period_start = str(raw_clause.get("period_start") or fallback_period_start)
    period_end = str(raw_clause.get("period_end") or fallback_period_end)
    if period_start not in anchors.dates:
        period_start = fallback_period_start
    if period_end not in anchors.dates:
        period_end = fallback_period_end

    amount_source = _compile_metric_source(raw_clause.get("amount_source"))
    numerator = _compile_metric_source(raw_clause.get("numerator"))
    denominator = _compile_metric_source(raw_clause.get("denominator"))

    if metric_kind == MetricKind.AMOUNT and amount_source is None:
        confidence -= 0.4
    if metric_kind == MetricKind.RATIO and (numerator is None or denominator is None):
        confidence -= 0.4

    exceptions = tuple(str(x) for x in (raw_clause.get("exceptions") or []) if x)
    carve_outs = tuple(str(x) for x in (raw_clause.get("carve_outs") or []) if x)
    conditions = tuple(str(x) for x in (raw_clause.get("conditions") or []) if x)
    evidence_applicable = bool(raw_clause.get("evidence_applicable", True))
    raw_text = str(raw_clause.get("raw_clause_text", ""))

    return CovenantRule(
        scenario_id=scenario_id,
        clause=clause_number,
        metric_kind=metric_kind,
        operator=operator,
        threshold=threshold,
        threshold_unit=threshold_unit,
        period_start=period_start,
        period_end=period_end,
        amount_source=amount_source,
        numerator=numerator,
        denominator=denominator,
        exceptions=exceptions,
        carve_outs=carve_outs,
        conditions=conditions,
        evidence_applicable=evidence_applicable,
        raw_clause_text=raw_text,
        provenance=tuple(provenance),
        confidence=max(0.0, min(1.0, confidence)),
    )
