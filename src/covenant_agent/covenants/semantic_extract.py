"""Semantic interpretation layer: OCR'd Section 6 prose -> structured clause
proposals (COVENANT_DSL.md "Semantic layers"). The LLM here only labels and
structures what the document says -- it never computes a number. Every
numeric field it proposes is re-validated against deterministic Latin
anchors by covenants/compiler.py before becoming part of an executable
CovenantRule.
"""

from __future__ import annotations

import hashlib
from typing import Any

from covenant_agent.ledger.categories import Category
from covenant_agent.llm.cache import cached_json
from covenant_agent.llm.client import extract_structured

STATEMENT_FIELDS = ("revenue", "operating_expenses", "ebitda", "capex", "group_capex")

_METRIC_SOURCE_SCHEMA = (
    '{"source": "ledger", "categories": [<subset of the category list below>], '
    '"sign": "outflow"|"inflow"|"any", "scope": "aggregate"|"single_txn"} '
    "OR "
    f'{{"source": "financial_statement", "field": one of {list(STATEMENT_FIELDS)}}}'
)

_CLAUSE_SCHEMA = f"""
{{
  "metric_kind": "ratio" | "amount",
  "operator": "<=" | ">=" | "<" | ">" | "==",
  "threshold_value": <number as written in the clause>,
  "threshold_unit": "USD" | "x" | "ratio",
  "period_start": "YYYY-MM-DD",
  "period_end": "YYYY-MM-DD",
  "amount_source": {_METRIC_SOURCE_SCHEMA} | null,
  "numerator": {_METRIC_SOURCE_SCHEMA} | null,
  "denominator": {_METRIC_SOURCE_SCHEMA} | null,
  "exceptions": [<verbatim or closely-paraphrased strings, one per exception/carve-out found>],
  "carve_outs": [<strings>],
  "conditions": [<strings, e.g. "provided that...", "notwithstanding...", date conditions>],
  "evidence_applicable": <true if a single transaction could plausibly determine the verdict (e.g. a per-payment cap or a threshold an aggregate could cross), false if this is a pure ratio/minimum that no single transaction determines>,
  "raw_clause_text": "<the clause text this was extracted from, verbatim>"
}}
""".strip()

_TASK_INSTRUCTIONS = f"""
You are reading Section 6 ("Financial Covenants") of a Kazakh corporate
credit agreement, OCR'd from Russian. It defines exactly three numbered
covenant sub-clauses (their numbers appear in the text, e.g. "6.1", "6.2",
"6.3" -- use whatever numbers actually appear as the JSON keys).

For EACH sub-clause, extract a structured description of the rule using
this schema:
{_CLAUSE_SCHEMA}

If a metric is amount_source (metric_kind "amount"), leave numerator and
denominator null. If it is a ratio (metric_kind "ratio"), leave
amount_source null and fill numerator and denominator.

OPERATOR POLARITY -- read carefully, this is the most common mistake.
Russian legal text often phrases a maximum as a negated obligation ("не
допускать, чтобы ... превышал", "обязуется не допускать превышения",
"не более", "не должен превышать") -- ALL of these mean the metric must
stay AT OR BELOW the number, i.e. operator "<=" , even though grammatically
they don't contain the word "maximum" itself. Conversely "не менее", "не
ниже", "должен составлять не менее", "минимальный" mean the metric must
stay AT OR ABOVE the number, i.e. operator ">=" . Before picking an
operator, identify whether the clause is a CAP (small headline word like
Maximum/Максимальные, or a "must not exceed" phrasing) or a FLOOR (Minimum/
Минимальная, or a "must be at least" phrasing) -- do not default to "<="
or ">=" without this check.

SIGN CONVENTION for ledger-sourced amounts -- money leaving the Borrower
(expenses, payments, capital expenditure, interest, rent, payroll, taxes)
is "outflow"; money received BY the Borrower (revenue, rebates, refunds,
deposits returned) is "inflow". Get this right per component: e.g. a ratio
denominator described as "operating expenses plus rental payments" is
"outflow", not "inflow", even though it sits in a denominator.

COMPLETENESS -- if a clause's metric is a SUM of several described things
(e.g. "the sum of operating expenses and rental payments"), include ALL of
the matching categories in "categories", not just one.

For any ledger-sourced amount_source/numerator/denominator, "categories"
must be chosen ONLY from this fixed list (use the exact strings):
{[c.value for c in Category]}

Use "financial_statement" as the source (with one of {list(STATEMENT_FIELDS)}
as "field") whenever the clause says the metric is determined by the
Borrower's (or its Group's) audited/consolidated financial statements
rather than by looking at individual payments -- this is common for
EBITDA, Revenue, and CapEx-based covenants. If the clause concerns payments
to related/affiliated parties, use a ledger amount_source with the
"adjustments.related_party" category.

Respond with a single JSON object: {{"clauses": {{"<number>": {{...}}, ...}}}}
""".strip()


def extract_clauses(section6_text: str, *, use_cache: bool = True) -> dict[str, dict[str, Any]]:
    """Returns clause_number -> raw (unvalidated) clause dict. Callers MUST
    run each through covenants/compiler.py before use. Cached to disk keyed
    by a hash of the *exact* input text (not the source document -- if
    upstream preprocessing changes what text we send, the cache key changes
    too, so a stale result is never silently reused). Pure engineering
    resilience against a slow/flaky local model, not a correctness
    dependency (llm/cache.py).
    """

    def _compute() -> dict[str, Any]:
        return extract_structured(
            task_instructions=_TASK_INSTRUCTIONS,
            untrusted_document_text=section6_text,
            schema_description='{"clauses": {"<clause_number>": {...}, ...}}',
        )

    if use_cache:
        # Hash covers both the input text AND the prompt -- if either
        # changes (e.g. a prompt-wording fix), the key changes too, so a
        # cached response from the old prompt is never silently reused.
        cache_input = _TASK_INSTRUCTIONS + "\x00" + section6_text
        text_hash = hashlib.sha256(cache_input.encode("utf-8")).hexdigest()
        result = cached_json(f"clauses_{text_hash}", _compute)
    else:
        result = _compute()
    clauses = result.get("clauses", {})
    return clauses if isinstance(clauses, dict) else {}
