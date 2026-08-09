"""Authoritative document facts.

Covenants delegate parts of their definition to other documents: the KYC
dossier decides who is a related party, the audited notes and the final
agreed-upon-procedures report decide how a transaction is classified, the
treasury memo supplies amounts missing from the ledger export. This module
turns that prose into typed facts.

Every extractor is written against the documents' own structural
conventions (a disclosure is a numbered clause naming an amount, a
counterparty, and an accounting caption) rather than against any specific
borrower's wording, and none of them carry a scenario, account, or
transaction literal.

Nothing here mutates a ledger row. Facts are applied later, by the
calculation engine, so that the same transaction and authority path stays
reproducible in an audit record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from covenant_agent.documents.ocr_text import canon, ru
from covenant_agent.ledger.metrics import Metric

MONEY = r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
# Matched against the canonical (case/script-folded) text, so the id's
# letters are lower-case here.
TXN_REF = r"(txn-[a-z0-9]+-\d+)"
_QUOTES = "«»\"“”'"


def _money(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace(" ", ""))
    except InvalidOperation:
        return None


def _flatten(text: str) -> str:
    """Collapse OCR line wrapping so a disclosure split across lines still
    matches as one sentence.
    """
    return " ".join(text.split())


def canonical_party(name: str) -> str:
    """Comparison key for counterparty names: case/punctuation-insensitive
    and ignoring a parenthesised site qualifier, so "Aktau Holdings L.L.P."
    and "Aktau Holdings LLP (Almaty office)" compare equal.
    """
    value = re.sub(r"\([^)]*\)", " ", name).casefold()
    return "".join(ch for ch in value if ch.isalnum())


#: Accounting captions as they appear in auditor prose, mapped to metrics.
#: Inflected Russian forms are matched by stem so declension does not matter.
_CAPTION_STEMS: tuple[tuple[str, Metric], ...] = (
    ("процентн", Metric.INTEREST_EXPENSE),
    ("страхов", Metric.INSURANCE_PREMIUMS),
    ("капитальн", Metric.CAPITAL_EXPENDITURES),
    ("операционн", Metric.OPERATING_EXPENSES),
    ("выручк", Metric.REVENUE),
    ("аренд", Metric.LEASE_PAYMENTS),
    ("коммунальн", Metric.UTILITIES),
    ("оплату труда", Metric.PAYROLL),
    ("налог", Metric.TAXES),
    ("консультацион", Metric.PROFESSIONAL_SERVICES),
)


def caption_to_metric(caption: str) -> Metric | None:
    """Map an auditor's caption to a metric, tolerant of OCR script swaps."""
    folded = canon(caption)
    for stem, metric in _CAPTION_STEMS:
        if canon(stem) in folded:
            return metric
    return None


@dataclass(frozen=True)
class Reclassification:
    """An auditor moved an amount from one statement caption to another."""

    amount: Decimal
    counterparty: str | None
    txn_id: str | None
    from_metric: Metric | None
    to_metric: Metric | None
    source_document: str


@dataclass(frozen=True)
class OffLedgerAmount:
    """An obligation disclosed in the notes that has no ledger row at all."""

    amount: Decimal
    metric: Metric
    source_document: str


@dataclass(frozen=True)
class ResolvedAmount:
    """The true value of a ledger row whose amount cell was left blank."""

    txn_id: str
    amount: Decimal
    is_outflow: bool
    source_document: str


@dataclass(frozen=True)
class FxSettlement:
    """A disclosed foreign-invoice / USD-settlement pair, which fixes the
    rate for that counterparty's foreign-currency rows.
    """

    counterparty: str
    foreign_amount: Decimal
    foreign_currency: str
    usd_amount: Decimal
    source_document: str

    @property
    def rate(self) -> Decimal:
        return self.usd_amount / self.foreign_amount


@dataclass(frozen=True)
class PeriodReassignment:
    """A transaction whose service period falls outside the covenant period
    and therefore does not belong to it (revenue/expense cut-off).
    """

    txn_id: str
    service_period_start: str
    service_period_end: str
    source_document: str


@dataclass(frozen=True)
class OneOffItem:
    """A non-recurring operating item the auditor identified; it is an
    operating expense and is added back to EBITDA when it meets the
    disclosed materiality floor.
    """

    counterparty: str
    amount: Decimal
    source_document: str


