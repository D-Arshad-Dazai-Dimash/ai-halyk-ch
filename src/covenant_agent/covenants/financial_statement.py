"""Financial-statement figure extraction (DATA_MODEL.md source-of-truth
hierarchy level 2). Some covenants (PLAN.md finding: e.g. a CapEx/EBITDA
leverage ratio) are defined over the Borrower's or its Group's audited
financial statements, not over individual ledger transactions -- so this is
a distinct source module from ledger/, consulted by calc/engine_a.py
whenever a CovenantRule's MetricSource is a StatementFigureSource.

Key finding baked into this module's design: the numeric figures inside
these statement notes are stated with a "$" prefix, so they are Latin/
numeric anchors that survive PyMuPDF's raw text extraction without OCR
(same as covenant thresholds) -- only the *labels* attached to each number
need OCR/LLM reading to identify which figure is which. Every LLM-proposed
figure is therefore cross-checked against the anchor-parsed dollar amounts
before being trusted, exactly like covenant thresholds.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from covenant_agent.common.config import ANCHOR_RECONCILE_REL_TOL
from covenant_agent.ingestion.anchors import DocumentAnchors
from covenant_agent.llm.cache import cached_json
from covenant_agent.llm.client import extract_structured


class OneOffItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    counterparty: str | None = None
    amount: float
    matched_txn_id: str | None = None  # filled in later by matching against the ledger, if found


class FinancialStatementExtract(BaseModel):
    """Structured figures pulled from one scenario's audited financial
    statement notes. Any field the extractor didn't find stays None --
    calc/engine_a.py treats a missing required field as LOW_CONFIDENCE
    rather than substituting a guess.
    """

    model_config = ConfigDict(frozen=True)

    scenario_id: str
    revenue: float | None = None
    operating_expenses: float | None = None
    capex: float | None = None
    group_capex: float | None = None
    one_off_items: tuple[OneOffItem, ...] = Field(default_factory=tuple)
    one_off_materiality_threshold: float | None = None
    confidence: float = 1.0
    source_document_hash: str | None = None

    @property
    def ebitda_base(self) -> float | None:
        """Revenue less operating expenses, before one-off addbacks --
        matches the pattern observed in the dataset ("EBITDA calculated
        from the Borrower's own statements as Revenue less Operating
        Expenses").
        """
        if self.revenue is None or self.operating_expenses is None:
            return None
        return self.revenue - self.operating_expenses

    @property
    def ebitda_adjusted(self) -> float | None:
        """EBITDA base plus one-off items that meet the materiality
        threshold (only items >= threshold are added back -- matches the
        observed EBITDA-adjustments note pattern).
        """
        base = self.ebitda_base
        if base is None:
            return None
        threshold = self.one_off_materiality_threshold or 0.0
        addback = sum(item.amount for item in self.one_off_items if abs(item.amount) >= threshold)
        return base + addback


_SCHEMA = """
{
  "revenue": <number or null>,
  "operating_expenses": <number or null, as a positive figure>,
  "capex": <number or null, the Borrower's own capital expenditure>,
  "group_capex": <number or null, the Group's consolidated capital expenditure, if separately stated>,
  "one_off_materiality_threshold": <number or null, the minimum size for an item to count as a one-off EBITDA addback>,
  "one_off_items": [
    {"description": "<what the item is>", "counterparty": "<name or null>", "amount": <number>}
  ]
}
""".strip()

_TASK = f"""
You are reading the "Notes to Financial Statements" of a Kazakh corporate
borrower (OCR'd, Russian language), which accompanies its credit-covenant
schedule. Extract the following figures, using the schema below. Use null
for any figure not stated in the text. All amounts are in USD.

{_SCHEMA}

Respond with a single JSON object matching that schema exactly.
""".strip()


def _reconcile_figure(value: float | None, anchors: DocumentAnchors) -> tuple[float | None, bool]:
    if value is None or not anchors.dollar_amounts:
        return value, False
    best = min(anchors.dollar_amounts, key=lambda a: abs(a - value))
    rel_error = abs(best - value) / abs(best) if best else abs(value)
    if rel_error <= ANCHOR_RECONCILE_REL_TOL:
        return best, True
    return value, False


def extract_financial_statement(
    *, scenario_id: str, statement_text: str, anchors: DocumentAnchors, source_document_hash: str
) -> FinancialStatementExtract:
    def _compute() -> dict:
        return extract_structured(
            task_instructions=_TASK,
            untrusted_document_text=statement_text,
            schema_description=_SCHEMA,
        )

    text_hash = hashlib.sha256(statement_text.encode("utf-8")).hexdigest()
    raw = cached_json(f"statement_{text_hash}", _compute)

    confidence = 1.0

    def _num(key: str) -> float | None:
        nonlocal confidence
        v = raw.get(key)
        if v is None:
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            confidence -= 0.2
            return None
        reconciled, matched = _reconcile_figure(fv, anchors)
        if not matched:
            confidence -= 0.1
        return reconciled

    revenue = _num("revenue")
    opex = _num("operating_expenses")
    capex = _num("capex")
    group_capex = _num("group_capex")
    threshold = _num("one_off_materiality_threshold")

    items: list[OneOffItem] = []
    for raw_item in raw.get("one_off_items", []) or []:
        if not isinstance(raw_item, dict):
            continue
        try:
            amount = float(raw_item.get("amount"))
        except (TypeError, ValueError):
            continue
        reconciled_amount, matched = _reconcile_figure(amount, anchors)
        if not matched:
            confidence -= 0.05
        items.append(
            OneOffItem(
                description=str(raw_item.get("description", "")),
                counterparty=raw_item.get("counterparty"),
                amount=reconciled_amount,
            )
        )

    return FinancialStatementExtract(
        scenario_id=scenario_id,
        revenue=revenue,
        operating_expenses=opex,
        capex=capex,
        group_capex=group_capex,
        one_off_items=tuple(items),
        one_off_materiality_threshold=threshold,
        confidence=max(0.0, min(1.0, confidence)),
        source_document_hash=source_document_hash,
    )
