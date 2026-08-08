"""Accounting category taxonomy + classifier (DATA_MODEL.md "Category
taxonomy" section).

Categories are derived from general accounting semantics and the actual
vocabulary used in transaction descriptions -- not fitted to the 36 public
GT cells (which only ever validate coverage, in calibration/, never select
these rules). Classification is rule-first (deterministic, auditable) with
an LLM fallback for residual/ambiguous rows; the LLM never invents a new
category outside this fixed taxonomy, and never touches the amount.
"""

from __future__ import annotations

import re
from enum import Enum

from covenant_agent.llm.client import extract_structured


class Category(str, Enum):
    # operating
    PAYROLL = "operating.payroll"
    TAXES = "operating.taxes"
    UTILITIES = "operating.utilities"
    INSURANCE = "operating.insurance"
    RENT_LEASE = "operating.rent_lease"
    MARKETING = "operating.marketing"
    PROFESSIONAL_SERVICES = "operating.professional_services"
    MAINTENANCE = "operating.maintenance"
    # financing
    INTEREST = "financing.interest"
    LOAN_PRINCIPAL = "financing.loan_principal"
    RESTRICTED_PAYMENTS = "financing.restricted_payments"
    SUBORDINATED = "financing.subordinated"
    # investing
    CAPEX = "investing.capex"
    DISPOSALS = "investing.disposals"
    # adjustments
    REFUNDS_CREDITS = "adjustments.refunds_credits"
    CORRECTIONS = "adjustments.corrections"
    RELATED_PARTY = "adjustments.related_party"
    # revenue
    SALES_SETTLEMENT = "revenue.sales_settlement"
    REBATES_RECEIVED = "revenue.rebates_received"
    DEPOSITS_RETURNED = "revenue.deposits_returned"
    # fallback
    UNCATEGORIZED = "uncategorized"


# Category groupings used as a ledger-aggregate fallback for
# StatementFigureSource("revenue"/"operating_expenses") when a scenario has
# no dedicated financial-statement document (calc/engine_a.py). Not every
# borrower in the corpus has one; where it's absent, "revenue" is read as
# "amounts booked under the revenue line items" and "operating expenses" as
# "amounts booked under the operating expense line items" -- the ledger
# itself, since it is the underlying transaction record.
REVENUE_CATEGORIES = frozenset(
    # Deposit refunds/returns are balance-sheet reversals of amounts
    # previously paid out, not revenue from contracts with customers
    # (IFRS 15) -- deliberately excluded even though they're cash inflows.
    {Category.SALES_SETTLEMENT, Category.REBATES_RECEIVED}
)
OPERATING_EXPENSE_CATEGORIES = frozenset(
    {
        Category.PAYROLL,
        Category.TAXES,
        Category.UTILITIES,
        Category.INSURANCE,
        Category.RENT_LEASE,
        Category.MARKETING,
        Category.PROFESSIONAL_SERVICES,
        Category.MAINTENANCE,
    }
)


# Ordered (priority-first) keyword rules. Each entry: (category, patterns).
# Patterns are matched against the lowercased description; the first
# matching rule wins, so more specific phrases are listed before more
# generic single-word rules.
_RULES: list[tuple[Category, tuple[str, ...]]] = [
    (Category.RESTRICTED_PAYMENTS, ("dividend", "buyback", "share repurchase", "distribution to shareholder")),
    (Category.SUBORDINATED, ("subordinated",)),
    (Category.LOAN_PRINCIPAL, ("principal repayment", "loan principal", "principal instalment")),
    (Category.INTEREST, ("interest",)),
    (Category.SALES_SETTLEMENT, ("sales settlement", "capacity availability sales", "revenue settlement")),
    (Category.REBATES_RECEIVED, ("rebate received", "rebate", "volume rebate")),
    (Category.DEPOSITS_RETURNED, ("deposit returned", "deposit released", "deposit refunded")),
    (Category.REFUNDS_CREDITS, ("refund", "overpayment", "overbilling", "credit received", "correction credit", "reversal")),
    (Category.CAPEX, ("purchase of", "capital expenditure", "capex", "construction of", "acquisition of equipment")),
    (Category.DISPOSALS, ("disposal of", "sale of asset", "sale of equipment")),
    (Category.PAYROLL, ("payroll", "wages", "salaries", "staff pay")),
    (Category.TAXES, ("tax", "excise", "customs duty", "levy", "withholding")),
    (Category.INSURANCE, ("insurance", "premium")),
    (Category.UTILITIES, ("electricity", "utility", "water charge", "telecom", "metering")),
    (Category.RENT_LEASE, ("rent", "lease")),
    (Category.MARKETING, ("marketing", "advertis", "campaign", "media buy", "newsletter", "sponsorship")),
    (Category.MAINTENANCE, ("maintenance", "servicing", "repair", "upkeep")),
    (Category.PROFESSIONAL_SERVICES, ("consulting", "advisory", "professional service", "legal fee", "audit fee")),
]


def classify_by_rules(description: str) -> Category:
    text = description.lower()
    for category, patterns in _RULES:
        if any(re.search(re.escape(p), text) for p in patterns):
            return category
    return Category.UNCATEGORIZED


_LLM_TASK = (
    "Classify the transaction description into exactly one of the "
    "following categories (respond with the category's string value "
    "exactly as given, in field 'category'; if genuinely none fit, use "
    "'uncategorized'):\n" + "\n".join(f"- {c.value}" for c in Category)
)


def classify_residual_with_llm(description: str) -> Category:
    """Only called for rows classify_by_rules left UNCATEGORIZED. The LLM
    picks a label from the fixed enum; it never computes or touches the
    transaction amount (computation-boundary rule, PLAN.md).
    """
    result = extract_structured(
        task_instructions=_LLM_TASK,
        untrusted_document_text=description,
        schema_description='{"category": <one of the listed category strings>}',
    )
    raw = str(result.get("category", "")).strip()
    try:
        return Category(raw)
    except ValueError:
        return Category.UNCATEGORIZED