@dataclass(frozen=True)
class OwnershipStake:
    party: str
    percent: Decimal


@dataclass
class DocumentFacts:
    reclassifications: list[Reclassification] = field(default_factory=list)
    off_ledger: list[OffLedgerAmount] = field(default_factory=list)
    resolved_amounts: list[ResolvedAmount] = field(default_factory=list)
    fx_settlements: list[FxSettlement] = field(default_factory=list)
    period_reassignments: list[PeriodReassignment] = field(default_factory=list)
    one_off_items: list[OneOffItem] = field(default_factory=list)
    one_off_floor: Decimal | None = None
    related_parties: list[str] = field(default_factory=list)
    unrestricted_subsidiaries: list[str] = field(default_factory=list)

    def extend(self, other: DocumentFacts) -> None:
        self.reclassifications.extend(other.reclassifications)
        self.off_ledger.extend(other.off_ledger)
        self.resolved_amounts.extend(other.resolved_amounts)
        self.fx_settlements.extend(other.fx_settlements)
        self.period_reassignments.extend(other.period_reassignments)
        self.one_off_items.extend(other.one_off_items)
        if other.one_off_floor is not None:
            self.one_off_floor = other.one_off_floor
        self.related_parties.extend(other.related_parties)
        self.unrestricted_subsidiaries.extend(other.unrestricted_subsidiaries)


# --- individual extractors ---------------------------------------------------

# All patterns below run against canon(text): Russian literals are folded
# with ru() so an OCR script swap ("не" read as Latin "He") still matches,
# while ASCII regex structure is untouched.

# "Сумма в размере $X, выплаченная контрагенту NAME, первоначально учтённая
#  как CAPTION_A, переклассифицирована ... как CAPTION_B."
_RECLASS_BY_PARTY = re.compile(
    MONEY + r"[^.]{0,80}?" + ru("контрагенту") + r"\s+(.{3,80}?),\s*"
    + ru("первоначально") + r"\s+" + ru("учт") + r"\w*\s+" + ru("как") + r"\s+"
    r"([^,]{3,60}?),\s*" + ru("переклассифицирован") + r"\w*[^.]*?"
    + ru("как") + r"\s+([^.]{3,60}?)\."
)

# "Операция TXN-..., первоначально учтённая как CAPTION_A ($X),
#  переклассифицирована ... как CAPTION_B."
_RECLASS_BY_TXN = re.compile(
    TXN_REF + r",?\s*" + ru("первоначально") + r"\s+" + ru("учт") + r"\w*\s+"
    + ru("как") + r"\s+([^(]{3,60}?)\s*\(" + MONEY + r"\)[^.]*?"
    + ru("переклассифицирован") + r"\w*[^.]*?" + ru("как") + r"\s+([^.]{3,60}?)\."
)

# A proposal the auditor considered and REJECTED keeps its original caption.
_RECLASS_REJECTED = re.compile(
    TXN_REF + r"[^.]{0,400}?(?:" + ru("сохраняется") + r"|"
    + ru("не производилась") + r"|" + ru("не требуется") + r")"
)

# "совокупное обязательство по программе ... в размере $X ... не отражается
#  отдельной операцией в бухгалтерской книге"
_OFF_LEDGER = re.compile(
    ru("обязательство") + r"[^.]{0,120}?" + ru("в размере") + r"\s+" + MONEY
    + r"[^.]{0,200}?" + ru("не") + r"\s+" + ru("отражается") + r"[^.]{0,80}?"
    + ru("операцией")
)

# "Операция TXN-... (...): сумма не отражена в выгрузке реестра; фактическая
#  сумма операции составляет $X (расход)."
_RESOLVED_AMOUNT = re.compile(
    TXN_REF + r"[^.;]{0,140}?" + ru("сумма") + r"\s+" + ru("не") + r"\s+"
    + ru("отражена") + r"[^.;]{0,100}?;\s*" + ru("фактическая") + r"\s+"
    + ru("сумма") + r"\s+" + ru("операции") + r"\s+" + ru("составляет") + r"\s+"
    + MONEY + r"\s*\((" + ru("расход") + r"|" + ru("доход") + r")\)"
)

