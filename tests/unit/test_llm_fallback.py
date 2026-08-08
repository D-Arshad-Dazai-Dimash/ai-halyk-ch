"""The model fallback's acceptance boundary.

The fallback exists to rescue clauses the deterministic parser cannot read,
so what matters is not that it accepts good proposals but that it refuses
bad ones: an unanchored threshold, an invented metric, a pathological
expression, or a provider that is simply not there. Every refusal must land
on "leave the cell unresolved", never on "report a number the clause does
not support".

No test here touches a network: ``build_covenant`` is pure, and the one
end-to-end case substitutes a stub for the provider call.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from covenant_agent.covenants import llm_fallback
from covenant_agent.covenants.ast import (
    Comparator,
    Ebitda,
    MetricLeaf,
    Ratio,
    Scope,
    Unit,
)
from covenant_agent.covenants.llm_fallback import (
    FallbackCompiler,
    Rejected,
    build_covenant,
    numbers_in,
)
from covenant_agent.ledger.metrics import Metric
from covenant_agent.llm.cloud import ModelUnavailable, Provider

CLAUSE = (
    "Пункт 6.1. Заёмщик обязуется не допускать, чтобы отношение суммы "
    "операционных расходов и лизинговых платежей к EBITDA превышало 0,08х "
    "по итогам отчётного периода."
)

RATIO_PROPOSAL = {
    "unsupported": False,
    "comparator": "<=",
    "unit": "ratio",
    "threshold": "0.08",
    "expression": {
        "type": "ratio",
        "numerator": {
            "type": "add",
            "operands": [
                {"type": "metric", "name": "operating_expenses"},
                {"type": "metric", "name": "lease_payments"},
            ],
        },
        "denominator": {"type": "ebitda", "scope": "borrower", "adjusted": False},
    },
    "springing": None,
}


def compile_ratio(**overrides):
    proposal = {**RATIO_PROPOSAL, **overrides}
    return build_covenant(proposal, "6.1", CLAUSE, "2025-01-01", "2025-12-31")


class TestNumberAnchoring:
    """A threshold is only real if the clause writes it."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("не более 5 000 000 долларов США", Decimal("5000000")),
            ("сумму 5,000,000.00 USD", Decimal("5000000")),
            ("порог 1 500 000,00", Decimal("1500000")),
            ("не превышал 0,08х", Decimal("0.08")),
            ("не превышал 0.08x", Decimal("0.08")),
        ],
    )
    def test_reads_each_written_convention(self, text, expected):
        assert expected in numbers_in(text)

    def test_thousands_separator_is_not_read_as_a_decimal_point(self):
        # "1,500" is one and a half thousand, not one and a half.
        assert numbers_in("сумма 1,500 долларов") == {Decimal("1500")}

    def test_ocr_digit_substitution_still_anchors(self):
        # Tesseract renders some digits as their Cyrillic lookalikes; the
        # canonicaliser folds those back before the number is read.
        assert Decimal("0.08") in numbers_in("превышало 0,08х")


class TestThresholdMustComeFromTheClause:
    def test_accepts_a_threshold_the_clause_states(self):
        covenant = compile_ratio()
        assert covenant.threshold == Decimal("0.08")
        assert covenant.unit is Unit.RATIO
        assert covenant.comparator is Comparator.LE

    def test_accepts_a_differently_written_form_of_the_same_value(self):
        clause = "не более 5,000,000.00 долларов США"
        covenant = build_covenant(
            {
                "comparator": "<=",
                "unit": "USD",
                "threshold": "5000000",
                "expression": {"type": "metric", "name": "capital_expenditures"},
            },
            "6.2",
            clause,
            "2025-01-01",
            "2025-12-31",
        )
        assert covenant.threshold == Decimal("5000000")

    def test_rejects_a_threshold_absent_from_the_clause(self):
        # The single most dangerous failure: a plausible number that was
        # never written down. Reporting it would look like a real answer.
        with pytest.raises(Rejected, match="does not occur"):
            compile_ratio(threshold="0.09")

    def test_rejects_a_non_positive_threshold(self):
        with pytest.raises(Rejected, match="not positive"):
            compile_ratio(threshold="0")

    def test_rejects_a_non_numeric_threshold(self):
        with pytest.raises(Rejected):
            compile_ratio(threshold="восемь сотых")


