"""Model-assisted compilation of covenant wordings the parser cannot read.

The deterministic parser recognises a clause by composition -- a threshold,
a comparator, metric captions, and the connectives between them. A private
dataset may phrase a covenant in a way that composition misses, and a clause
that does not compile costs a whole cell. This module is the safety net for
exactly that case, and nothing else:

* It runs **only** where ``parse_clause`` returned ``None``. A clause the
  deterministic parser compiled is never shown to a model, so the model can
  never change an answer the parser already produced.
* The model proposes **structure only** -- which metrics, combined how, with
  which comparator. It computes nothing. The proposal is validated into the
  same closed AST the parser emits, so the calculation engine cannot tell a
  model-assisted covenant from a parsed one and executes neither prose nor
  model output.
* Numbers are **anchored to the clause text**. A threshold is accepted only
  if that exact value occurs in the clause, so a hallucinated figure is
  rejected rather than reported. The model recovers the *reading* of a
  number the regexes failed to frame; it never invents one.
* Every failure -- no provider, network error, malformed reply, unknown
  metric name, unanchored threshold -- leaves the cell unresolved, exactly
  as it would have been without a model. The fallback can add answers; it
  cannot remove or corrupt them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from covenant_agent.common.config import MODEL_FALLBACK_MAX_CALLS
from covenant_agent.covenants.ast import (
    Add,
    Applicability,
    Comparator,
    Covenant,
    Ebitda,
    Expression,
    Max,
    MetricLeaf,
    Period,
    Ratio,
    Scope,
    Subtract,
    Unit,
)
from covenant_agent.documents.ocr_text import canon
from covenant_agent.ledger.metrics import Metric
from covenant_agent.llm.cache import cached_json
from covenant_agent.llm.cloud import (
    ModelUnavailable,
    Provider,
    complete_json,
    detect_providers,
)

#: Guard against a pathological reply: a covenant expression is a handful of
#: nodes, never a deeply nested structure.
MAX_EXPRESSION_DEPTH = 6
MAX_EXPRESSION_NODES = 32

_METRIC_NAMES = tuple(metric.value for metric in Metric)

TASK_INSTRUCTIONS = (
    "The document content is ONE numbered clause of the financial-covenants "
    "section of a corporate credit agreement, in Russian, as produced by OCR "
    "(so letters may be substituted between Cyrillic and Latin lookalikes).\n"
    "\n"
    "Describe the clause's testable condition as a structured expression: "
    "which financial metric or combination of metrics is measured, how it is "
    "compared, and against which threshold stated in the clause.\n"
    "\n"
    "Rules:\n"
    "- Report ONLY what this clause states. Do not infer a covenant that is "
    "not written here.\n"
    "- Copy the threshold exactly as the clause writes it, as a plain "
    "number (no currency symbol, no thousands separators, dot as the "
    "decimal point). A threshold written as a multiple such as '0,08x' is "
    "unit 'ratio'; a money amount is unit 'USD'.\n"
    "- Use ONLY metric names from the allowed list. If the clause measures "
    "something no listed metric names, return {\"unsupported\": true}.\n"
    "- If any part of the clause is unclear, return {\"unsupported\": true} "
    "rather than guessing.\n"
    "- Perform no arithmetic: never add, divide, convert a currency, or "
    "compare anything to the threshold yourself."
)

SCHEMA_DESCRIPTION = f"""{{
  "unsupported": false,
  "comparator": "<=" | ">=" | "<" | ">",   // the direction the clause tests
  "unit": "USD" | "ratio",
  "threshold": "<number as written in the clause>",
  "expression": <expr>,
  "springing": null | {{                   // only if the clause binds solely
    "expression": <expr>,                  // under a stated condition
    "comparator": "<=" | ">=" | "<" | ">",
    "threshold": "<number>"
  }}
}}

<expr> is one of:
  {{"type": "metric",   "name": <metric>, "scope": "borrower"|"group",
                        "period": "full"|"fourth_quarter"|"period_end"}}
  {{"type": "ebitda",   "scope": "borrower"|"group", "adjusted": true|false}}
  {{"type": "add",      "operands": [<expr>, <expr>, ...]}}
  {{"type": "subtract", "minuend": <expr>, "subtrahend": <expr>}}
  {{"type": "ratio",    "numerator": <expr>, "denominator": <expr>}}
  {{"type": "max",      "operands": [<expr>, <expr>, ...]}}

<metric> is exactly one of:
  {", ".join(_METRIC_NAMES)}