# "Расчёты с контрагентом «X»: счёт на сумму N CUR урегулирован платежом
#  в долларах США в размере $M."
_FX_SETTLEMENT = re.compile(
    ru("контрагентом") + r"\s*[" + _QUOTES + r"]\s*(.{3,80}?)\s*[" + _QUOTES + r"]\s*:?"
    r"[^.]{0,80}?" + ru("сч") + r"\w*\s+" + ru("на сумму") + r"\s+"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([a-z]{3})"
    r"[^.]{0,140}?" + ru("урегулирован") + r"\s+" + ru("платежом") + r"[^.]{0,80}?" + MONEY
)

# "Операция TXN-... (счёт-фактура от DATE) относится к услугам, оказанным
#  в период с DATE по DATE."
_PERIOD_REASSIGN = re.compile(
    TXN_REF + r"[^.]{0,140}?" + ru("относится") + r"\s+" + ru("к") + r"\s+"
    + ru("услугам") + r"[^.]{0,80}?" + ru("в период с") + r"\s+(\d{4}-\d{2}-\d{2})\s+"
    + ru("по") + r"\s+(\d{4}-\d{2}-\d{2})"
)

# "Операция TXN-..., датированная DATE, исключена из ковенантного периода."
# A cut-off stated as a direct exclusion rather than as a service period.
_PERIOD_EXCLUSION = re.compile(
    TXN_REF + r"[^.]{0,140}?" + ru("исключен") + r"\w*\s+" + ru("из")
    + r"\s+" + ru("ковенантного периода")
)

# One-off table row: "<nature> «<counterparty>» $X". Bounded so a stray
# quotation mark earlier in the document cannot swallow whole paragraphs.
_ONE_OFF_ROW = re.compile(
    r"[" + _QUOTES + r"]\s*([^" + _QUOTES + r"\n]{3,60}?)\s*[" + _QUOTES + r"]\s*" + MONEY
)

# "Разовыми ... признаются статьи в сумме не менее $X"
_ONE_OFF_FLOOR = re.compile(
    ru("разов") + r"\w*[^.]{0,140}?" + ru("не менее") + r"\s+" + MONEY
)

# The one-off table sits under its own note heading; anchor on it so only
# table rows -- not prose quotations elsewhere -- are treated as items.
_ONE_OFF_TABLE_START = re.compile(ru("корректировки") + r"\s+ebitda")

# Ownership table row: "Name 34.5%"
_OWNERSHIP_ROW = re.compile(r"^(.{3,80}?)\s+([0-9]+(?:\.[0-9]+)?)\s*%\s*$", re.MULTILINE)

# "Организации, в которых Группа владеет 20.0% и более голосующих прав,
#  признаются связанными сторонами"
_RELATED_THRESHOLD = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*%\s*" + ru("и") + r"\s+" + ru("более") + r"[^.]{0,140}?"
    + ru("связанным") + r"\w*\s+" + ru("сторон")
)

# "Контрагент «Altyn Capital L.L.P.» классифицирован как АФФИЛИРОВАННОЕ
#  ЛИЦО Заёмщика" -- the dossier states the conclusion rather than the
#  holdings it was reached from. The name is taken from the quotation marks
#  so a trailing corporate form ("L.L.P.") is not truncated by a full stop.
_DECLARED_RELATED_PARTY = re.compile(
    ru("контрагент") + r"\w*\s*[«\"']([^»\"']{2,80})[»\"']"
    r"[^.]{0,120}?"
    r"(?:" + ru("аффилирован") + r"|" + ru("связанн") + r")\w*"
)

#: Ownership thresholds are sometimes written as a fraction in words rather
#: than a percentage ("не менее одной четверти голосующих прав"). The
#: dossier means the same test either way, and a borrower whose threshold is
#: worded has no related parties at all if only digits are read.
_WORDED_FRACTIONS: tuple[tuple[str, Decimal], ...] = (
    (ru("одной десятой"), Decimal("10")),
    (ru("одной пятой"), Decimal("20")),
    (ru("одной четверти"), Decimal("25")),
    (ru("двух десятых"), Decimal("20")),
    (ru("трех десятых"), Decimal("30")),
    (ru("трёх десятых"), Decimal("30")),
    (ru("одной трети"), Decimal("33.3")),
    (ru("одной третьей"), Decimal("33.3")),
    (ru("двух пятых"), Decimal("40")),
    (ru("четырех десятых"), Decimal("40")),
    (ru("половины"), Decimal("50")),
)

