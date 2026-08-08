"""Covenant DSL (COVENANT_DSL.md). Pydantic models only -- no execution
logic here (that's calc/engine_a.py and verify/engine_b.py), and no
arithmetic is ever performed by anything that constructs these models from
LLM output: numeric fields are always reconciled against deterministic
anchors before a CovenantRule is considered valid (see compiler.py).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from covenant_agent.common.enums import MetricKind, Operator, Scope, Sign
from covenant_agent.ledger.categories import Category


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: str  # credit_agreement | financial_statement | ledger | derived
    source_document: str | None = None  # content hash, or None
    page: int | None = None
    source_span: str | None = None
    extraction_method: str  # text_anchor | ocr | llm | rule | computed
    validation_method: str = "none"  # anchor_reconciled | schema | ab_agreement | range_check | none


class LedgerAggregateSource(BaseModel):
    """A metric computed by aggregating normalized ledger transactions."""

    model_config = ConfigDict(frozen=True)

    kind: str = "ledger_aggregate"
    include_categories: tuple[Category, ...] = Field(default_factory=tuple)
    exclude_categories: tuple[Category, ...] = Field(default_factory=tuple)
    sign: Sign = Sign.ANY
    scope: Scope = Scope.AGGREGATE  # AGGREGATE -> abs_sum; SINGLE_TXN -> max_abs


class StatementFigureSource(BaseModel):
    """A metric read from a scenario's audited financial-statement notes
    (see covenants/financial_statement.py), not from the ledger.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = "statement_figure"
    field_name: str  # e.g. "revenue", "operating_expenses", "ebitda", "capex", "group_capex"


MetricSource = LedgerAggregateSource | StatementFigureSource


class CovenantRule(BaseModel):
    """The validated, executable representation of one covenant clause."""

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    clause: str  # "6.1" | "6.2" | "6.3" | ...

    metric_kind: MetricKind
    operator: Operator
    threshold: float
    threshold_unit: str  # "USD" | "x" | "ratio"

    period_start: str
    period_end: str

    # exactly one of these two populated depending on metric_kind
    amount_source: LedgerAggregateSource | StatementFigureSource | None = None
    numerator: LedgerAggregateSource | StatementFigureSource | None = None
    denominator: LedgerAggregateSource | StatementFigureSource | None = None

    exceptions: tuple[str, ...] = Field(default_factory=tuple)  # free-text, for audit/explainability
    carve_outs: tuple[str, ...] = Field(default_factory=tuple)
    conditions: tuple[str, ...] = Field(default_factory=tuple)

    evidence_applicable: bool = True

    raw_clause_text: str = ""
    provenance: tuple[Provenance, ...] = Field(default_factory=tuple)
    confidence: float = 1.0

    def is_structurally_valid(self) -> bool:
        if self.metric_kind == MetricKind.AMOUNT:
            return self.amount_source is not None
        if self.metric_kind == MetricKind.RATIO:
            return self.numerator is not None and self.denominator is not None
        return False
