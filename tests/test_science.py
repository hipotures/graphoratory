from fractions import Fraction
from pathlib import Path

import pytest
from sglab.model import BitGraph  # type: ignore[import-untyped]

import graphoratory.science.evaluator as evaluator_module
from graphoratory.graphs import Graph
from graphoratory.science.baseline import (
    ForbiddenCycleBreakBaseline,
    UniformTwoSwitchBaseline,
    baseline_for_selector,
)
from graphoratory.science.evaluator import (
    EnergyScale,
    IndependentEvaluator,
    IntegerInterval,
)
from graphoratory.science.worker import (
    INITIAL_NODE_BUDGET,
    ScoreTimeoutWithoutPartial,
    ScoreWorker,
)


def _complete_graph(order: int) -> Graph:
    return Graph.from_edges(
        order,
        ((u, v) for u in range(order) for v in range(u + 1, order)),
    )


def test_baseline_matches_frozen_heg_k5_rewrite() -> None:
    candidate = UniformTwoSwitchBaseline().propose(_complete_graph(5), step_index=0)

    assert candidate is not None
    assert candidate.edges == (
        (0, 1),
        (0, 2),
        (0, 3),
        (0, 4),
        (1, 2),
        (1, 3),
        (1, 4),
        (2, 4),
        (3, 4),
    )


def test_legacy_baseline_selector_mapping_is_exact() -> None:
    random = baseline_for_selector("random")
    structural = baseline_for_selector("structural")

    assert isinstance(random, UniformTwoSwitchBaseline)
    assert random.name == "heg_uniform_two_switch"
    assert random.provenance()["operator"] == "uniform_two_edge_switch"
    assert isinstance(structural, ForbiddenCycleBreakBaseline)
    assert structural.name == "heg_forbidden_cycle_break"
    assert structural.provenance()["operator"] == "forbidden_cycle_break_switch"


def test_energy_scale_matches_legacy_hand_vector() -> None:
    scale = EnergyScale.build(8, (4,), 4)

    assert scale.edge_min == 12
    assert scale.edge_max == 28
    assert scale.energy_max == 5524
    assert scale.utility(IntegerInterval(0, 5524)).lower == Fraction(0)
    assert scale.utility(IntegerInterval(0, 5524)).upper == Fraction(1)


def test_cpp_score_worker_matches_legacy_k4_vector() -> None:
    graph = BitGraph.from_graph6("C~")
    with ScoreWorker() as worker:
        response = worker.score(
            graph,
            lengths=(4,),
            witness_cap=64,
            node_budget=INITIAL_NODE_BUDGET,
        )

    assert [
        (
            int(result.length),
            int(result.count),
            bool(result.complete),
            int(result.nodes),
        )
        for result in response.results
    ] == [(4, 3, True, 24)]
    scale = EnergyScale.build(4, (4,), 64)
    energy = scale._encode(3, 48, 6)  # noqa: SLF001 - frozen legacy parity vector
    assert energy == 3123
    assert scale.utility(IntegerInterval(energy, energy)).lower == Fraction(61, 64)


def test_independent_evaluator_matches_legacy_k4_fitness() -> None:
    class FixedPolicy:
        name = "fixed_fixture"
        seed = 0
        horizon = 0
        witness_cap = 64

        def propose(self, graph: Graph, *, step_index: int) -> Graph | None:
            raise AssertionError("a zero-horizon policy must not be invoked")

        def provenance(self) -> dict[str, object]:
            return {"name": self.name}

    result = IndependentEvaluator().evaluate((_complete_graph(4),), FixedPolicy())

    assert result.score.lower == Fraction(61, 64)
    assert result.score.upper == Fraction(61, 64)


def test_same_fixed_graph_evaluated_twice_has_identical_score() -> None:
    graph = _complete_graph(5)
    baseline = UniformTwoSwitchBaseline()

    first = IndependentEvaluator().evaluate((graph,), baseline)
    second = IndependentEvaluator().evaluate((graph,), baseline)

    assert first.score == second.score
    assert first.diagnostics.accepted_rewrites == second.diagnostics.accepted_rewrites


def test_unsafe_initial_timeout_produces_full_conservative_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Identity:
        def payload(self) -> dict[str, object]:
            return {"backend": "timeout-fixture"}

    class TimeoutWorker:
        identity = Identity()

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def score(self, *_args: object, **_kwargs: object) -> object:
            raise ScoreTimeoutWithoutPartial("fixture timeout")

    monkeypatch.setattr(evaluator_module, "ScoreWorker", TimeoutWorker)
    result = IndependentEvaluator().evaluate(
        (_complete_graph(5),),
        UniformTwoSwitchBaseline(),
    )

    assert result.score == evaluator_module.RationalInterval(Fraction(), Fraction(1))
    assert result.diagnostics.unsafe_score_timeouts == 1


def test_baseline_and_evaluator_are_separate_components() -> None:
    root = Path(__file__).parents[1] / "src" / "graphoratory" / "science"
    baseline_source = (root / "baseline.py").read_text(encoding="utf-8")
    evaluator_source = (root / "evaluator.py").read_text(encoding="utf-8")

    assert "science.evaluator" not in baseline_source
    assert "science.baseline" not in evaluator_source