_WORDED_RELATED_THRESHOLD = re.compile(
    ru("не менее") + r"\s+(" + "|".join(word for word, _ in _WORDED_FRACTIONS) + r")"
    r"[^.]{0,140}?" + ru("связанным") + r"\w*\s+" + ru("сторон")
)

#: "Доля в <A> удерживается косвенно через <B>; Группе принадлежит 26.0%
#: голосующих прав в <B>. ... учитывается эффективная доля Группы."
#: The holding in A is then the product of the two, not the stated figure.
_INDIRECT_HOLDING = re.compile(
    ru("доля") + r"\s+" + ru("в") + r"\s+(.{2,60}?)\s+"
    + ru("удерживается") + r"\s+" + ru("косвенно")
    + r"[^.]{0,200}?([0-9]+(?:\.[0-9]+)?)\s*%"
)


# "Дочерние организации, в которых доля ... ниже 50.0%, признаются
#  Неограниченными дочерними организациями"
_UNRESTRICTED_THRESHOLD = re.compile(
    ru("ниже") + r"\s+([0-9]+(?:\.[0-9]+)?)\s*%[^.]{0,180}?" + ru("неограниченн") + r"\w*"
)


_SUBSIDIARY_HEADING = re.compile(ru("дочерн") + r"\w*\s+" + ru("организац"))


def extract_ownership_facts(text: str, source: str) -> DocumentFacts:
    """Related parties and unrestricted subsidiaries, per the dossier's own
    stated percentage tests. Thresholds are read from the document rather
    than assumed, because they differ between borrowers.

    The two ownership tables share the same "Name pct%" row shape, so they
    are split on the subsidiary heading before either is read; matching runs
    on the canonical text while names are sliced from the original so their
    spelling survives.
    """
    facts = DocumentFacts()
    folded = canon(text)

    split_at = _SUBSIDIARY_HEADING.search(folded)
    boundary = split_at.start() if split_at else len(text)

    def rows(start: int, end: int) -> list[tuple[str, Decimal]]:
        found: list[tuple[str, Decimal]] = []
        for row in _OWNERSHIP_ROW.finditer(folded[start:end]):
            name = text[start + row.start(1) : start + row.end(1)].strip()
            found.append((name, Decimal(row.group(2))))
        return found

    threshold = None
    related_match = _RELATED_THRESHOLD.search(folded)
    if related_match:
        threshold = Decimal(related_match.group(1))
    else:
        worded = _WORDED_RELATED_THRESHOLD.search(folded)
        if worded:
            threshold = next(
                value for word, value in _WORDED_FRACTIONS if word == worded.group(1)
            )

    if threshold is not None:
        # An indirect holding counts at its effective size when the dossier
        # says so: a 61% stake held through a vehicle the group owns 26% of
        # is a 15.9% interest, below every threshold in the corpus.
        effective: dict[str, Decimal] = {}
        for indirect in _INDIRECT_HOLDING.finditer(folded):
            held = text[indirect.start(1) : indirect.end(1)].strip()
            effective[canon(held)] = Decimal(indirect.group(2)) / Decimal(100)

        for name, percent in rows(0, boundary):
            factor = effective.get(canon(name))
            if factor is not None:
                percent = percent * factor
            if percent >= threshold:
                facts.related_parties.append(name)

    unrestricted_match = _UNRESTRICTED_THRESHOLD.search(folded[boundary:])
    if unrestricted_match:
        threshold = Decimal(unrestricted_match.group(1))
        facts.unrestricted_subsidiaries.extend(
            name for name, percent in rows(boundary, len(text)) if percent < threshold
        )

    # Some dossiers state the conclusion instead of the arithmetic: rather
    # than a table of holdings and a percentage test, they name the
    # counterparty and record the classification reached ("Контрагент «X»
    # классифицирован как АФФИЛИРОВАННОЕ ЛИЦО"). That is the same fact, so
    # it is read the same way -- a dossier written this way otherwise
    # yields no related parties at all and the covenant measures nothing.
    for declared in _DECLARED_RELATED_PARTY.finditer(folded):
        name = text[declared.start(1) : declared.end(1)].strip()
        if name and name not in facts.related_parties:
            facts.related_parties.append(name)

    _ = source
    return facts


