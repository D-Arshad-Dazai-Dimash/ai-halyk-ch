"""EUR -> USD conversion (PLAN.md "FX Recovery", production-safe).

Production priority order:
  1. A rate stated explicitly in a scenario's own documents (credit
     agreement, financial statement notes) -- searched deterministically.
  2. A documented, explicit configuration default.

Fitting this rate to the test-only answer key is a calibration-only
diagnostic (calibration/) that checks the configured rate; it must never be
the production source (THREAT_MODEL.md #9, PLAN.md open question #1).
"""

from __future__ import annotations

import re
from decimal import Decimal

# Explicit rate patterns such as "1 EUR = 1.0800 USD", "EUR/USD 1.08",
# "курс ... 1.08". Deliberately conservative (few false positives) --
# absence of a match just means "no in-corpus rate found", not an error.
_RATE_PATTERNS = (
    re.compile(r"1\s*EUR\s*=\s*([\d.]+)\s*USD", re.IGNORECASE),
    re.compile(r"EUR\s*/\s*USD\D{0,10}([\d.]{3,6})"),
    re.compile(r"курс[^\d]{0,30}([\d.]{3,6})\s*(?:USD|доллар)", re.IGNORECASE),
)

# Documented configuration default. Financial-statement notes in this
# corpus state the accounting *policy* (standard IFRS boilerplate): foreign-
# currency transactions are translated "at the rates published by the
# National Bank [of Kazakhstan] on the corresponding settlement dates" --
# i.e. a historical daily rate per transaction, not a single fixed rate.
# No such daily rate table is embedded anywhere in the offline dataset, and
# fetching historical NBK rates would violate the offline constraint, so a
# single representative rate is used as a documented approximation (PLAN.md
# open question #1). Override via resolve_eur_to_usd_rate(..., default=...)
# if a better source becomes available.
DEFAULT_EUR_TO_USD_RATE = 1.08


def find_explicit_rate_in_text(text: str) -> float | None:
    for pattern in _RATE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def resolve_eur_to_usd_rate(
    scenario_document_texts: list[str], default: float = DEFAULT_EUR_TO_USD_RATE
) -> float:
    """Search the scenario's own documents for an explicit rate first;
    fall back to the documented default. Never reads the test-only answer key.
    """
    for text in scenario_document_texts:
        rate = find_explicit_rate_in_text(text)
        if rate is not None:
            return rate
    return default


def as_decimal(value: Decimal | float | int | str) -> Decimal:
    """Coerce a numeric to Decimal without going through binary float.

    Money crosses this boundary from several callers, some of which still
    hold plain numbers; converting via ``str`` keeps the written value
    exact instead of inheriting a float's representation error.
    """
    return value if isinstance(value, Decimal) else Decimal(str(value))


def convert_to_usd(
    amount: Decimal | float, currency: str, eur_to_usd_rate: Decimal | float | str
) -> Decimal:
    """Convert to USD in exact decimal arithmetic."""
    money = as_decimal(amount)
    if currency == "USD":
        return money
    if currency == "EUR":
        return money * as_decimal(eur_to_usd_rate)
    raise ValueError(f"unsupported currency: {currency!r}")
