from __future__ import annotations

from random import Random
from typing import Any

from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import PLUGIN  # type: ignore[import-untyped]

from graphoratory.graphs import Graph

BASELINE_NAME = "heg_uniform_two_switch"
BASELINE_SEED = 4001
BASELINE_HORIZON = 32
WITNESS_CAP = 64
HEG_COMMIT = "27cbec9c2307b6ea5f936f858821d11d808b68f3"
HEG_SOURCE_TREE = "85fb2a34a14fc0274137f91aef02cb8c33484d97"


class UniformTwoSwitchBaseline:
    """Frozen HEG random baseline for unrestricted minimum-degree-three graphs."""

    name = BASELINE_NAME
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
                "mutation_operator": "uniform_two_edge_switch",
            },
        )
        candidate = Graph.from_edges(result.graph.n, result.graph.edges())
        candidate.validate_scientific_invariants()
        return candidate

    def provenance(self) -> dict[str, object]:
        return {
            "name": self.name,
            "operator": "uniform_two_edge_switch",
            "graph_mode": "unrestricted_min_degree_3",
            "seed": self.seed,
            "horizon": self.horizon,
            "witness_cap": self.witness_cap,
            "heg_commit": HEG_COMMIT,
            "heg_source_tree": HEG_SOURCE_TREE,
        }