class TestExpressionValidation:
    def test_builds_the_closed_ast_nodes(self):
        expression = compile_ratio().expression
        assert isinstance(expression, Ratio)
        assert isinstance(expression.denominator, Ebitda)
        assert {leaf.metric for leaf in expression.numerator.operands} == {
            Metric.OPERATING_EXPENSES,
            Metric.LEASE_PAYMENTS,
        }

    def test_metric_defaults_are_borrower_and_full_period(self):
        leaf = compile_ratio(
            expression={"type": "metric", "name": "revenue"}
        ).expression
        assert isinstance(leaf, MetricLeaf)
        assert leaf.scope is Scope.BORROWER

    def test_rejects_a_metric_the_taxonomy_does_not_define(self):
        with pytest.raises(Rejected, match="unknown metric"):
            compile_ratio(expression={"type": "metric", "name": "goodwill_impairment"})

    def test_rejects_an_unknown_node_type(self):
        with pytest.raises(Rejected, match="unknown expression node"):
            compile_ratio(expression={"type": "logarithm", "operand": {}})

    def test_rejects_an_unknown_comparator(self):
        with pytest.raises(Rejected, match="unknown comparator"):
            compile_ratio(comparator="approximately")

    def test_rejects_an_unknown_unit(self):
        with pytest.raises(Rejected, match="unknown unit"):
            compile_ratio(unit="EUR")

    def test_rejects_an_over_nested_expression(self):
        node = {"type": "metric", "name": "revenue"}
        for _ in range(llm_fallback.MAX_EXPRESSION_DEPTH + 2):
            node = {"type": "ratio", "numerator": node, "denominator": node}
        with pytest.raises(Rejected, match="too deeply|too many nodes"):
            compile_ratio(expression=node)

    def test_rejects_an_expression_with_too_many_nodes(self):
        operands = [{"type": "metric", "name": "revenue"}] * (
            llm_fallback.MAX_EXPRESSION_NODES + 5
        )
        with pytest.raises(Rejected, match="too many nodes"):
            compile_ratio(expression={"type": "add", "operands": operands})

    def test_rejects_a_single_operand_sum(self):
        with pytest.raises(Rejected, match="at least two operands"):
            compile_ratio(
                expression={
                    "type": "add",
                    "operands": [{"type": "metric", "name": "revenue"}],
                }
            )


class TestProposalEnvelope:
    def test_honours_the_models_own_refusal(self):
        # Told to say so rather than guess, the model saying so must be
        # taken at its word.
        with pytest.raises(Rejected, match="unsupported"):
            compile_ratio(unsupported=True)

    def test_rejects_a_non_object_proposal(self):
        with pytest.raises(Rejected):
            build_covenant(["nope"], "6.1", CLAUSE, "2025-01-01", "2025-12-31")

    def test_springing_condition_is_validated_like_the_covenant(self):
        clause = CLAUSE + " Настоящий пункт применяется только при условии, что заёмные средства превышают 5 000 000 долларов США."
        covenant = build_covenant(
            {
                **RATIO_PROPOSAL,
                "springing": {
                    "expression": {"type": "metric", "name": "financing_proceeds"},
                    "comparator": ">",
                    "threshold": "5000000",
                },
            },
            "6.1",
            clause,
            "2025-01-01",
            "2025-12-31",
        )
        assert covenant.applicability is not None
        assert covenant.applicability.threshold == Decimal("5000000")

    def test_springing_threshold_must_also_be_anchored(self):
        with pytest.raises(Rejected, match="does not occur"):
            compile_ratio(
                springing={
                    "expression": {"type": "metric", "name": "financing_proceeds"},
                    "comparator": ">",
                    "threshold": "7500000",
                }
            )