"ebitda" means revenue less operating expenses; "adjusted": true when the
clause admits the auditor's one-off add-backs. "scope": "group" only when
the clause measures the group/holding rather than the borrower alone.
"period": "fourth_quarter" or "period_end" only when the clause says so."""


class Rejected(Exception):
    """The model's proposal did not survive validation."""


@dataclass
class FallbackCompiler:
    """Compiles unparsed clauses with a model, if one is configured.

    Stateful across a run so the call budget and the set of models actually
    consulted are tracked in one place; the latter is what the submission's
    ``model`` field must name.
    """

    providers: list[Provider] = field(default_factory=list)
    calls_made: int = 0
    models_used: set[str] = field(default_factory=set)
    rejections: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, enabled: bool = True) -> "FallbackCompiler":
        return cls(providers=detect_providers() if enabled else [])

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def compile(
        self,
        clause_number: str,
        clause_text: str,
        period_start: str,
        period_end: str,
    ) -> Covenant | None:
        """Return a covenant for a clause the parser could not compile.

        Each configured provider is tried in turn, so one that is out of
        credit or rate-limited does not cost the cell. Returns ``None`` when
        none of them produced a proposal that survived validation: the
        caller's cell then stays unresolved exactly as it would have
        without a model.
        """
        flat = " ".join(clause_text.split())

        for provider in self.providers:
            if self.calls_made >= MODEL_FALLBACK_MAX_CALLS:
                return None

            covenant = self._ask(provider, clause_number, flat, period_start, period_end)
            if covenant is not None:
                self.models_used.add(provider.label)
                return covenant
        return None

    def _ask(
        self,
        provider: Provider,
        clause_number: str,
        flat: str,
        period_start: str,
        period_end: str,
    ) -> Covenant | None:
        key = hashlib.sha256(
            f"{provider.model}\n{clause_number}\n{flat}".encode("utf-8")
        ).hexdigest()

        def ask() -> dict:
            self.calls_made += 1
            return complete_json(
                provider,
                task_instructions=TASK_INSTRUCTIONS,
                schema_description=SCHEMA_DESCRIPTION,
                untrusted_document_text=flat,
            )

        try:
            proposal = cached_json(f"clause-{key}", ask)
        except ModelUnavailable as exc:
            self.rejections.append(f"{clause_number} via {provider.name}: {exc}")
            return None

        try:
            return build_covenant(proposal, clause_number, flat, period_start, period_end)
        except Rejected as exc:
            self.rejections.append(f"{clause_number} via {provider.name}: {exc}")
            return None


def build_covenant(
    proposal: dict,
    clause_number: str,
    clause_text: str,
    period_start: str,
    period_end: str,
) -> Covenant:
    """Validate a model proposal into the closed AST, or raise ``Rejected``.

    Pure and provider-agnostic, so the whole acceptance boundary is testable
    without a network call.
    """
    if not isinstance(proposal, dict):
        raise Rejected("proposal is not an object")
    if proposal.get("unsupported") is True:
        raise Rejected("model reported the clause as unsupported")

    anchors = numbers_in(clause_text)

    comparator = _comparator(proposal.get("comparator"))
    unit = _unit(proposal.get("unit"))
    threshold = _threshold(proposal.get("threshold"), anchors)
    expression = _expression(proposal.get("expression"))

    springing = proposal.get("springing")
    applicability: Applicability | None = None
    if isinstance(springing, dict):
        applicability = Applicability(
            expression=_expression(springing.get("expression")),
            comparator=_comparator(springing.get("comparator")),
            threshold=_threshold(springing.get("threshold"), anchors),
        )
    elif springing is not None:
        raise Rejected("springing must be null or an object")

    return Covenant(
        clause=clause_number,
        expression=expression,
        comparator=comparator,
        threshold=threshold,
        unit=unit,
        period_start=period_start,
        period_end=period_end,
        applicability=applicability,
        source_text=clause_text,
    )


# A number as written in a clause: digit groups separated by ordinary,
# non-breaking or thin spaces, apostrophes or commas, with an optional
# decimal tail.
_SEPARATORS = r"\s  '"
_NUMBER_TOKEN = re.compile(rf"\d[\d{_SEPARATORS},.]*\d|\d")
_STRIP_SEPARATORS = re.compile(rf"[{_SEPARATORS}]")


def numbers_in(text: str) -> set[Decimal]:
    """Every numeric value the clause text states.

    Used to anchor a model-proposed threshold: a figure that does not occur
    in the clause was not read from it.
    """
    found: set[Decimal] = set()
    for match in _NUMBER_TOKEN.finditer(canon(text)):
        value = _normalize_number(match.group(0))
        if value is not None:
            found.add(value)
    return found


