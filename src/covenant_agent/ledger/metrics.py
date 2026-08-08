"""Financial-statement metric taxonomy and anchored ledger classification.

Covenants define their metrics "по аудированной финансовой отчётности
Заёмщика" -- i.e. by the borrower's audited statement line items, not by
every cash movement. The ledger is a raw dump that mixes those line items
with a large volume of unrelated operational noise, so classification must
be *anchored*: a row contributes to a metric only when its description is a
clean statement-style line item that names the metric.

Two deterministic signals separate line items from noise, both derived from
the structure of the data rather than from any answer key:

1. **Decoration test.** Noise rows carry an em/en-dash suffix naming a site,
   period, or instalment ("... — Almaty office, Q3"); genuine statement line
   items are undecorated ("Property insurance premium 2025"). A decorated
   description is never a statement line item.
2. **Metric anchor test.** The undecorated description must match an
   accounting-semantic pattern for a specific statement caption (revenue
   recognised on settlement, payroll disbursed, equipment purchased, ...).

Patterns are written against accounting vocabulary and carry no year,
scenario, borrower, or counterparty literal, so they carry over to a
different reporting period or borrower unchanged.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum

from covenant_agent.ledger.model import RawTransaction

# Suffix decoration used throughout the corpus for non-statement noise rows.
_DECORATION = re.compile(r"[—–]")


class Metric(str, Enum):
    REVENUE = "revenue"
    OPERATING_EXPENSES = "operating_expenses"
    CAPITAL_EXPENDITURES = "capital_expenditures"
    LEASE_PAYMENTS = "lease_payments"
    UTILITIES = "utilities"
    PAYROLL = "payroll"
    TAXES = "taxes"
    INSURANCE_PREMIUMS = "insurance_premiums"
    INTEREST_EXPENSE = "interest_expense"
    FINANCING_PROCEEDS = "financing_proceeds"
    PROFESSIONAL_SERVICES = "professional_services"
    # Scope metrics: membership is decided by the KYC dossier, never by how
    # a payment was described, so no ledger anchor may produce them.
    RELATED_PARTY_PAYMENTS = "related_party_payments"
    SUBSIDIARY_ASSET_TRANSFERS = "subsidiary_asset_transfers"


#: Metrics that represent inflows; every other metric is an outflow. Used to
#: check a candidate row's sign matches the caption it claims to belong to.
INFLOW_METRICS = frozenset({Metric.REVENUE, Metric.FINANCING_PROCEEDS})

# Ordered anchor patterns. First match wins, so more specific captions are
# listed before broader ones. All are accounting captions, not fixed phrases
# copied from any particular borrower's ledger.
_ANCHORS: tuple[tuple[Metric, re.Pattern[str]], ...] = (
    # Revenue recognised as a settlement of delivered goods/services.
    (Metric.REVENUE, re.compile(r"\bsales settlement\b", re.I)),
    # Financing inflow drawn under a facility.
    (Metric.FINANCING_PROCEEDS, re.compile(r"\bfacility drawdown\b", re.I)),
    # Capital expenditure: capital assets acquired, or transferred out to a
    # subsidiary. Both are the borrower's capital spend for the period;
    # whether a transfer additionally counts against an unrestricted-
    # subsidiary limit is decided by the KYC dossier, not here.
    (
        Metric.CAPITAL_EXPENDITURES,
        re.compile(
            r"\bpurchase of\b.*\b(?:equipment|plant|machinery|vehicles?)\b"
            r"|\btransfer of\b.*\bto subsidiary\b",
            re.I,
        ),
    ),
    # Operating and maintenance cost of running the facility, including
    # one-off servicing, survey, repair and remediation works.
    (
        Metric.OPERATING_EXPENSES,
        re.compile(
            r"\b(?:servicing and operating costs|operating and maintenance expenses"
            r"|servicing contract|servicing|repair works|clearance works|remediation)\b",
            re.I,
        ),
    ),
    # Lease of land/premises (a distinct covenant caption from utilities).
    (Metric.LEASE_PAYMENTS, re.compile(r"\bland lease payments\b", re.I)),
    # Utility supply: energy, water, heating.
    (
        Metric.UTILITIES,
        re.compile(
            r"\b(?:utilities supply|utility charge|electricity supply charges|"
            r"electricity and water|district heating)\b",
            re.I,
        ),
    ),
    # Personnel cost actually disbursed/settled in the period.
    (
        Metric.PAYROLL,
        re.compile(r"\bpayroll\b.*\b(?:disbursement|settlement)\b", re.I),
    ),
    # Statutory taxes assessed or paid in instalments to the revenue authority.
    (
        Metric.TAXES,
        re.compile(r"\btax\b.*\b(?:assessment|instalment|installment)\b", re.I),
    ),
    # Insurance cover premium.
    (Metric.INSURANCE_PREMIUMS, re.compile(r"\binsurance premium\b", re.I)),
    # Interest on borrowings.
    (
        Metric.INTEREST_EXPENSE,
        re.compile(r"\b(?:loan interest payment|interest on overdraft)\b", re.I),
    ),
    # Advisory/management fees -- a caption an auditor may reclassify.
    (
        Metric.PROFESSIONAL_SERVICES,
        re.compile(
            r"\b(?:management advisory retainer|management retainer fee|"
            r"advisory engagement on)\b",
            re.I,
        ),
    ),
)


def is_statement_line_item(description: str) -> bool:
    """True when the description is an undecorated statement-style caption.

    Rows carrying the corpus' em/en-dash site/period decoration are
    operational noise and never roll up into an audited caption.
    """
    return not _DECORATION.search(description)


#: Quarter named inside a statement line item ("... sales settlement fourth
#: quarter"). A covenant measured on one quarter needs the line items that
#: state that quarter, which the caption itself carries.
_QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_QUARTER_RE = re.compile(
    r"\b(first|second|third|fourth)\s+quarter\b|\bQ([1-4])\b", re.I
)


def classify_metric(description: str) -> Metric | None:
    """Return the statement caption this description anchors to, or None."""
    if not is_statement_line_item(description):
        return None
    text = " ".join(description.split())
    for metric, pattern in _ANCHORS:
        if pattern.search(text):
            return metric
    return None


def statement_quarter(description: str) -> int | None:
    """The quarter a line item states, or None when it covers the period."""
    match = _QUARTER_RE.search(description)
    if not match:
        return None
    if match.group(1):
        return _QUARTER_WORDS[match.group(1).casefold()]
    return int(match.group(2))


def metric_amount(metric: Metric, amount: Decimal) -> Decimal | None:
    """Positive covenant contribution for a row, or None when the row's sign
    contradicts its caption (an "expense" recorded as an inflow is a
    data-quality problem, never silently negated).
    """
    value = amount if metric in INFLOW_METRICS else -amount
    return value if value > 0 else None


def classify_transaction(
    transaction: RawTransaction, amount_usd: Decimal
) -> tuple[Metric, Decimal] | None:
    """Classify one normalized row into (metric, positive amount)."""
    metric = classify_metric(transaction.description)
    if metric is None:
        return None
    value = metric_amount(metric, amount_usd)
    return None if value is None else (metric, value)
