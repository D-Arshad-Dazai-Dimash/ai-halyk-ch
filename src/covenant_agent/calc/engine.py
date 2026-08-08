"""Deterministic covenant evaluation.

Metric totals are built once per borrower from the ledger and then adjusted
by the facts the authoritative documents disclose -- resolved amounts,
reclassifications between captions, off-ledger obligations, period
cut-offs, and disclosed FX settlements. The AST is then evaluated with
``Decimal`` throughout, so no covenant number ever passes through binary
floating point.

Nothing here reads a document or calls a model: it consumes typed inputs
only, which keeps every reported figure reproducible from the audit record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from covenant_agent.covenants.ast import (
    Add,
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
)
from covenant_agent.documents.facts import DocumentFacts, canonical_party
from covenant_agent.ledger.metrics import (
    Metric,
    classify_metric,
    metric_amount,
    statement_quarter,
)
from covenant_agent.ledger.model import RawTransaction

CENTS = Decimal("0.01")
#: The official scoring tolerance. A rounding decision that moves the
#: reported value by less than this is immaterial to the submitted answer.
MATERIALITY = Decimal("0.05")


@dataclass
class Contribution:
    txn_id: str
    metric: Metric
    amount: Decimal
    rule: str
    scope: Scope = Scope.BORROWER
    quarter: int | None = None


@dataclass
class MetricTotals:
    """Per-metric totals plus the contributions that produced them."""

    contributions: list[Contribution] = field(default_factory=list)
    off_ledger: dict[Metric, Decimal] = field(default_factory=dict)
    one_off_addbacks: Decimal = Decimal(0)
    #: Transactions whose treatment an authority document corrected --
    #: reclassified to another caption or given an amount the export
    #: omitted. These are the corrections a covenant breach can turn on.
    adjusted_txn_ids: set[str] = field(default_factory=set)

    def total(self, metric: Metric, scope: Scope = Scope.BORROWER) -> Decimal:
        value = sum(
            (c.amount for c in self.contributions if c.metric is metric and c.scope is scope),
            start=Decimal(0),
        )
        if scope is Scope.BORROWER:
            value += self.off_ledger.get(metric, Decimal(0))
        return value

    def txns_for(self, metric: Metric, scope: Scope = Scope.BORROWER) -> list[Contribution]:
        return [c for c in self.contributions if c.metric is metric and c.scope is scope]


#: Corporate-form markers identifying a counterparty as a group entity.
_GROUP_ENTITY_MARKERS = ("group", "holdings")
#: Captions that are never capital expenditure, whoever they are paid to.
_NON_CAPITAL_MARKERS = ("insurance",)


def _is_group_entity(counterparty: str) -> bool:
    words = set(re.findall(r"[a-z]+", counterparty.casefold()))
    return bool(words.intersection(_GROUP_ENTITY_MARKERS))


def _group_capital_spend(transaction: RawTransaction, amount: Decimal) -> Decimal | None:
    """Group-consolidated capital spend attributable to one ledger row.

    A group-scope covenant measures "capital expenditure of all Group
    participants per the ultimate parent's consolidated statements". This
    corpus contains no consolidated parent statement, so the figure cannot
    be read from a document and is approximated from the ledger: outflows
    to counterparties carrying a group corporate-form marker, less captions
    that are not capital in nature.

    This is the weakest inference in the system -- it leans on counterparty
    naming rather than on a disclosure -- and is deliberately confined to
    the group scope, which nothing else uses. A holdout whose consolidated
    figure is actually disclosed should read it from that document instead.
    """
    if amount >= 0 or not _is_group_entity(transaction.counterparty):
        return None
    caption = transaction.description.casefold()
    if any(marker in caption for marker in _NON_CAPITAL_MARKERS):
        return None
    return -amount


def _usd(transaction: RawTransaction, amount: Decimal, facts: DocumentFacts) -> Decimal:
    """Convert to USD using a rate the auditor actually disclosed for that
    counterparty; a foreign amount with no disclosed settlement stays
    unconverted rather than being guessed at a market rate.
    """
    if transaction.currency == "USD":
        return amount
    party = canonical_party(transaction.counterparty)
    for settlement in facts.fx_settlements:
        if (
            settlement.foreign_currency == transaction.currency
            and canonical_party(settlement.counterparty) == party
        ):
            return amount * settlement.rate
    return amount


def build_totals(
    transactions: list[RawTransaction],
    facts: DocumentFacts,
    period_start: str,
    period_end: str,
) -> MetricTotals:
    """Classify a borrower's ledger into metric totals, applying every
    document-disclosed adjustment.
    """
    totals = MetricTotals()

    resolved = {item.txn_id: item for item in facts.resolved_amounts}
    excluded = {item.txn_id for item in facts.period_reassignments}
    related = {canonical_party(name) for name in facts.related_parties}
    unrestricted = {canonical_party(name) for name in facts.unrestricted_subsidiaries}
    # One-off items are matched to their ledger row by amount rather than by
    # counterparty: the disclosure table's names come through OCR badly
    # damaged, while the stated figures are exact.
    one_off_amounts = {item.amount for item in facts.one_off_items}

    reclass_by_txn = {r.txn_id: r for r in facts.reclassifications if r.txn_id}
    reclass_by_party = {
        canonical_party(r.counterparty): r for r in facts.reclassifications if r.counterparty
    }

    for transaction in transactions:
        if not (period_start <= transaction.date <= period_end):
            continue
        # A service period the auditor placed outside the covenant period
        # does not belong to it, regardless of when it was invoiced.
        if transaction.txn_id in excluded:
            continue

        amount = transaction.amount
        if amount is None:
            supplied = resolved.get(transaction.txn_id)
            if supplied is None:
                continue  # unresolved: never treated as zero, just not counted
            amount = -supplied.amount if supplied.is_outflow else supplied.amount
            totals.adjusted_txn_ids.add(transaction.txn_id)

        amount = _usd(transaction, amount, facts)
        party = canonical_party(transaction.counterparty)

        metric = classify_metric(transaction.description)
        # An auditor's reclassification overrides the booked caption.
        reclass = reclass_by_txn.get(transaction.txn_id) or reclass_by_party.get(party)
        if reclass is not None and reclass.to_metric is not None:
            metric = reclass.to_metric
            totals.adjusted_txn_ids.add(transaction.txn_id)
        # A one-off item identified by the auditor is an operating cost.
        elif metric is None and abs(amount) in one_off_amounts:
            metric = Metric.OPERATING_EXPENSES

        if metric is not None:
            value = metric_amount(metric, amount)
            if value is not None:
                totals.contributions.append(
                    Contribution(
                        transaction.txn_id,
                        metric,
                        value,
                        "ledger_anchor",
                        quarter=statement_quarter(transaction.description),
                    )
                )

        # Related-party and unrestricted-subsidiary scopes are decided by
        # the KYC dossier, not by how the payment was described.
        if amount < 0 and party in related:
            totals.contributions.append(
                Contribution(
                    transaction.txn_id,
                    Metric.RELATED_PARTY_PAYMENTS,
                    -amount,
                    "kyc_related_party",
                )
            )
        group_spend = _group_capital_spend(transaction, amount)
        if group_spend is not None:
            totals.contributions.append(
                Contribution(
                    transaction.txn_id,
                    Metric.CAPITAL_EXPENDITURES,
                    group_spend,
                    "group_entity_capital_spend",
                    scope=Scope.GROUP,
                )
            )

        if amount < 0 and party in unrestricted:
            totals.contributions.append(
                Contribution(
                    transaction.txn_id,
                    Metric.SUBSIDIARY_ASSET_TRANSFERS,
                    -amount,
                    "kyc_unrestricted_subsidiary",
                )
            )

    for item in facts.off_ledger:
        totals.off_ledger[item.metric] = totals.off_ledger.get(item.metric, Decimal(0)) + item.amount

    floor = facts.one_off_floor
    if floor is not None:
        totals.one_off_addbacks = sum(
            (item.amount for item in facts.one_off_items if item.amount >= floor),
            start=Decimal(0),
        )

    return totals


def evaluate(expression: Expression, totals: MetricTotals) -> Decimal | None:
    """Evaluate an AST node exactly, or None when it is undefined."""
    if isinstance(expression, MetricLeaf):
        if expression.period is Period.FOURTH_QUARTER:
            return _quarter_total(expression.metric, totals)
        return totals.total(expression.metric, expression.scope)

    if isinstance(expression, Ebitda):
        base = totals.total(Metric.REVENUE, expression.scope) - totals.total(
            Metric.OPERATING_EXPENSES, expression.scope
        )
        return base + totals.one_off_addbacks if expression.adjusted else base

    if isinstance(expression, Add):
        parts = [evaluate(item, totals) for item in expression.operands]
        return None if any(p is None for p in parts) else sum(parts, start=Decimal(0))

    if isinstance(expression, Max):
        parts = [evaluate(item, totals) for item in expression.operands]
        return None if any(p is None for p in parts) else max(parts)

    if isinstance(expression, Subtract):
        left = evaluate(expression.minuend, totals)
        right = evaluate(expression.subtrahend, totals)
        return None if left is None or right is None else left - right

    if isinstance(expression, Ratio):
        numerator = evaluate(expression.numerator, totals)
        denominator = evaluate(expression.denominator, totals)
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator / denominator

    raise TypeError(f"unsupported expression node: {type(expression).__name__}")


def _quarter_total(metric: Metric, totals: MetricTotals, quarter: int = 4) -> Decimal:
    """Total of the line items that state the given quarter. The quarter is
    carried by the caption itself, so a covenant measured on one quarter
    picks up exactly the items recognised in it.
    """
    return sum(
        (
            c.amount
            for c in totals.contributions
            if c.metric is metric and c.quarter == quarter
        ),
        start=Decimal(0),
    )


_COMPARATORS = {
    Comparator.LE: lambda actual, limit: actual <= limit,
    Comparator.GE: lambda actual, limit: actual >= limit,
    Comparator.LT: lambda actual, limit: actual < limit,
    Comparator.GT: lambda actual, limit: actual > limit,
}


def round_reported(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Verdict:
    raw: Decimal
    reported: Decimal
    compliant: bool
    used_raw_basis: bool


def decide(covenant: Covenant, raw: Decimal) -> Verdict:
    """Compare against the threshold, reconciling exact and reported values.

    The submitted figure is rounded to cents, so the exact and rounded
    values can fall on opposite sides of a threshold. The reported value
    governs -- it is what the answer states -- unless rounding moved the
    figure by at least the official tolerance, in which case the rounding is
    material and the exact comparison stands. Both are recorded either way.
    """
    reported = round_reported(raw)
    compare = _COMPARATORS[covenant.comparator]
    raw_verdict = compare(raw, covenant.threshold)
    reported_verdict = compare(reported, covenant.threshold)

    if raw_verdict == reported_verdict:
        return Verdict(raw, reported, raw_verdict, used_raw_basis=False)

    deviation = abs(raw - reported) / abs(raw) if raw else Decimal(0)
    if deviation >= MATERIALITY:
        return Verdict(raw, reported, raw_verdict, used_raw_basis=True)
    return Verdict(raw, reported, reported_verdict, used_raw_basis=False)


def applies(covenant: Covenant, totals: MetricTotals) -> bool:
    """Whether a springing covenant's trigger condition is met."""
    condition = covenant.applicability
    if condition is None:
        return True
    value = evaluate(condition.expression, totals)
    if value is None:
        return False
    return _COMPARATORS[condition.comparator](value, condition.threshold)
