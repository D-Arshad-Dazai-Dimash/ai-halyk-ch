"""Decisive-transaction search.

Evidence is the single transaction whose corrected treatment changes the
verdict -- not the largest contributor and not the last one before period
end. Each candidate is re-evaluated counterfactually against the full
covenant expression, so a transaction only qualifies when removing it
actually flips compliance.

When a covenant is breached but no single removal flips it (many
transactions each push it over, or the metric comes from a disclosed
figure with no ledger row), the answer is ``None``: the outcome is
aggregate, and naming one row would misstate why.
"""

from __future__ import annotations

from copy import copy
from decimal import Decimal

from covenant_agent.calc.engine import (
    Contribution,
    MetricTotals,
    applies,
    decide,
    evaluate,
)
from covenant_agent.covenants.ast import Covenant, metrics_in


def _without(totals: MetricTotals, txn_id: str) -> MetricTotals:
    counterfactual = copy(totals)
    counterfactual.contributions = [
        item for item in totals.contributions if item.txn_id != txn_id
    ]
    return counterfactual


def find_decisive(
    covenant: Covenant, totals: MetricTotals, baseline_compliant: bool
) -> str | None:
    """Return the unique transaction that determines the verdict, or None."""
    relevant = metrics_in(covenant.expression)
    candidates: list[Contribution] = [
        item for item in totals.contributions if item.metric in relevant
    ]
    if not candidates:
        return None

    flipping: list[str] = []
    for candidate in candidates:
        counterfactual = _without(totals, candidate.txn_id)
        if not applies(covenant, counterfactual):
            # Removing it switches the covenant off entirely, which is a
            # change of applicability rather than of compliance.
            continue
        value = evaluate(covenant.expression, counterfactual)
        if value is None:
            continue
        if decide(covenant, value).compliant != baseline_compliant:
            flipping.append(candidate.txn_id)

    unique = sorted(set(flipping))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        return None

    # Several transactions each move the metric across the threshold, so
    # size alone cannot single one out. The decisive one is the transaction
    # an authority actually corrected -- reclassified to another caption, or
    # given the amount the ledger export omitted -- because that correction
    # is what changed the treatment and produced the verdict. Absent such a
    # correction the outcome is genuinely aggregate and evidence stays None.
    corrected = [txn_id for txn_id in unique if txn_id in totals.adjusted_txn_ids]
    return corrected[0] if len(corrected) == 1 else None
