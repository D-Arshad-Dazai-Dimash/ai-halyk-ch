"""Ledger row model. See DATA_MODEL.md Transaction entity."""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

TXN_ID_SCENARIO_PATTERN = re.compile(r"^TXN-([A-Z0-9]+)-(\d+)$")


class RawTransaction(BaseModel):
    """A single row of the ledger CSV, lightly typed but unmodified.

    ``amount`` keeps the ledger's own sign convention (expenses negative,
    receipts positive) and is a ``Decimal`` so money never passes through
    binary floating point. Normalization (abs, FX) happens downstream in
    ledger/normalize.py, never here, so this model stays a faithful,
    replayable record of the source file.

    ``amount`` is ``None`` when the source cell is blank. The corpus
    deliberately omits amounts for transactions whose value is stated only
    in an authoritative document (a treasury memo or the audited notes);
    such a row is retained as unresolved rather than dropped or silently
    treated as zero, because dropping it would understate a covenant metric
    that the document says must include it.
    """

    model_config = ConfigDict(frozen=True)

    txn_id: str
    date: str  # ISO YYYY-MM-DD, kept as str; parsed by callers that need date arithmetic
    account_id: str
    counterparty: str
    description: str
    amount: Decimal | None
    currency: str

    @field_validator("txn_id")
    @classmethod
    def _txn_id_must_match_scenario_pattern(cls, v: str) -> str:
        if not TXN_ID_SCENARIO_PATTERN.match(v):
            raise ValueError(f"txn_id {v!r} does not match TXN-<SCENARIO>-<NNNN> pattern")
        return v

    @property
    def scenario_id(self) -> str:
        """The scenario/borrower prefix encoded in the transaction id.

        This is the ledger's own documented convention: every txn_id begins
        with the scenario_id of the borrower on whose account it sits.
        Purely mechanical -- generalizes to any scenario naming scheme
        without modification.
        """
        match = TXN_ID_SCENARIO_PATTERN.match(self.txn_id)
        assert match is not None  # guaranteed by the validator above
        return match.group(1)

    @property
    def amount_is_unresolved(self) -> bool:
        return self.amount is None
