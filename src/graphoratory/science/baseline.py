from __future__ import annotations

from random import Random
from typing import Any

from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import PLUGIN  # type: ignore[import-untyped]

from graphoratory.graphs import Graph

RANDOM_BASELINE = "random"
STRUCTURAL_BASELINE = "structural"
SUPPORTED_BASELINES = (RANDOM_BASELINE, STRUCTURAL_BASELINE)
UNIFORM_TWO_SWITCH_NAME = "heg_uniform_two_switch"
FORBIDDEN_CYCLE_BREAK_NAME = "heg_forbidden_cycle_break"
BASELINE_SEED = 4001
BASELINE_HORIZON = 32
WITNESS_CAP = 64
HEG_COMMIT = "27cbec9c2307b6ea5f936f858821d11d808b68f3"
HEG_SOURCE_TREE = "85fb2a34a14fc0274137f91aef02cb8c33484d97"


class _HegBaseline:
    selector: str
    name: str
    mutation_operator: str
    seed = BASELINE_SEED
    horizon = BASELINE_HORIZON
    witness_cap = WITNESS_CAP

    def propose(self, graph: Graph, *, step_index: int) -> Graph | None:
        source = BitGraph.from_edges(graph.order, graph.edges)
        rng = Random((self.seed << 32) ^ step_index)
        result: Any = PLUGIN.mutate_with_delta(
            source,
            rng,
            {
                "mode": "unrestricted_min_degree_3",
                "mutation_operator": self.mutation_operator,
            },
        )
        candidate = Graph.from_edges(result.graph.n, result.graph.edges())
        candidate.validate_scientific_invariants()
        return candidate

    def provenance(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "name": self.name,
            "operator": self.mutation_operator,
            "graph_mode": "unrestricted_min_degree_3",
            "seed": self.seed,
            "horizon": self.horizon,
            "witness_cap": self.witness_cap,
            "heg_commit": HEG_COMMIT,
            "heg_source_tree": HEG_SOURCE_TREE,
        }


class UniformTwoSwitchBaseline(_HegBaseline):
    """Frozen HEG random baseline for unrestricted minimum-degree-three graphs."""

    selector = RANDOM_BASELINE
    name = UNIFORM_TWO_SWITCH_NAME
    mutation_operator = "uniform_two_edge_switch"


class ForbiddenCycleBreakBaseline(_HegBaseline):
    """Frozen HEG structural baseline targeting forbidden-cycle witnesses."""

    selector = STRUCTURAL_BASELINE
    name = FORBIDDEN_CYCLE_BREAK_NAME
    mutation_operator = "forbidden_cycle_break_switch"


def baseline_for_selector(
    selector: str,
) -> UniformTwoSwitchBaseline | ForbiddenCycleBreakBaseline:
    if selector == RANDOM_BASELINE:
        return UniformTwoSwitchBaseline()
    if selector == STRUCTURAL_BASELINE:
        return ForbiddenCycleBreakBaseline()
    choices = ", ".join(SUPPORTED_BASELINES)
    raise ValueError(f"invalid baseline {selector!r}; expected one of: {choices}")