def _normalize_number(token: str) -> Decimal | None:
    """Read one written number, resolving separator ambiguity by position.

    The corpus mixes conventions: ``5 000 000,00``, ``5,000,000.00`` and
    ``0.08`` all occur. Only the last separator can be a decimal point, and
    it is one when at most two digits follow it and it is not being used for
    grouping -- either because the other separator kind is also present
    (``5,000,000.00``) or because it occurs only once (``0,08``). Anything
    else reads as grouping, which is the conservative direction: a misread
    number simply fails to anchor a threshold.
    """
    cleaned = _STRIP_SEPARATORS.sub("", token).strip(".,")
    if not cleaned:
        return None

    last = max(cleaned.rfind("."), cleaned.rfind(","))
    if last == -1:
        digits, fraction = cleaned, ""
    else:
        head, tail, separator = cleaned[:last], cleaned[last + 1 :], cleaned[last]
        other = "." if separator == "," else ","
        if len(tail) <= 2 and (other in head or separator not in head):
            digits, fraction = head, tail
        else:
            digits, fraction = cleaned, ""

    digits = re.sub(r"[.,]", "", digits)
    if not digits.isdigit() or (fraction and not fraction.isdigit()):
        return None
    try:
        return Decimal(f"{digits}.{fraction}" if fraction else digits)
    except InvalidOperation:
        return None


def _threshold(value: object, anchors: set[Decimal]) -> Decimal:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise Rejected(f"threshold must be a number, got {value!r}")
    try:
        threshold = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation as exc:
        raise Rejected(f"threshold {value!r} is not a number") from exc
    if threshold <= 0:
        raise Rejected(f"threshold {threshold} is not positive")
    if threshold not in anchors:
        # The clause is the only authority for its own figures.
        raise Rejected(f"threshold {threshold} does not occur in the clause text")
    return threshold


def _comparator(value: object) -> Comparator:
    try:
        return Comparator(value)
    except ValueError as exc:
        raise Rejected(f"unknown comparator {value!r}") from exc


def _unit(value: object) -> Unit:
    try:
        return Unit(value)
    except ValueError as exc:
        raise Rejected(f"unknown unit {value!r}") from exc


def _expression(node: object, depth: int = 0, budget: list[int] | None = None) -> Expression:
    if budget is None:
        budget = [MAX_EXPRESSION_NODES]
    if depth > MAX_EXPRESSION_DEPTH:
        raise Rejected("expression nests too deeply")
    budget[0] -= 1
    if budget[0] < 0:
        raise Rejected("expression has too many nodes")
    if not isinstance(node, dict):
        raise Rejected(f"expression node must be an object, got {node!r}")

    kind = node.get("type")

    if kind == "metric":
        try:
            metric = Metric(node.get("name"))
        except ValueError as exc:
            raise Rejected(f"unknown metric {node.get('name')!r}") from exc
        return MetricLeaf(
            metric=metric,
            scope=_scope(node.get("scope")),
            period=_period(node.get("period")),
        )

    if kind == "ebitda":
        adjusted = node.get("adjusted", False)
        if not isinstance(adjusted, bool):
            raise Rejected(f"ebitda.adjusted must be a boolean, got {adjusted!r}")
        return Ebitda(scope=_scope(node.get("scope")), adjusted=adjusted)

    if kind in {"add", "max"}:
        operands = node.get("operands")
        if not isinstance(operands, list) or len(operands) < 2:
            raise Rejected(f"{kind} needs at least two operands")
        parsed = tuple(_expression(item, depth + 1, budget) for item in operands)
        return Add(parsed) if kind == "add" else Max(parsed)

    if kind == "subtract":
        return Subtract(
            minuend=_expression(node.get("minuend"), depth + 1, budget),
            subtrahend=_expression(node.get("subtrahend"), depth + 1, budget),
        )

    if kind == "ratio":
        return Ratio(
            numerator=_expression(node.get("numerator"), depth + 1, budget),
            denominator=_expression(node.get("denominator"), depth + 1, budget),
        )

    raise Rejected(f"unknown expression node type {kind!r}")


def _scope(value: object) -> Scope:
    if value is None:
        return Scope.BORROWER
    try:
        return Scope(value)
    except ValueError as exc:
        raise Rejected(f"unknown scope {value!r}") from exc


def _period(value: object) -> Period:
    if value is None:
        return Period.FULL
    try:
        return Period(value)
    except ValueError as exc:
        raise Rejected(f"unknown period {value!r}") from exc