class TestCompilerBehaviour:
    def test_is_inert_without_a_provider(self, monkeypatch):
        # The offline default: no key, no call, no answer -- and crucially
        # no crash, so a run without credentials behaves exactly as before.
        monkeypatch.setattr(llm_fallback, "detect_providers", list)
        compiler = FallbackCompiler.create()
        assert not compiler.available
        assert compiler.compile("6.1", CLAUSE, "2025-01-01", "2025-12-31") is None
        assert compiler.calls_made == 0

    def test_disabled_flag_beats_a_configured_provider(self, monkeypatch):
        monkeypatch.setattr(
            llm_fallback, "detect_providers", lambda: [Provider("groq", "m", "k")]
        )
        assert not FallbackCompiler.create(enabled=False).available

    def test_compiles_a_clause_end_to_end(self, monkeypatch):
        compiler = _stub_compiler(monkeypatch, RATIO_PROPOSAL)
        covenant = compiler.compile("6.1", CLAUSE, "2025-01-01", "2025-12-31")
        assert covenant is not None
        assert covenant.clause == "6.1"
        assert compiler.models_used == {"stub-model"}

    def test_a_rejected_proposal_yields_no_covenant_and_names_no_model(
        self, monkeypatch
    ):
        # A model whose proposal is thrown away has not produced an answer,
        # so it must not appear in the submission's ``model`` field.
        compiler = _stub_compiler(monkeypatch, {**RATIO_PROPOSAL, "threshold": "0.09"})
        assert compiler.compile("6.1", CLAUSE, "2025-01-01", "2025-12-31") is None
        assert compiler.models_used == set()
        assert compiler.rejections

    def test_a_provider_failure_is_not_an_error(self, monkeypatch):
        def explode(*_args, **_kwargs):
            raise ModelUnavailable("network is down")

        compiler = _stub_compiler(monkeypatch, None, side_effect=explode)
        assert compiler.compile("6.1", CLAUSE, "2025-01-01", "2025-12-31") is None
        assert compiler.rejections

    def test_falls_over_to_the_next_provider(self, monkeypatch):
        # The failure this exists for: the first provider is configured but
        # unusable (exhausted balance, quota, outage). Discovering that at
        # submission time must cost a retry, not the cell.
        seen: list[str] = []

        def complete(provider, **_kwargs):
            seen.append(provider.name)
            if provider.name == "dead":
                raise ModelUnavailable("credit balance is too low")
            return RATIO_PROPOSAL

        monkeypatch.setattr(llm_fallback, "complete_json", complete)
        monkeypatch.setattr(llm_fallback, "cached_json", lambda _key, compute: compute())
        monkeypatch.setattr(
            llm_fallback,
            "detect_providers",
            lambda: [Provider("dead", "dead-model", "k"), Provider("live", "live-model", "k")],
        )

        compiler = FallbackCompiler.create()
        assert compiler.compile("6.1", CLAUSE, "2025-01-01", "2025-12-31") is not None
        assert seen == ["dead", "live"]
        # Only the model that actually answered may be declared.
        assert compiler.models_used == {"live-model"}

    def test_stops_at_the_call_budget(self, monkeypatch):
        monkeypatch.setattr(llm_fallback, "MODEL_FALLBACK_MAX_CALLS", 2)
        compiler = _stub_compiler(monkeypatch, RATIO_PROPOSAL)
        for index in range(5):
            # Distinct clause text so the cache cannot mask the budget.
            compiler.compile("6.1", f"{CLAUSE} вариант {index}", "2025-01-01", "2025-12-31")
        assert compiler.calls_made == 2


class TestItActuallyRescuesAClause:
    """The point of the whole module: turn a lost cell into a real answer."""

    # A cap phrased as "ограничивается величиной" rather than with any of
    # the comparator wordings the parser knows. Everything else about it is
    # ordinary -- which is exactly the private-dataset risk.
    UNPARSEABLE = (
        "Пункт 6.4. Совокупный объём капитальных вложений Заёмщика, "
        "определяемый по аудированной финансовой отчётности, ограничивается "
        "величиной 5 000 000 долларов США за отчётный период."
    )

    def test_the_deterministic_parser_cannot_read_it(self):
        from covenant_agent.covenants.parser import parse_clause

        assert parse_clause("6.4", self.UNPARSEABLE, "2025-01-01", "2025-12-31") is None

    def test_the_fallback_produces_a_covenant_the_engine_can_evaluate(
        self, monkeypatch
    ):
        from covenant_agent.calc.engine import Contribution, MetricTotals, decide, evaluate

        compiler = _stub_compiler(
            monkeypatch,
            {
                "comparator": "<=",
                "unit": "USD",
                "threshold": "5000000",
                "expression": {"type": "metric", "name": "capital_expenditures"},
            },
        )
        covenant = compiler.compile(
            "6.4", self.UNPARSEABLE, "2025-01-01", "2025-12-31"
        )
        assert covenant is not None

        # The figure is still arithmetic over ledger rows -- the model only
        # said *which* rows and *which* direction.
        totals = MetricTotals(
            contributions=[
                Contribution(
                    txn_id="TXN-X-0001",
                    metric=Metric.CAPITAL_EXPENDITURES,
                    amount=Decimal("6000000"),
                    rule="test-fixture",
                    scope=Scope.BORROWER,
                )
            ]
        )
        actual = evaluate(covenant.expression, totals)
        assert actual == Decimal("6000000")
        assert decide(covenant, actual).compliant is False


def _stub_compiler(monkeypatch, proposal, side_effect=None):
    """A compiler wired to a fake provider, with the disk cache bypassed."""

    def fake_complete(*_args, **_kwargs):
        if side_effect is not None:
            return side_effect()
        return proposal

    monkeypatch.setattr(llm_fallback, "complete_json", fake_complete)
    monkeypatch.setattr(llm_fallback, "cached_json", lambda _key, compute: compute())
    monkeypatch.setattr(
        llm_fallback, "detect_providers", lambda: [Provider("stub", "stub-model", "key")]
    )
    return FallbackCompiler.create()
