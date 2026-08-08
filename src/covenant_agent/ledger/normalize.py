"""Transaction normalization: sign/abs, FX->USD, category classification.
See DATA_MODEL.md Transaction entity.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from covenant_agent.ledger.categories import Category, classify_by_rules, classify_residual_with_llm
from covenant_agent.ledger.fx import convert_to_usd
from covenant_agent.ledger.model import RawTransaction


class NormalizedTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    txn_id: str
    scenario_id: str
    date: str
    account_id: str
    counterparty: str
    description: str

    amount_raw: Decimal  # ledger's original signed amount, original currency
    currency: str
    amount_usd: Decimal  # signed, FX-converted (outflow negative, inflow positive)
    amount_usd_abs: Decimal  # abs(amount_usd) -- what `actual` reporting always uses

    is_outflow: bool
    category: Category
    category_from_llm: bool  # provenance: False if a deterministic rule matched


def normalize_transaction(
    raw: RawTransaction, eur_to_usd_rate: Decimal | float, *, use_llm_fallback: bool = True
) -> NormalizedTransaction:
    amount_usd = convert_to_usd(raw.amount, raw.currency, eur_to_usd_rate)

    category = classify_by_rules(raw.description)
    from_llm = False
    if category == Category.UNCATEGORIZED and use_llm_fallback:
        category = classify_residual_with_llm(raw.description)
        from_llm = True

    return NormalizedTransaction(
        txn_id=raw.txn_id,
        scenario_id=raw.scenario_id,
        date=raw.date,
        account_id=raw.account_id,
        counterparty=raw.counterparty,
        description=raw.description,
        amount_raw=raw.amount,
        currency=raw.currency,
        amount_usd=amount_usd,
        amount_usd_abs=abs(amount_usd),
        is_outflow=amount_usd < 0,
        category=category,
        category_from_llm=from_llm,
    )


def normalize_transactions(
    transactions: list[RawTransaction], eur_to_usd_rate: Decimal | float, *, use_llm_fallback: bool = True
) -> list[NormalizedTransaction]:
    return [
        normalize_transaction(t, eur_to_usd_rate, use_llm_fallback=use_llm_fallback)
        for t in transactions
    ]
