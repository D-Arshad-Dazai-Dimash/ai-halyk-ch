"""Closed covenant expression AST.

A covenant compiles to a small, fixed set of node types. Legal prose never
reaches the calculation engine: only these nodes do, and the evaluator can
execute nothing else. That keeps every reported number traceable to a
metric, an operator, and a threshold rather than to a model's opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from covenant_agent.ledger.metrics import Metric


class Scope(str, Enum):
    """Which entity's figures a metric leaf draws on."""

    BORROWER = "borrower"
    GROUP = "group"


class Period(str, Enum):
    """Which slice of the covenant period a metric leaf measures."""

    FULL = "full"
    FOURTH_QUARTER = "fourth_quarter"
    PERIOD_END = "period_end"
    #: Covenants tested "за любой финансовый квартал" bind on the worst
    #: quarter, not on the year. Which quarter is worst depends on the
    #: direction: a floor is broken by the smallest, a cap by the largest.
    QUARTERLY_MIN = "quarterly_min"
    QUARTERLY_MAX = "quarterly_max"


class Comparator(str, Enum):
    LE = "<="
    GE = ">="
    LT = "<"
    GT = ">"


class Unit(str, Enum):
    USD = "USD"
    RATIO = "ratio"


@dataclass(frozen=True)
class MetricLeaf:
    metric: Metric
    scope: Scope = Scope.BORROWER
    period: Period = Period.FULL


@dataclass(frozen=True)
class Ebitda:
    """Revenue less operating expenses, optionally with the auditor's
    material one-time add-backs (an "adjusted EBITDA").
    """

    scope: Scope = Scope.BORROWER
    adjusted: bool = False
    #: EBITDA is measured per quarter by the same covenants that measure a
    #: lone metric that way ("минимальная квартальная EBITDA").
    period: Period = Period.FULL


@dataclass(frozen=True)
class Add:
    operands: tuple["Expression", ...]


@dataclass(frozen=True)
class Subtract:
    minuend: "Expression"
    subtrahend: "Expression"


@dataclass(frozen=True)
class Ratio:
    numerator: "Expression"
    denominator: "Expression"


@dataclass(frozen=True)
class Max:
    operands: tuple["Expression", ...]


Expression = MetricLeaf | Ebitda | Add | Subtract | Ratio | Max


@dataclass(frozen=True)
class Applicability:
    """A springing test: the covenant binds only when this condition holds."""

    expression: Expression
    comparator: Comparator
    threshold: Decimal


@dataclass(frozen=True)
class Covenant:
    clause: str
    expression: Expression
    comparator: Comparator
    threshold: Decimal
    unit: Unit
    period_start: str
    period_end: str
    applicability: Applicability | None = None
    title: str = ""
    source_text: str = ""

    @property
    def is_ratio(self) -> bool:
        return self.unit is Unit.RATIO


def metrics_in(expression: Expression) -> set[Metric]:
    """Every base metric an expression depends on (EBITDA expands to its
    revenue and operating-expense components).
    """
    if isinstance(expression, MetricLeaf):
        return {expression.metric}
    if isinstance(expression, Ebitda):
        return {Metric.REVENUE, Metric.OPERATING_EXPENSES}
    if isinstance(expression, Add | Max):
        return set().union(*(metrics_in(item) for item in expression.operands))
    if isinstance(expression, Subtract):
        return metrics_in(expression.minuend) | metrics_in(expression.subtrahend)
    if isinstance(expression, Ratio):
        return metrics_in(expression.numerator) | metrics_in(expression.denominator)
    raise TypeError(f"unsupported expression node: {type(expression).__name__}")