def extract_disclosure_facts(text: str, source: str) -> DocumentFacts:
    """Adjustments disclosed by an audited-notes, AUP, or treasury document.

    Patterns run against the canonical text (OCR script-insensitive) while
    names are sliced from the original so their spelling is preserved.
    """
    facts = DocumentFacts()
    flat = _flatten(text)
    folded = canon(flat)

    rejected = {m.group(1).upper() for m in _RECLASS_REJECTED.finditer(folded)}

    for match in _RECLASS_BY_PARTY.finditer(folded):
        amount = _money(match.group(1))
        if amount is None:
            continue
        facts.reclassifications.append(
            Reclassification(
                amount=amount,
                counterparty=flat[match.start(2) : match.end(2)].strip(" .,"),
                txn_id=None,
                from_metric=caption_to_metric(match.group(3)),
                to_metric=caption_to_metric(match.group(4)),
                source_document=source,
            )
        )

    for match in _RECLASS_BY_TXN.finditer(folded):
        txn_id = match.group(1).upper()
        amount = _money(match.group(3))
        if amount is None or txn_id in rejected:
            continue
        facts.reclassifications.append(
            Reclassification(
                amount=amount,
                counterparty=None,
                txn_id=txn_id,
                from_metric=caption_to_metric(match.group(2)),
                to_metric=caption_to_metric(match.group(4)),
                source_document=source,
            )
        )

    for match in _OFF_LEDGER.finditer(folded):
        amount = _money(match.group(1))
        if amount is not None:
            facts.off_ledger.append(
                OffLedgerAmount(
                    amount=amount, metric=Metric.PAYROLL, source_document=source
                )
            )

    outflow_marker = canon("расход")
    for match in _RESOLVED_AMOUNT.finditer(folded):
        amount = _money(match.group(2))
        if amount is not None:
            facts.resolved_amounts.append(
                ResolvedAmount(
                    txn_id=match.group(1).upper(),
                    amount=amount,
                    is_outflow=match.group(3).startswith(outflow_marker),
                    source_document=source,
                )
            )

    for match in _FX_SETTLEMENT.finditer(folded):
        foreign = _money(match.group(2)) or Decimal(0)
        usd = _money(match.group(4))
        if usd is not None and foreign > 0:
            facts.fx_settlements.append(
                FxSettlement(
                    counterparty=flat[match.start(1) : match.end(1)].strip(),
                    foreign_amount=foreign,
                    foreign_currency=flat[match.start(3) : match.end(3)].upper(),
                    usd_amount=usd,
                    source_document=source,
                )
            )

    for match in _PERIOD_REASSIGN.finditer(folded):
        facts.period_reassignments.append(
            PeriodReassignment(
                txn_id=match.group(1).upper(),
                service_period_start=match.group(2),
                service_period_end=match.group(3),
                source_document=source,
            )
        )

    for match in _PERIOD_EXCLUSION.finditer(folded):
        facts.period_reassignments.append(
            PeriodReassignment(
                txn_id=match.group(1).upper(),
                service_period_start="",
                service_period_end="",
                source_document=source,
            )
        )

    floor_match = _ONE_OFF_FLOOR.search(folded)
    if floor_match:
        facts.one_off_floor = _money(floor_match.group(1))
        # Item rows sit between the EBITDA-adjustment heading and the
        # materiality sentence; bounding on both keeps prose quotations
        # elsewhere in the document out of the table.
        heading = _ONE_OFF_TABLE_START.search(folded)
        start = heading.end() if heading and heading.end() < floor_match.start() else 0
        for row in _ONE_OFF_ROW.finditer(folded[start : floor_match.start()]):
            amount = _money(row.group(2))
            if amount is not None:
                facts.one_off_items.append(
                    OneOffItem(
                        counterparty=flat[
                            start + row.start(1) : start + row.end(1)
                        ].strip(),
                        amount=amount,
                        source_document=source,
                    )
                )

    return facts
