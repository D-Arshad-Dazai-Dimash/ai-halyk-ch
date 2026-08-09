"""Deterministic covenant clause parser.

The parser is *compositional*: it locates the covenant's parts -- the
comparator, the threshold, and each metric the clause names -- and then
assembles them using the connective the clause actually uses ("отношение
... к ...", "сумме ... и ...", "за вычетом", "наибольшей из"). It does not
match whole clause sentences, so a clause that expresses the same structure
in different words still compiles.

Everything runs on the OCR-canonical form of the text (documents/ocr_text.py)
so a Cyrillic/Latin glyph swap does not change the parse. A clause whose
structure is not recognised compiles to ``None`` and is reported as
requiring review rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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
from covenant_agent.documents.ocr_text import canon, ru
from covenant_agent.ledger.metrics import Metric

MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
# A ratio multiple: "9.00x" / "0.42х" -- the trailing letter may be the
# Latin or Cyrillic glyph, both fold to "x".
RATIO_RE = re.compile(r"(?<![0-9])([0-9]+(?:\.[0-9]+)?)\s*x\b")
#: The same limit written as a percentage of another metric ("не превышали
#: 80 процентов совокупной выручки"). It is a ratio, just stated per
#: hundred, so the value is divided down rather than treated as a multiple.
PERCENT_RE = re.compile(
    r"(?<![0-9])([0-9]+(?:[.,][0-9]+)?)\s*(?:%|" + ru("процент") + r"\w*)"
)
DATE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+" + ru("по") + r"\s+(\d{4}-\d{2}-\d{2})")
#: A clause heading, with its number when the print survived OCR. The word
#: is matched with its inflection so a cross-reference ("согласно Пункту
#: 5.1") captures its number too -- without that, the number is invisible
#: and the reference is indistinguishable from a heading.
CLAUSE_HEADING_RE = re.compile(
    r"(?:" + ru("пункт") + r"\w*|section|article)[_\s]*(\d+\.\d+)?"
)
SENTENCE_END_RE = re.compile(r"\.\s")

#: Cost nouns that confirm an adjective refers to an expense caption rather
#: than to something else ("операционных расходов" is a caption;
#: "операционных записях" is a bookkeeping record). Coordinated captions
#: share one noun ("операционных и капитальных затрат"), so allow one
#: intervening conjunction plus adjective before the noun.
#: A lookahead, so a coordinated caption ("операционных и капитальных
#: затрат") lets each adjective match its own metric instead of the first
#: one swallowing the whole phrase.
_COST_NOUN = (
    r"(?=(?:\s+" + ru("и") + r"\s+\w+)?\s+(?:"
    + ru("расход") + r"|" + ru("затрат") + r"|" + ru("платеж") + r")\w*)"
)

#: Metric vocabulary, as inflection-tolerant patterns in canonical form.
#: More specific patterns are listed first so a broad stem cannot shadow a
#: narrower one.
_METRIC_TERMS: tuple[tuple[str, Metric], ...] = (
    (ru("поступлени") + r"\w*\s+" + ru("по финансированию"), Metric.FINANCING_PROCEEDS),
    (ru("финансировани") + r"\w*", Metric.FINANCING_PROCEEDS),
    # "неоперационных" is the negation of this caption, so require that the
    # stem is not preceded by the negating prefix.
    (r"(?<!" + ru("не") + r")" + ru("операционн") + r"\w*" + _COST_NOUN, Metric.OPERATING_EXPENSES),
    (ru("капитальн") + r"\w*(?:" + _COST_NOUN + r"|\s+" + ru("актив") + r"\w*)",
     Metric.CAPITAL_EXPENDITURES),
    (ru("капиталоемкости"), Metric.CAPITAL_EXPENDITURES),
    (ru("арендн") + r"\w*", Metric.LEASE_PAYMENTS),
    (ru("коммунальн") + r"\w*", Metric.UTILITIES),
    (ru("оплату труда"), Metric.PAYROLL),
    (ru("персонал") + r"\w*", Metric.PAYROLL),
    (ru("страхов") + r"\w*\s+" + ru("преми") + r"\w*", Metric.INSURANCE_PREMIUMS),
    (ru("страховых премий"), Metric.INSURANCE_PREMIUMS),
    (ru("процентн") + r"\w*" + _COST_NOUN, Metric.INTEREST_EXPENSE),
    (ru("процентов"), Metric.INTEREST_EXPENSE),
    (ru("налог") + r"\w*", Metric.TAXES),
    (ru("выручк") + r"\w*", Metric.REVENUE),
)

_RELATED_PARTY_TERMS = (ru("связанн"), ru("аффилирован"))
_UNRESTRICTED_TERMS = (ru("неограниченн"),)
_EBITDA_TERM = "ebitda"

# Connectives and qualifiers.
_RATIO_WORD = ru("отношени")  # stem: matches "отношение"/"отношения"/"отношению"
_TO_SEPARATOR = re.compile(r"\s" + ru("к") + r"\s")
_SUM_WORD = re.compile(ru("сумм") + r"\w*|" + ru("совокупн") + r"\w*\s+" + ru("величин") + r"\w*")
_LARGEST_WORD = re.compile(ru("наибольш"))
_LESS_WORD = re.compile(ru("за вычетом"))
_GROUP_WORD = re.compile(ru("группы"))
_ADJUSTED_WORD = re.compile(ru("скорректированн"))
_Q4_WORD = re.compile(ru("четверт") + r"\w*\s+(?:" + ru("финансов") + r"\w*\s+)?" + ru("квартал"))
_SPRINGING_WORD = re.compile(ru("только при условии"))
#: The other way a springing covenant is written: the trigger comes first
#: and the limit it governs follows ("Если <ratio> превышает 2.40х, то
#: <borrower> обязуется не допускать превышения ... $5,050,000.00"). Read
#: without this, the trigger's own figure is nearer the front and is taken
#: for the covenant's threshold -- a wrong number, not a failure to parse.
_IF_WORD = re.compile(ru("если"))
_THEN_SPLIT = re.compile(r",\s*" + ru("то") + r"\b")

_MIN_COMPARATOR_TERMS = (
    ru("не менее"),
    ru("не ниже"),
    ru("снижения"),
    # Some borrowers are documented entirely in English, stating the same
    # covenants with the same structure.
    "no less than",
    "not less than",
    "at least",
    "minimum",
)
_MAX_COMPARATOR_TERMS = (
    # Stems, not inflected forms. A cap is written a dozen ways -- "не
    # превышало", "не допускать превышения", "превышающую", "не превысят",
    # "допускает превышение" -- and enumerating surface forms loses the one
    # that was not anticipated. A minimum clause that mentions "превышает"
    # inside its own definition still resolves correctly, because a clause
    # carrying both directions is read as the floor it states.
    ru("превыш"),
    ru("превыс"),
    ru("не превыш"),
    ru("превышал"),
    ru("превышали"),
    ru("превысил"),
    ru("не должны превыш"),
    ru("не вправе"),
    ru("более"),
    ru("свыше"),
    # "Ограничение отношения ... величиной 1.70х" caps the metric.
    ru("ограничение"),
    ru("не должен превыш"),
    ru("потолок"),
    "shall not permit",
    "not exceed",
    "to exceed",
    "exceeds",
    "exceeding",
    "maximum",
)


@dataclass(frozen=True)
class MetricMention:
    start: int
    end: int
    expression: Expression


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace(" ", ""))
    except InvalidOperation:
        return None


def _operative_threshold_positions(folded: str) -> list[int]:
    """Positions of thresholds that are part of an operative limit.

    A figure counts only when a comparator word introduces it, which keeps
    incidental amounts inside definitions and carve-outs from being read as
    a covenant's own limit.
    """
    comparators = _MIN_COMPARATOR_TERMS + _MAX_COMPARATOR_TERMS
    positions: list[int] = []
    for match in list(MONEY_RE.finditer(folded)) + list(RATIO_RE.finditer(folded)):
        window = folded[max(0, match.start() - 140) : match.start()]
        if any(term in window for term in comparators):
            positions.append(match.start())
    return sorted(positions)


def split_clauses(
    section_text: str, article: str = "6", expected: int | None = None
) -> dict[str, str]:
    """Split a covenants article into ``{clause_number: clause_text}``.

    Each heading starts a clause and the next heading ends it, so a clause
    keeps exactly its own definitions and carve-outs -- important because
    neighbouring clauses define overlapping terms.

    OCR damages these headings in two ways: it drops the printed number
    ("Пункт Максимальные платежи..."), and it sometimes destroys the
    heading word itself. Numbers are therefore assigned positionally, the
    first clause is taken to start at the article heading, and when fewer
    clauses were found than the template expects, an over-long segment
    holding two operative limits is split at the paragraph break between
    them. Left unhandled, a lost heading silently merges two covenants and
    both parse wrongly.
    """
    folded = canon(section_text)

    boundaries: set[int] = set()
    for heading in CLAUSE_HEADING_RE.finditer(folded):
        printed = heading.group(1)
        if printed is not None and printed.split(".")[0] != article:
            # A cross-reference to another article's clause ("определяемое
            # согласно Пункту 5.1"), not a heading. Treating it as one cuts
            # the clause in half: the condition stays, the limit it governs
            # is lost, and what remains parses as a covenant with no
            # threshold at all.
            continue
        boundaries.add(heading.start())
    article_heading = re.search(ru("статья") + r"\s*" + re.escape(article), folded)
    if article_heading:
        # Start the first clause after the whole heading line; stopping just
        # past the article number would leave its title ("— Финансовые
        # ковенанты") behind as a spurious content segment.
        line_end = section_text.find("\n", article_heading.end())
        boundaries.add(line_end + 1 if line_end != -1 else article_heading.end())
    ordered = sorted(boundaries)
    if not ordered:
        return {}

    def cut(positions: list[int]) -> list[str]:
        pieces: list[str] = []
        for index, start in enumerate(positions):
            end = positions[index + 1] if index + 1 < len(positions) else len(section_text)
            piece = section_text[start:end]
            # The span between the article heading and the first clause
            # heading carries no covenant; keeping it would shift every
            # later clause number.
            if piece.strip():
                pieces.append(piece)
        return pieces

    segments = cut(ordered)
    if expected is not None and len(segments) < expected:
        segments = cut(
            _recover_missing_boundaries(section_text, folded, ordered, expected)
        )

    clauses: dict[str, str] = {}
    position = 0
    for segment in segments:
        printed = CLAUSE_HEADING_RE.search(canon(segment))
        number = printed.group(1) if printed else None
        if number:
            position = int(number.split(".")[1])
        else:
            position += 1
            number = f"{article}.{position}"
        clauses[number] = segment
    return clauses


def _recover_missing_boundaries(
    section_text: str, folded: str, ordered: list[int], expected: int
) -> list[int]:
    """Split segments that contain more than one operative limit, at the
    paragraph break separating them.
    """
    thresholds = _operative_threshold_positions(folded)
    recovered = list(ordered)

    # Blank leading spans do not carry a covenant, so target the number of
    # content-bearing segments rather than the raw boundary count.
    def content_count(positions: list[int]) -> int:
        total = 0
        for index, start in enumerate(positions):
            end = positions[index + 1] if index + 1 < len(positions) else len(section_text)
            if section_text[start:end].strip():
                total += 1
        return total

    while content_count(recovered) < expected:
        added = False
        for index, start in enumerate(recovered):
            end = recovered[index + 1] if index + 1 < len(recovered) else len(section_text)
            inside = [pos for pos in thresholds if start <= pos < end]
            if len(inside) < 2:
                continue
            gap = section_text.find("\n\n", inside[0], inside[1])
            if gap == -1:
                continue
            recovered.append(gap + 2)
            recovered.sort()
            added = True
            break
        if not added:
            break
    return recovered


def _find_mentions(folded: str) -> list[MetricMention]:
    """Locate every metric the clause names, left to right, without overlaps."""
    found: list[MetricMention] = []
    taken: list[tuple[int, int]] = []

    def claim(start: int, end: int) -> bool:
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            return False
        taken.append((start, end))
        return True

    # Restricted/related-party payments and unrestricted-subsidiary
    # transfers are named by their defining phrase rather than a caption.
    for term in _RELATED_PARTY_TERMS:
        for match in re.finditer(re.escape(term), folded):
            if claim(match.start(), match.end()):
                found.append(
                    MetricMention(
                        match.start(),
                        match.end(),
                        MetricLeaf(Metric.RELATED_PARTY_PAYMENTS),
                    )
                )
    for term in _UNRESTRICTED_TERMS:
        for match in re.finditer(re.escape(term), folded):
            if claim(match.start(), match.end()):
                found.append(
                    MetricMention(
                        match.start(),
                        match.end(),
                        MetricLeaf(Metric.SUBSIDIARY_ASSET_TRANSFERS),
                    )
                )

    for match in re.finditer(_EBITDA_TERM, folded):
        if not claim(match.start(), match.end()):
            continue
        window = folded[max(0, match.start() - 40) : match.start()]
        found.append(
            MetricMention(
                match.start(),
                match.end(),
                Ebitda(adjusted=bool(_ADJUSTED_WORD.search(window))),
            )
        )

    for pattern, metric in _METRIC_TERMS:
        for match in re.finditer(pattern, folded):
            if not claim(match.start(), match.end()):
                continue
            tail = folded[match.end() : match.end() + 30]
            scope = Scope.GROUP if _GROUP_WORD.search(tail) else Scope.BORROWER
            period = Period.FULL
            if metric is Metric.REVENUE and _Q4_WORD.search(
                folded[max(0, match.start() - 60) : match.end() + 60]
            ):
                period = Period.FOURTH_QUARTER
            found.append(
                MetricMention(
                    match.start(), match.end(), MetricLeaf(metric, scope, period)
                )
            )

    found.sort(key=lambda item: item.start)
    return found


def _dedupe(mentions: list[MetricMention]) -> list[MetricMention]:
    """Collapse repeated references to the same metric into one operand.

    A clause names its metrics several times -- once in the operative
    sentence and again in the definitions that follow -- but each distinct
    metric contributes to the covenant exactly once.
    """
    seen: set[Expression] = set()
    unique: list[MetricMention] = []
    for item in mentions:
        if item.expression in seen:
            continue
        seen.add(item.expression)
        unique.append(item)
    return unique


def _combine(mentions: list[MetricMention], segment: str) -> Expression | None:
    """Assemble one side of a covenant from the metrics it names, using the
    connective present in that segment.
    """
    mentions = _dedupe(mentions)
    if not mentions:
        return None
    expressions = [item.expression for item in mentions]
    if len(expressions) == 1:
        return expressions[0]

    if _LARGEST_WORD.search(segment):
        # "наибольшей из величин A и B" -- and, when a subtrahend precedes
        # it, "X за вычетом наибольшей из A и B".
        if _LESS_WORD.search(segment) and len(expressions) >= 3:
            return Subtract(expressions[0], Max(tuple(expressions[1:])))
        return Max(tuple(expressions))

    if _LESS_WORD.search(segment):
        return Subtract(expressions[0], expressions[1])

    if _SUM_WORD.search(segment) or len(expressions) > 1:
        return Add(tuple(expressions))
    return expressions[0]


def _parse_expression(clause_folded: str, unit: Unit) -> Expression | None:
    """Build the covenant's measured expression.

    A ratio is written either explicitly ("отношение A к B") or implicitly,
    with the multiple standing between the two sides ("A ... превышал 0.08х
    B"). Both are handled by locating the split point and combining each
    side independently.
    """
    mentions = _find_mentions(clause_folded)
    if not mentions:
        return None

    if unit is Unit.RATIO:
        return _parse_ratio(clause_folded, mentions)

    combined = _drop_ebitda_definition(mentions)
    return _combine(combined, clause_folded)


def _sentence_end(text: str, start: int) -> int:
    """End of the sentence containing ``start`` -- the boundary beyond which
    a following definition sentence must not contribute operands.
    """
    match = SENTENCE_END_RE.search(text, start)
    return match.start() if match else len(text)


def _parse_ratio(clause_folded: str, mentions: list[MetricMention]) -> Expression | None:
    """Split a ratio clause into its two sides.

    Written explicitly the clause says "отношение <numerator> к
    <denominator>"; written implicitly the multiple itself separates the
    sides ("<numerator> ... превышал 0.08х <denominator>"). Each side is
    bounded by its own sentence so that a later definition sentence naming
    the same captions again does not add phantom operands.
    """
    ratio_word = clause_folded.find(_RATIO_WORD)
    separator = _TO_SEPARATOR.search(clause_folded, ratio_word) if ratio_word != -1 else None

    if separator is not None:
        numerator_region = (ratio_word, separator.start())
        denominator_region = (separator.end(), _sentence_end(clause_folded, separator.end()))
    else:
        # The implicit form separates the two sides with the limit itself,
        # written either as a multiple ("... превышал 0.08х <denominator>")
        # or as a percentage ("... не превышали 80 процентов <denominator>").
        multiple = RATIO_RE.search(clause_folded) or PERCENT_RE.search(clause_folded)
        if multiple is None:
            return None
        # Implicit form: the subject is the metric named closest before the
        # multiple, and everything up to the sentence end after it is the
        # denominator.
        before = [m for m in mentions if m.end <= multiple.start()]
        if not before:
            return None
        numerator_region = (before[-1].start, multiple.start())
        denominator_region = (multiple.end(), _sentence_end(clause_folded, multiple.end()))

    def side(region: tuple[int, int]) -> Expression | None:
        start, end = region
        inside = [m for m in mentions if m.start >= start and m.end <= end]
        return _combine(_drop_ebitda_definition(inside), clause_folded[start:end])

    numerator = side(numerator_region)
    denominator = side(denominator_region)
    if numerator is None or denominator is None:
        return None
    return Ratio(numerator, denominator)


def _drop_ebitda_definition(mentions: list[MetricMention]) -> list[MetricMention]:
    """Remove revenue/operating-expense mentions that merely spell out an
    EBITDA already present on the same side.
    """
    if not any(isinstance(item.expression, Ebitda) for item in mentions):
        return mentions
    return [
        item
        for item in mentions
        if not (
            isinstance(item.expression, MetricLeaf)
            and item.expression.metric in {Metric.REVENUE, Metric.OPERATING_EXPENSES}
        )
    ]


def _parse_comparator(clause_folded: str) -> Comparator | None:
    minimum = any(term in clause_folded for term in _MIN_COMPARATOR_TERMS)
    maximum = any(term in clause_folded for term in _MAX_COMPARATOR_TERMS)
    if minimum and not maximum:
        return Comparator.GE
    if maximum and not minimum:
        return Comparator.LE
    if minimum and maximum:
        # "не допускать снижения ... ниже X" is a floor expressed with a
        # negation; the explicit minimum wording wins.
        return Comparator.GE
    return None


def _conditional_split(clause_folded: str) -> tuple[int, int, int] | None:
    """Bounds of an "Если ..., то ..." clause: condition, then covenant.

    Returns ``(condition_start, condition_end, covenant_start)`` or None
    when the clause is not written in that form.
    """
    opener = _IF_WORD.search(clause_folded)
    split = _THEN_SPLIT.search(clause_folded)
    if opener is None or split is None or opener.start() >= split.start():
        return None
    return opener.end(), split.start(), split.end()


def _applicability_from_condition(condition: str) -> Applicability | None:
    """The trigger of an "Если ..., то ..." covenant.

    The trigger is usually a ratio ("Коэффициент долговой нагрузки ...
    превышает 2.40х"), so the whole ratio is recovered rather than the last
    metric named -- testing EBITDA against 2.40 would pass for any borrower
    and quietly make the covenant always binding.
    """
    match = RATIO_RE.search(condition) or MONEY_RE.search(condition)
    if match is None:
        return None
    threshold = _decimal(match.group(1))
    if threshold is None:
        return None

    head = condition[: match.start()]
    expression = _parse_expression(head, Unit.RATIO)
    if expression is None:
        mentions = _find_mentions(head)
        if not mentions:
            return None
        expression = mentions[-1].expression
    return Applicability(
        expression=expression, comparator=Comparator.GT, threshold=threshold
    )


def _parse_applicability(clause_folded: str) -> Applicability | None:
    """A springing covenant states its trigger after "только при условии"."""
    trigger = _SPRINGING_WORD.search(clause_folded)
    if not trigger:
        return None
    tail = clause_folded[trigger.end() :]
    amount = MONEY_RE.search(tail)
    if not amount:
        return None
    threshold = _decimal(amount.group(1))
    mentions = _find_mentions(tail[: amount.start()])
    if threshold is None or not mentions:
        return None
    return Applicability(
        expression=mentions[-1].expression,
        comparator=Comparator.GT,
        threshold=threshold,
    )


def parse_clause(
    clause_number: str,
    clause_text: str,
    period_start: str,
    period_end: str,
) -> Covenant | None:
    """Compile one clause, or return None when its structure is unknown."""
    folded = canon(" ".join(clause_text.split()))

    springing = _SPRINGING_WORD.search(folded)
    conditional = _conditional_split(folded) if springing is None else None
    # The trigger carries its own figure; keep it out of the covenant's own
    # threshold search. Which side to keep depends on the form: "только при
    # условии" puts the trigger last, "Если ..., то ..." puts it first.
    if conditional is not None:
        threshold_area = folded[conditional[2] :]
    elif springing is not None:
        threshold_area = folded[: springing.start()]
    else:
        threshold_area = folded

    ratio_match = RATIO_RE.search(threshold_area)
    percent_match = PERCENT_RE.search(threshold_area)
    if ratio_match:
        unit = Unit.RATIO
        threshold = _decimal(ratio_match.group(1))
    elif percent_match:
        # A percentage of another metric is a ratio stated per hundred.
        unit = Unit.RATIO
        stated = _decimal(percent_match.group(1).replace(",", "."))
        threshold = None if stated is None else stated / Decimal(100)
    else:
        money_match = MONEY_RE.search(threshold_area)
        if not money_match:
            return None
        unit = Unit.USD
        threshold = _decimal(money_match.group(1))
    if threshold is None:
        return None

    comparator = _parse_comparator(threshold_area)
    expression = _parse_expression(threshold_area, unit)
    if comparator is None or expression is None:
        return None

    dates = DATE_RANGE_RE.search(folded)
    start, end = (dates.group(1), dates.group(2)) if dates else (period_start, period_end)

    return Covenant(
        clause=clause_number,
        expression=expression,
        comparator=comparator,
        threshold=threshold,
        unit=unit,
        period_start=start,
        period_end=end,
        applicability=_parse_applicability(folded)
        or (
            _applicability_from_condition(folded[conditional[0] : conditional[1]])
            if conditional is not None
            else None
        ),
        source_text=" ".join(clause_text.split()),
    )
