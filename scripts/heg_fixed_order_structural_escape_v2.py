#!/usr/bin/env python3
"""
HEG fixed-order structural escape v2.

This version fixes two issues found in the first structural run:

1. CPU bottleneck
   Structural cycle reconstruction was CPU-bound Python submitted to the same
   ThreadPoolExecutor. Because of the GIL, the main python process saturated one
   core while the external sglab score workers were mostly idle.

   v2 uses a persistent spawn-based ProcessPoolExecutor for structural analysis.

2. Escape starvation
   The old structural shortlist filled all --structural-pool slots with
   TOTAL==global_best states before it ever reached TOTAL==global_best+1.
   Consequently the log could show:
       escape=0 fill=8
   even though escape states existed.

   v2 reserves --escape-structural-pool slots specifically for the escape band.

The first run also revealed an important structural fact about the current n=26
best states:

    (7,0,0): tauE=7, unionE=28, maxEdgeLoad=1
    (6,1,0): tauE=7, unionE=32, maxEdgeLoad=1

So the seven forbidden cycles are edge-disjoint in those states. Edge overlap
cannot distinguish them. v2 therefore also computes vertex concentration:

    tauV               exact minimum vertex hitting-set size
    unionV             number of vertices used by forbidden cycles
    maxVertexLoad      maximum forbidden cycles through one vertex

Structural ranking for equal TOTAL is now:

    complete-structure first,
    tauE,
    tauV,
    unionE,
    unionV,
    -maxEdgeLoad,
    -maxVertexLoad,
    weighted score only afterwards.

Long-cycle safety
-----------------
Naive reconstruction of C16 can occasionally dominate runtime even when only a
few C16 witnesses exist. By default v2 structurally reconstructs only nonzero
components with length <= --max-structural-cycle-length (default 8).

States containing a positive longer component are NOT discarded. Their
structural metric is marked partial and structural/profile lanes can still keep
them, but main structural ranking prefers fully analyzed states at equal TOTAL.
The scientific scorer remains authoritative for TOTAL and success.

Default shortlist is reduced from 512 to 128 candidates:
    80 current-best TOTAL
    48 escape-band TOTAL

This is enough to feed a 32-state beam while reducing structural work by 4x.

Recommended run:

    uv run python scripts/heg_fixed_order_structural_escape_v2.py \
      --start-graph mixed_polish_best_n26.json \
      --beam-width 32 \
      --main-lanes 16 \
      --structural-lanes 8 \
      --escape-lanes 8 \
      --escape-height 1 \
      --structural-pool 128 \
      --escape-structural-pool 48 \
      --structural-workers 8 \
      --max-structural-cycle-length 8 \
      --max-depth 40 \
      --step-seconds 120 \
      --total-seconds 1200 \
      --workers 16 \
      --families two_switch,endpoint_relocate \
      --node-budget 10000000 \
      --witness-cap 1000000 \
      --save-best structural_v2_best_n26.json \
      --save-lineage structural_v2_lineage_n26.json
"""

from __future__ import annotations

import argparse
import multiprocessing
import atexit
import json
import math
import random
import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from rich.console import Console
from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import forbidden_lengths  # type: ignore[import-untyped]

from graphoratory.config import (
    DegreeRangeConfig,
    ErdosRenyiConfig,
    GraphConfig,
    MixedGeneratorConfig,
)
from graphoratory.graphs import Graph, generate_graphs
from graphoratory.science.worker import ScoreWorker


Edge = tuple[int, int]

MAX_ORDER = 128
DEFAULT_WITNESS_CAP = 1_000_000
DEFAULT_NODE_BUDGET = 10_000_000

STATUS_EXACT = "EXACT"
STATUS_SATURATED = "SATURATED_AT_CAP"
STATUS_BUDGET = "SEARCH_BUDGET_EXHAUSTED"

PRESET_REWIRE2_N26 = "rewire2-n26"
PRESET_EXPECTED_HASH_PREFIX = "5ba52272"
SUPPORTED_FAMILIES = ("two_switch", "endpoint_relocate")

console = Console()

_tls = threading.local()
_workers_lock = threading.Lock()
_live_score_workers: list[ScoreWorker] = []


@dataclass(frozen=True, slots=True)
class ComponentScore:
    length: int
    observed: int
    status: str
    nodes: int
    elapsed_ns: int

    @property
    def exact(self) -> bool:
        return self.status == STATUS_EXACT


@dataclass(frozen=True, slots=True)
class GraphScore:
    graph: Graph
    components: tuple[ComponentScore, ...]
    elapsed_seconds: float

    @property
    def fully_exact(self) -> bool:
        return all(component.exact for component in self.components)

    @property
    def total(self) -> int | None:
        if not self.fully_exact:
            return None
        return sum(component.observed for component in self.components)

    @property
    def weighted(self) -> int | None:
        if not self.fully_exact:
            return None
        return sum(
            weight(component.length) * component.observed
            for component in self.components
        )

    def component_map(self) -> dict[int, ComponentScore]:
        return {component.length: component for component in self.components}


@dataclass(frozen=True, slots=True)
class Mutation:
    family: str
    removed_edges: tuple[Edge, ...]
    added_edges: tuple[Edge, ...]
    variant: str = ""

    def label(self) -> str:
        removed = ",".join(f"{u}-{v}" for u, v in self.removed_edges)
        added = ",".join(f"{u}-{v}" for u, v in self.added_edges)
        suffix = f":{self.variant}" if self.variant else ""
        return f"{self.family}{suffix} rm[{removed}] add[{added}]"


@dataclass(frozen=True, slots=True)
class BeamState:
    score: GraphScore
    parent_state: "BeamState | None"
    mutation: Mutation | None
    depth: int

    @property
    def parent_score(self) -> GraphScore | None:
        return None if self.parent_state is None else self.parent_state.score


@dataclass(frozen=True, slots=True)
class StructuralMetrics:
    complete: bool
    tau_edge: int
    tau_vertex: int
    forbidden_edge_union: int
    forbidden_vertex_union: int
    max_cycles_per_edge: int
    max_cycles_per_vertex: int
    hitting_edges: tuple[Edge, ...]
    hitting_vertices: tuple[int, ...]
    analyzed_cycle_count: int
    skipped_cycle_count: int

    @property
    def cycle_count(self) -> int:
        return self.analyzed_cycle_count + self.skipped_cycle_count

    def compact(self) -> str:
        prefix = "" if self.complete else "partial "
        return (
            f"{prefix}tauE={self.tau_edge} tauV={self.tau_vertex} "
            f"unionE={self.forbidden_edge_union} "
            f"unionV={self.forbidden_vertex_union} "
            f"maxE={self.max_cycles_per_edge} "
            f"maxV={self.max_cycles_per_vertex}"
        )


def norm_edge(u: int, v: int) -> Edge:
    if u == v:
        raise ValueError("self-loop")
    return (u, v) if u < v else (v, u)


def weight(length: int) -> int:
    return max(1, 64 // length)


def parse_families(raw: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise argparse.ArgumentTypeError("at least one mutation family is required")
    unknown = sorted(set(names) - set(SUPPORTED_FAMILIES))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown families: {', '.join(unknown)}; "
            f"supported: {', '.join(SUPPORTED_FAMILIES)}"
        )
    return tuple(dict.fromkeys(names))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Polish one fixed-order HEG graph with 2-switches and degree-changing endpoint relocation."
        )
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--preset",
        choices=(PRESET_REWIRE2_N26,),
        default=None,
        help=(
            "Built-in reconstruction of the promising n=26 graph from the "
            "strong-rewiring experiment."
        ),
    )
    source.add_argument(
        "--start-graph",
        type=Path,
        default=None,
        help="JSON containing at least {order, edges}.",
    )

    parser.add_argument("--seed", type=int, default=4001)
    parser.add_argument("--beam-width", type=int, default=32)
    parser.add_argument("--main-lanes", type=int, default=16)
    parser.add_argument("--structural-lanes", type=int, default=8)
    parser.add_argument("--escape-lanes", type=int, default=8)
    parser.add_argument(
        "--escape-height",
        type=int,
        default=1,
        help="Keep escape states with TOTAL <= global_best_TOTAL + this value.",
    )
    parser.add_argument(
        "--structural-pool",
        type=int,
        default=128,
        help=(
            "Maximum exact candidates per depth receiving structural analysis. "
            "Includes the reserved escape quota."
        ),
    )
    parser.add_argument(
        "--escape-structural-pool",
        type=int,
        default=48,
        help=(
            "Structural-pool slots reserved for TOTAL above the current best "
            "but inside the escape barrier."
        ),
    )
    parser.add_argument(
        "--structural-workers",
        type=int,
        default=8,
        help="Spawn-based process workers for CPU-bound structural analysis.",
    )
    parser.add_argument(
        "--max-structural-cycle-length",
        type=int,
        default=8,
        help=(
            "Reconstruct actual cycle witnesses only up to this length. "
            "Longer positive components remain eligible but are marked partial."
        ),
    )
    parser.add_argument("--max-depth", type=int, default=40)
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=120.0,
        help="Wall-clock budget per search depth; 0 = unlimited.",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=1200.0,
        help="Whole-run wall-clock budget; 0 = unlimited.",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--families",
        type=parse_families,
        default=parse_families(",".join(SUPPORTED_FAMILIES)),
        help=(
            "Comma-separated mutation families. Supported: "
            + ",".join(SUPPORTED_FAMILIES)
        ),
    )
    parser.add_argument(
        "--max-mutations-per-family-parent",
        type=int,
        default=0,
        help=(
            "0 = enumerate all proposals; otherwise sample at most this many "
            "from EACH family for EACH beam parent."
        ),
    )
    parser.add_argument(
        "--inflight-per-worker",
        type=int,
        default=1,
    )
    parser.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET)
    parser.add_argument("--witness-cap", type=int, default=DEFAULT_WITNESS_CAP)
    parser.add_argument("--save-best", type=Path, default=None)
    parser.add_argument("--save-lineage", type=Path, default=None)
    parser.add_argument(
        "--hit-dir",
        type=Path,
        default=Path("growth_hits"),
    )

    args = parser.parse_args()

    if args.beam_width < 1:
        parser.error("--beam-width must be >= 1")
    if args.main_lanes < 0 or args.structural_lanes < 0 or args.escape_lanes < 0:
        parser.error("lane counts must be >= 0")
    if args.main_lanes + args.structural_lanes + args.escape_lanes > args.beam_width:
        parser.error("main+structural+escape lanes must be <= --beam-width")
    if args.escape_height < 0:
        parser.error("--escape-height must be >= 0")
    if args.structural_pool < args.beam_width:
        parser.error("--structural-pool must be >= --beam-width")
    if args.escape_structural_pool < args.escape_lanes:
        parser.error("--escape-structural-pool must be >= --escape-lanes")
    if args.escape_structural_pool >= args.structural_pool:
        parser.error("--escape-structural-pool must be < --structural-pool")
    if args.structural_workers < 1:
        parser.error("--structural-workers must be >= 1")
    if args.max_structural_cycle_length < 4:
        parser.error("--max-structural-cycle-length must be >= 4")
    if args.max_depth < 0:
        parser.error("--max-depth must be >= 0")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.inflight_per_worker < 1:
        parser.error("--inflight-per-worker must be >= 1")
    if args.max_mutations_per_family_parent < 0:
        parser.error("--max-mutations-per-family-parent must be >= 0")
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.step_seconds < 0 or args.total_seconds < 0:
        parser.error("time budgets must be >= 0")

    if args.start_graph is None and args.preset is None:
        args.preset = PRESET_REWIRE2_N26

    return args


def _thread_worker_init() -> None:
    worker = ScoreWorker()
    worker.__enter__()
    _tls.score_worker = worker
    with _workers_lock:
        _live_score_workers.append(worker)


def _close_workers() -> None:
    with _workers_lock:
        workers = list(_live_score_workers)
        _live_score_workers.clear()
    for worker in workers:
        try:
            worker.close()
        except Exception:
            pass


atexit.register(_close_workers)


def _score_worker() -> ScoreWorker:
    worker = getattr(_tls, "score_worker", None)
    if worker is None:
        raise RuntimeError("score worker thread was not initialized")
    return worker


def generated_root(order: int, seed: int) -> Graph:
    config = GraphConfig(
        generator="cycle_matching_stub_pairing",
        workspace_graph_count=1,
        line_graph_count=1,
        min_order=order,
        max_order=order,
        seed=seed,
        random_regular=DegreeRangeConfig(degree_min=3, degree_max=4),
        erdos_renyi_rejection=ErdosRenyiConfig(
            expected_degree_min=3.0,
            expected_degree_max=4.0,
        ),
        degree_sequence_rejection=DegreeRangeConfig(degree_min=3, degree_max=4),
        mixed=MixedGeneratorConfig(
            generators=("cycle_matching_stub_pairing",),
            weights=(1.0,),
        ),
    )
    graph = generate_graphs(config).graphs[0]
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    return graph


def apply_growth_replay_mutation(
    graph: Graph,
    *,
    family: str,
    removed_edges: tuple[Edge, ...],
    new_neighbors: tuple[int, ...],
    variant: str = "",
) -> Graph:
    """
    Replay just enough of the earlier strong-rewire mutation semantics to recover
    the reported n=26 graph.
    """
    edge_set = set(graph.edges)
    removed = tuple(norm_edge(*edge) for edge in removed_edges)

    for edge in removed:
        if edge not in edge_set:
            raise RuntimeError(
                f"preset replay mismatch at order {graph.order}: "
                f"missing removed edge {edge}"
            )
    for edge in removed:
        edge_set.remove(edge)

    if family == "switch_spoke":
        if len(removed) != 2:
            raise RuntimeError("switch_spoke replay expects two removed edges")

        (a, b), (c, d) = removed
        pairing, keep_role, _spoke_token = variant.split("/")

        if pairing == "ac_bd":
            cross1 = norm_edge(a, c)
            cross2 = norm_edge(b, d)
        elif pairing == "ad_bc":
            cross1 = norm_edge(a, d)
            cross2 = norm_edge(b, c)
        else:
            raise RuntimeError(f"unknown replay pairing {pairing}")

        kept = cross1 if keep_role == "keep1" else cross2
        if kept in edge_set:
            raise RuntimeError(
                f"preset replay would duplicate kept cross-edge {kept}"
            )
        edge_set.add(kept)

    elif family == "double_hub":
        if len(removed) != 2:
            raise RuntimeError("double_hub replay expects two removed edges")

    elif family == "split_spoke":
        if len(removed) != 1:
            raise RuntimeError("split_spoke replay expects one removed edge")

    else:
        raise RuntimeError(f"unsupported replay family {family}")

    x = graph.order
    for vertex in new_neighbors:
        edge_set.add(norm_edge(vertex, x))

    candidate = Graph.from_edges(graph.order + 1, edge_set)
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)
    return candidate


def reconstruct_preset_rewire2_n26(seed: int) -> Graph:
    if seed != 4001:
        raise ValueError(
            "--preset rewire2-n26 was produced with --seed 4001; "
            "use --seed 4001 or provide --start-graph"
        )

    graph = generated_root(16, seed)

    replay = (
        # n=17
        (
            "switch_spoke",
            ((5, 6), (8, 9)),
            (6, 8, 5),
            "ad_bc/keep1/sp5",
        ),
        # n=18
        (
            "switch_spoke",
            ((5, 16), (12, 13)),
            (13, 16, 12),
            "ac_bd/keep1/sp12",
        ),
        # n=19
        (
            "switch_spoke",
            ((3, 4), (11, 12)),
            (4, 11, 12),
            "ad_bc/keep1/sp12",
        ),
        # n=20
        (
            "switch_spoke",
            ((1, 13), (16, 17)),
            (13, 17, 1),
            "ac_bd/keep1/sp1",
        ),
        # n=21
        (
            "switch_spoke",
            ((1, 16), (6, 15)),
            (6, 16, 1),
            "ad_bc/keep1/sp1",
        ),
        # n=22
        (
            "switch_spoke",
            ((3, 14), (12, 17)),
            (3, 12, 14),
            "ac_bd/keep2/sp14",
        ),
        # n=23
        (
            "switch_spoke",
            ((1, 2), (8, 16)),
            (2, 8, 1),
            "ad_bc/keep1/sp1",
        ),
        # n=24
        (
            "switch_spoke",
            ((2, 22), (14, 21)),
            (14, 22, 21),
            "ad_bc/keep1/sp21",
        ),
        # n=25
        (
            "double_hub",
            ((2, 8), (3, 12)),
            (2, 8, 3, 12),
            "",
        ),
        # n=26
        (
            "split_spoke",
            ((12, 24),),
            (12, 24, 21),
            "",
        ),
    )

    for family, removed_edges, new_neighbors, variant in replay:
        graph = apply_growth_replay_mutation(
            graph,
            family=family,
            removed_edges=removed_edges,
            new_neighbors=new_neighbors,
            variant=variant,
        )

    if graph.order != 26:
        raise RuntimeError(f"preset replay ended at order {graph.order}, expected 26")

    if not graph.graph_hash.startswith(PRESET_EXPECTED_HASH_PREFIX):
        raise RuntimeError(
            "preset replay hash mismatch: "
            f"got {graph.graph_hash[:8]}, expected {PRESET_EXPECTED_HASH_PREFIX}. "
            "This usually means the local generator/code version differs from "
            "the run that produced the published lineage. Use --start-graph "
            "with an explicitly saved graph instead."
        )

    return graph


def load_start_graph(args: argparse.Namespace) -> Graph:
    if args.start_graph is not None:
        payload = json.loads(args.start_graph.read_text(encoding="utf-8"))
        graph = Graph.from_edges(
            int(payload["order"]),
            ((int(edge[0]), int(edge[1])) for edge in payload["edges"]),
        )
        graph.validate_scientific_invariants(max_order=MAX_ORDER)
        return graph

    return reconstruct_preset_rewire2_n26(args.seed)


def score_graph(
    graph: Graph,
    *,
    witness_cap: int,
    node_budget: int,
) -> GraphScore:
    started = time.perf_counter()
    lengths = tuple(int(length) for length in forbidden_lengths(graph.order))
    bit_graph = BitGraph.from_edges(graph.order, graph.edges)

    response = _score_worker().score(
        bit_graph,
        lengths=lengths,
        witness_cap=witness_cap,
        node_budget=node_budget,
    )

    by_length = {int(result.length): result for result in response.results}
    if set(by_length) != set(lengths):
        raise RuntimeError(
            f"scorer returned {sorted(by_length)}, expected {list(lengths)}"
        )

    components: list[ComponentScore] = []
    for length in lengths:
        result = by_length[length]
        raw_count = int(result.count)

        if raw_count >= witness_cap:
            observed = witness_cap
            status = STATUS_SATURATED
        elif bool(result.complete):
            observed = raw_count
            status = STATUS_EXACT
        else:
            observed = raw_count
            status = STATUS_BUDGET

        components.append(
            ComponentScore(
                length=length,
                observed=observed,
                status=status,
                nodes=int(result.nodes),
                elapsed_ns=int(result.elapsed_ns),
            )
        )

    return GraphScore(
        graph=graph,
        components=tuple(components),
        elapsed_seconds=time.perf_counter() - started,
    )


def score_status(score: GraphScore) -> str:
    if score.fully_exact:
        return "OK"

    sat = [
        f"C{component.length}"
        for component in score.components
        if component.status == STATUS_SATURATED
    ]
    bud = [
        f"C{component.length}"
        for component in score.components
        if component.status == STATUS_BUDGET
    ]
    parts: list[str] = []
    if sat:
        parts.append("CAP:" + ",".join(sat))
    if bud:
        parts.append("BUD:" + ",".join(bud))
    return " ".join(parts)


def disjoint_edge_pairs(edges: tuple[Edge, ...]) -> Iterator[tuple[Edge, Edge]]:
    for index, first in enumerate(edges):
        first_vertices = set(first)
        for second in edges[index + 1 :]:
            if first_vertices.isdisjoint(second):
                yield first, second


def two_switch_mutations(graph: Graph) -> list[Mutation]:
    edge_set = set(graph.edges)
    mutations: list[Mutation] = []

    for first, second in disjoint_edge_pairs(graph.edges):
        a, b = first
        c, d = second

        pairings = (
            ((norm_edge(a, c), norm_edge(b, d)), "ac_bd"),
            ((norm_edge(a, d), norm_edge(b, c)), "ad_bc"),
        )

        remaining = edge_set - {first, second}
        for added_edges, variant in pairings:
            if added_edges[0] == added_edges[1]:
                continue
            if added_edges[0] in remaining or added_edges[1] in remaining:
                continue

            mutations.append(
                Mutation(
                    family="two_switch",
                    removed_edges=(first, second),
                    added_edges=added_edges,
                    variant=variant,
                )
            )

    return mutations


def vertex_degrees(graph: Graph) -> list[int]:
    degrees = [0] * graph.order
    for u, v in graph.edges:
        degrees[u] += 1
        degrees[v] += 1
    return degrees


def endpoint_relocate_mutations(graph: Graph) -> list[Mutation]:
    """
    Move one endpoint of an existing edge while preserving order and edge count.

    Orient edge {fixed, donor}:
        remove (fixed, donor)
        add    (fixed, target)

    donor must have degree >=4, hence remains degree >=3 after losing the edge.
    target gains one degree. fixed keeps its degree.

    Connectivity is deliberately not inferred here; apply_mutation validates the
    resulting graph, so bridge-removal cases are safely rejected.
    """
    edge_set = set(graph.edges)
    degrees = vertex_degrees(graph)
    mutations: list[Mutation] = []

    for a, b in graph.edges:
        for fixed, donor in ((a, b), (b, a)):
            if degrees[donor] < 4:
                continue

            removed = norm_edge(fixed, donor)

            for target in range(graph.order):
                if target == fixed or target == donor:
                    continue

                added = norm_edge(fixed, target)
                if added in edge_set:
                    continue

                mutations.append(
                    Mutation(
                        family="endpoint_relocate",
                        removed_edges=(removed,),
                        added_edges=(added,),
                        variant=f"fixed={fixed}/donor={donor}/target={target}",
                    )
                )

    return mutations


def mutations_for_family(graph: Graph, family: str) -> list[Mutation]:
    if family == "two_switch":
        return two_switch_mutations(graph)
    if family == "endpoint_relocate":
        return endpoint_relocate_mutations(graph)
    raise ValueError(f"unsupported family {family}")


def apply_mutation(graph: Graph, mutation: Mutation) -> Graph:
    edge_set = set(graph.edges)

    for edge in mutation.removed_edges:
        edge = norm_edge(*edge)
        if edge not in edge_set:
            raise ValueError(f"missing removed edge {edge}")

    for edge in mutation.removed_edges:
        edge_set.remove(norm_edge(*edge))

    for edge in mutation.added_edges:
        edge = norm_edge(*edge)
        if edge in edge_set:
            raise ValueError(f"duplicate added edge {edge}")
        edge_set.add(edge)

    candidate = Graph.from_edges(graph.order, edge_set)
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)

    if candidate.order != graph.order:
        raise RuntimeError(f"{mutation.family} changed graph order")
    if len(candidate.edges) != len(graph.edges):
        raise RuntimeError(f"{mutation.family} changed edge count")

    return candidate


@dataclass(slots=True)
class CandidateBucket:
    parent: BeamState
    family: str
    mutations: list[Mutation]


def build_buckets(
    beam: list[BeamState],
    *,
    families: tuple[str, ...],
    depth: int,
    seed: int,
    limit_per_family_parent: int,
) -> list[CandidateBucket]:
    buckets: list[CandidateBucket] = []

    for parent in beam:
        parent_seed = int(parent.score.graph.graph_hash[:16], 16)

        for family_index, family in enumerate(families):
            mutations = mutations_for_family(parent.score.graph, family)

            rng = random.Random(
                seed
                ^ (depth << 24)
                ^ parent_seed
                ^ ((family_index + 1) * 0x9E3779B97F4A7C15)
            )
            rng.shuffle(mutations)

            if limit_per_family_parent:
                del mutations[limit_per_family_parent:]

            buckets.append(
                CandidateBucket(
                    parent=parent,
                    family=family,
                    mutations=mutations,
                )
            )

    return buckets


def interleaved_candidates(
    buckets: list[CandidateBucket],
    *,
    families: tuple[str, ...],
) -> Iterator[tuple[BeamState, Mutation]]:
    """
    Round-robin by family, then parent, then proposal index.

    This keeps a time-limited prefix balanced if one family has a much larger
    proposal space.
    """
    by_family: dict[str, list[CandidateBucket]] = {
        family: [] for family in families
    }
    for bucket in buckets:
        by_family[bucket.family].append(bucket)

    maximum = max((len(bucket.mutations) for bucket in buckets), default=0)

    for index in range(maximum):
        for family in families:
            for bucket in by_family[family]:
                if index < len(bucket.mutations):
                    yield bucket.parent, bucket.mutations[index]


def family_depth_stats(
    states: list[BeamState],
    families: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}

    for family in families:
        family_states = [
            state
            for state in states
            if state.mutation is not None and state.mutation.family == family
        ]

        exact = [
            state for state in family_states if state.score.fully_exact
        ]
        deltas = [
            (delta, state)
            for state in exact
            if (delta := delta_total(state)) is not None
        ]
        improving = [(delta, state) for delta, state in deltas if delta < 0]
        neutral = sum(delta == 0 for delta, _ in deltas)

        best_pair = (
            min(
                deltas,
                key=lambda pair: (
                    pair[0],
                    rank_key(pair[1]),
                    pair[1].score.graph.graph_hash,
                ),
            )
            if deltas
            else None
        )

        result[family] = {
            "scored": len(family_states),
            "exact": len(exact),
            "improving": len(improving),
            "neutral": neutral,
            "best_pair": best_pair,
        }

    return result



def component_profile(score: GraphScore) -> tuple[int, ...]:
    return tuple(
        component.observed
        for component in sorted(score.components, key=lambda item: item.length)
    )


def graph_adjacency(graph: Graph) -> tuple[tuple[int, ...], ...]:
    adjacency: list[list[int]] = [[] for _ in range(graph.order)]
    for u, v in graph.edges:
        adjacency[u].append(v)
        adjacency[v].append(u)
    return tuple(tuple(sorted(neighbors)) for neighbors in adjacency)


def graph_adjacency_masks(
    order: int,
    edges: tuple[Edge, ...],
) -> tuple[int, ...]:
    masks = [0] * order
    for u, v in edges:
        masks[u] |= 1 << v
        masks[v] |= 1 << u
    return tuple(masks)


def enumerate_exact_cycles_payload(
    order: int,
    edges: tuple[Edge, ...],
    *,
    length: int,
    expected_count: int,
) -> tuple[tuple[Edge, ...], ...]:
    """
    Recover exactly expected_count canonical simple cycles.

    Uses a bit-mask DFS and stops immediately once the scorer-proved count has
    been recovered. C4 uses a specialized common-neighbor implementation.
    """
    if expected_count == 0:
        return ()
    if expected_count < 0:
        raise ValueError("expected_count must be >= 0")

    if length == 4:
        # Implement C4 directly but retain explicit edge sets.
        masks = graph_adjacency_masks(order, edges)
        found_edges: set[tuple[Edge, ...]] = set()
        for u in range(order):
            for v in range(u + 1, order):
                common_mask = masks[u] & masks[v]
                common: list[int] = []
                while common_mask:
                    low = common_mask & -common_mask
                    common.append(low.bit_length() - 1)
                    common_mask ^= low

                for i in range(len(common)):
                    x = common[i]
                    for y in common[i + 1 :]:
                        if len({u, v, x, y}) != 4:
                            continue
                        cycle = tuple(
                            sorted(
                                (
                                    norm_edge(u, x),
                                    norm_edge(x, v),
                                    norm_edge(v, y),
                                    norm_edge(y, u),
                                )
                            )
                        )
                        found_edges.add(cycle)

        if len(found_edges) != expected_count:
            raise RuntimeError(
                f"C4 reconstruction mismatch: found {len(found_edges)}, "
                f"scorer proved {expected_count}"
            )
        return tuple(sorted(found_edges))

    adjacency_masks = graph_adjacency_masks(order, edges)
    all_vertices_mask = (1 << order) - 1
    found: list[tuple[Edge, ...]] = []
    stop = False

    for start in range(order):
        if stop:
            break

        start_bit = 1 << start
        # start must be the minimum cycle vertex.
        allowed_mask = all_vertices_mask & ~((1 << (start + 1)) - 1)
        path = [start]

        def dfs(current: int, used_mask: int) -> None:
            nonlocal stop
            if stop:
                return

            if len(path) == length:
                if adjacency_masks[current] & start_bit:
                    if path[1] < path[-1]:
                        cycle = tuple(
                            sorted(
                                norm_edge(
                                    path[index],
                                    path[(index + 1) % length],
                                )
                                for index in range(length)
                            )
                        )
                        found.append(cycle)
                        if len(found) == expected_count:
                            stop = True
                return

            candidates = adjacency_masks[current] & allowed_mask & ~used_mask

            # Final extension must close back to start.
            if len(path) == length - 1:
                closing = 0
                scan = candidates
                while scan:
                    low = scan & -scan
                    vertex = low.bit_length() - 1
                    if adjacency_masks[vertex] & start_bit:
                        closing |= low
                    scan ^= low
                candidates = closing

            while candidates and not stop:
                low = candidates & -candidates
                nxt = low.bit_length() - 1
                candidates ^= low

                path.append(nxt)
                dfs(nxt, used_mask | low)
                path.pop()

        dfs(start, start_bit)

    if len(found) != expected_count:
        raise RuntimeError(
            f"cycle reconstruction mismatch for C{length}: "
            f"found {len(found)}, scorer proved {expected_count}"
        )

    if len(set(found)) != len(found):
        raise RuntimeError(f"duplicate canonical C{length} cycles reconstructed")

    return tuple(found)


def minimum_hitting_set_masks(
    item_masks: dict[object, int],
    *,
    cycle_count: int,
) -> tuple[int, tuple[object, ...]]:
    if cycle_count == 0:
        return 0, ()

    target = (1 << cycle_count) - 1
    infinity = cycle_count + 1
    distance = [infinity] * (target + 1)
    previous: list[tuple[int, object] | None] = [None] * (target + 1)
    distance[0] = 0

    items = tuple(item_masks.items())

    for mask in range(target + 1):
        if distance[mask] == infinity:
            continue
        next_distance = distance[mask] + 1
        for item, item_mask in items:
            nxt = mask | item_mask
            if next_distance < distance[nxt]:
                distance[nxt] = next_distance
                previous[nxt] = (mask, item)

    if distance[target] == infinity:
        raise RuntimeError("failed to construct hitting set")

    chosen: list[object] = []
    mask = target
    while mask:
        step = previous[mask]
        if step is None:
            raise RuntimeError("broken hitting-set predecessor chain")
        prior_mask, item = step
        chosen.append(item)
        mask = prior_mask

    return distance[target], tuple(chosen)


def compute_structural_metrics_payload(
    payload: tuple[
        int,
        tuple[Edge, ...],
        tuple[tuple[int, int], ...],
        int,
    ],
) -> StructuralMetrics:
    """
    Process-safe CPU-bound structural analysis.

    payload:
        (order, edges, ((length, exact_count), ...), max_length)
    """
    order, edges, components, max_length = payload

    all_cycles: list[tuple[Edge, ...]] = []
    skipped_cycle_count = 0

    for length, count in components:
        if count == 0:
            continue
        if length > max_length:
            skipped_cycle_count += count
            continue

        cycles = enumerate_exact_cycles_payload(
            order,
            edges,
            length=length,
            expected_count=count,
        )
        all_cycles.extend(cycles)

    analyzed = len(all_cycles)
    complete = skipped_cycle_count == 0

    if analyzed == 0:
        return StructuralMetrics(
            complete=complete,
            tau_edge=0,
            tau_vertex=0,
            forbidden_edge_union=0,
            forbidden_vertex_union=0,
            max_cycles_per_edge=0,
            max_cycles_per_vertex=0,
            hitting_edges=(),
            hitting_vertices=(),
            analyzed_cycle_count=0,
            skipped_cycle_count=skipped_cycle_count,
        )

    edge_loads: Counter[Edge] = Counter()
    vertex_loads: Counter[int] = Counter()
    edge_masks: dict[Edge, int] = {}
    vertex_masks: dict[int, int] = {}

    for cycle_index, cycle in enumerate(all_cycles):
        bit = 1 << cycle_index
        vertices = {vertex for edge in cycle for vertex in edge}

        for edge in cycle:
            edge_loads[edge] += 1
            edge_masks[edge] = edge_masks.get(edge, 0) | bit

        for vertex in vertices:
            vertex_loads[vertex] += 1
            vertex_masks[vertex] = vertex_masks.get(vertex, 0) | bit

    tau_edge, hitting_edges_raw = minimum_hitting_set_masks(
        edge_masks,
        cycle_count=analyzed,
    )
    tau_vertex, hitting_vertices_raw = minimum_hitting_set_masks(
        vertex_masks,
        cycle_count=analyzed,
    )

    return StructuralMetrics(
        complete=complete,
        tau_edge=tau_edge,
        tau_vertex=tau_vertex,
        forbidden_edge_union=len(edge_loads),
        forbidden_vertex_union=len(vertex_loads),
        max_cycles_per_edge=max(edge_loads.values()),
        max_cycles_per_vertex=max(vertex_loads.values()),
        hitting_edges=tuple(hitting_edges_raw),  # type: ignore[arg-type]
        hitting_vertices=tuple(hitting_vertices_raw),  # type: ignore[arg-type]
        analyzed_cycle_count=analyzed,
        skipped_cycle_count=skipped_cycle_count,
    )


def structural_payload(
    state: BeamState,
    *,
    max_length: int,
) -> tuple[int, tuple[Edge, ...], tuple[tuple[int, int], ...], int]:
    score = state.score
    if not score.fully_exact or score.total is None:
        raise ValueError("structural metrics require an exact score")

    components = tuple(
        (component.length, component.observed)
        for component in sorted(score.components, key=lambda item: item.length)
    )
    return (
        score.graph.order,
        score.graph.edges,
        components,
        max_length,
    )


def compute_structural_metrics(
    state: BeamState,
    *,
    max_length: int,
) -> StructuralMetrics:
    return compute_structural_metrics_payload(
        structural_payload(state, max_length=max_length)
    )



def structural_rank_key(
    state: BeamState,
    metrics: StructuralMetrics,
) -> tuple[object, ...]:
    score = state.score
    if score.total is None or score.weighted is None:
        return (1,)

    return (
        0,
        score.total,
        0 if metrics.complete else 1,
        metrics.tau_edge,
        metrics.tau_vertex,
        metrics.forbidden_edge_union,
        metrics.forbidden_vertex_union,
        -metrics.max_cycles_per_edge,
        -metrics.max_cycles_per_vertex,
        score.weighted,
        component_profile(score),
        score.graph.graph_hash,
    )



def _stratified_take(
    states: list[BeamState],
    *,
    count: int,
    excluded_hashes: set[str],
) -> list[BeamState]:
    if count <= 0:
        return []

    buckets: dict[
        tuple[tuple[int, ...], str, str],
        list[BeamState],
    ] = defaultdict(list)

    for state in states:
        family = "-" if state.mutation is None else state.mutation.family
        parent_hash = (
            "-"
            if state.parent_score is None
            else state.parent_score.graph.graph_hash
        )
        buckets[
            (component_profile(state.score), family, parent_hash)
        ].append(state)

    for bucket in buckets.values():
        bucket.sort(key=lambda state: state.score.graph.graph_hash)

    keys = sorted(buckets)
    chosen: list[BeamState] = []
    index = 0

    while keys and len(chosen) < count:
        next_keys: list[tuple[tuple[int, ...], str, str]] = []
        for key in keys:
            bucket = buckets[key]
            if index < len(bucket):
                state = bucket[index]
                graph_hash = state.score.graph.graph_hash
                if graph_hash not in excluded_hashes:
                    chosen.append(state)
                    excluded_hashes.add(graph_hash)
                    if len(chosen) >= count:
                        break
            if index + 1 < len(bucket):
                next_keys.append(key)

        index += 1
        keys = next_keys

    return chosen


def stratified_structural_shortlist(
    states: list[BeamState],
    *,
    global_best_total: int,
    escape_height: int,
    limit: int,
    escape_quota: int,
) -> tuple[list[BeamState], int, int]:
    """
    Reserve structural-analysis capacity for both the current optimum and the
    escape band. This prevents TOTAL=best candidates from starving TOTAL=best+1.

    Returns:
        (shortlist, best_count, escape_count)
    """
    barrier = global_best_total + escape_height

    eligible = [
        state
        for state in states
        if state.score.fully_exact and state.score.total is not None
    ]

    best_states = [
        state
        for state in eligible
        if state.score.total == global_best_total
    ]
    escape_states = [
        state
        for state in eligible
        if global_best_total < state.score.total <= barrier
    ]

    best_quota = max(0, limit - escape_quota)
    used: set[str] = set()

    selected_best = _stratified_take(
        best_states,
        count=best_quota,
        excluded_hashes=used,
    )
    selected_escape = _stratified_take(
        escape_states,
        count=escape_quota,
        excluded_hashes=used,
    )

    # If one class cannot fill its quota, give unused slots to the other.
    remaining = limit - len(selected_best) - len(selected_escape)
    if remaining > 0:
        selected_best.extend(
            _stratified_take(
                best_states,
                count=remaining,
                excluded_hashes=used,
            )
        )
        remaining = limit - len(selected_best) - len(selected_escape)

    if remaining > 0:
        selected_escape.extend(
            _stratified_take(
                escape_states,
                count=remaining,
                excluded_hashes=used,
            )
        )

    shortlist = selected_best + selected_escape
    return shortlist, len(selected_best), len(selected_escape)



def choose_diverse(
    candidates: list[BeamState],
    *,
    metrics_by_hash: dict[str, StructuralMetrics],
    count: int,
    excluded_hashes: set[str],
    signature_mode: str,
) -> list[BeamState]:
    if count <= 0:
        return []

    ordered = sorted(
        [
            state
            for state in candidates
            if state.score.graph.graph_hash in metrics_by_hash
            and state.score.graph.graph_hash not in excluded_hashes
        ],
        key=lambda state: structural_rank_key(
            state,
            metrics_by_hash[state.score.graph.graph_hash],
        ),
    )

    chosen: list[BeamState] = []
    signatures: set[tuple[object, ...]] = set()

    def signature(state: BeamState) -> tuple[object, ...]:
        metrics = metrics_by_hash[state.score.graph.graph_hash]
        if signature_mode == "profile":
            return (component_profile(state.score),)
        if signature_mode == "structural":
            return (
                component_profile(state.score),
                metrics.complete,
                metrics.tau_edge,
                metrics.tau_vertex,
                metrics.forbidden_edge_union,
                metrics.forbidden_vertex_union,
                metrics.max_cycles_per_edge,
                metrics.max_cycles_per_vertex,
            )
        raise ValueError(signature_mode)

    # First pass: one state per requested signature.
    for state in ordered:
        sig = signature(state)
        if sig in signatures:
            continue
        chosen.append(state)
        signatures.add(sig)
        excluded_hashes.add(state.score.graph.graph_hash)
        if len(chosen) >= count:
            return chosen

    # Second pass: fill any remaining slots with the structurally best states.
    for state in ordered:
        graph_hash = state.score.graph.graph_hash
        if graph_hash in excluded_hashes:
            continue
        chosen.append(state)
        excluded_hashes.add(graph_hash)
        if len(chosen) >= count:
            break

    return chosen


def select_structural_escape_beam(
    analyzed: list[BeamState],
    *,
    metrics_by_hash: dict[str, StructuralMetrics],
    beam_width: int,
    main_lanes: int,
    structural_lanes: int,
    escape_lanes: int,
    escape_height: int,
    global_best_total: int,
) -> tuple[list[BeamState], dict[str, str]]:
    barrier = global_best_total + escape_height
    analyzed = [
        state
        for state in analyzed
        if state.score.total is not None
        and state.score.total <= barrier
        and state.score.graph.graph_hash in metrics_by_hash
    ]

    selected: list[BeamState] = []
    lane_by_hash: dict[str, str] = {}
    used: set[str] = set()

    best_total_states = [
        state for state in analyzed if state.score.total == global_best_total
    ]

    # Main exploitation lanes: structural ranking at the current best TOTAL.
    for state in sorted(
        best_total_states,
        key=lambda item: structural_rank_key(
            item, metrics_by_hash[item.score.graph.graph_hash]
        ),
    )[:main_lanes]:
        graph_hash = state.score.graph.graph_hash
        selected.append(state)
        used.add(graph_hash)
        lane_by_hash[graph_hash] = "main"

    # Structural/Pareto lanes: deliberately preserve different profiles and
    # concentration signatures at the best TOTAL.
    structural = choose_diverse(
        best_total_states,
        metrics_by_hash=metrics_by_hash,
        count=structural_lanes,
        excluded_hashes=used,
        signature_mode="structural",
    )
    for state in structural:
        selected.append(state)
        lane_by_hash[state.score.graph.graph_hash] = "structural"

    # Escape lanes: only genuinely above the current best, inside the fixed
    # absolute barrier. Preserve profile diversity first.
    escape_candidates = [
        state
        for state in analyzed
        if state.score.total is not None
        and global_best_total < state.score.total <= barrier
    ]
    escape = choose_diverse(
        escape_candidates,
        metrics_by_hash=metrics_by_hash,
        count=escape_lanes,
        excluded_hashes=used,
        signature_mode="profile",
    )
    for state in escape:
        selected.append(state)
        lane_by_hash[state.score.graph.graph_hash] = "escape"

    # Fill any unused beam slots with the best remaining analyzed state,
    # irrespective of lane class, while respecting the escape barrier.
    if len(selected) < beam_width:
        remaining = sorted(
            [
                state
                for state in analyzed
                if state.score.graph.graph_hash not in used
            ],
            key=lambda item: structural_rank_key(
                item, metrics_by_hash[item.score.graph.graph_hash]
            ),
        )
        for state in remaining:
            if len(selected) >= beam_width:
                break
            graph_hash = state.score.graph.graph_hash
            selected.append(state)
            used.add(graph_hash)
            lane_by_hash[graph_hash] = "fill"

    return selected, lane_by_hash



def rank_key(state: BeamState) -> tuple[int, ...]:
    score = state.score
    if score.total is None or score.weighted is None:
        return (1, 0, 0)

    component_vector = tuple(
        component.observed
        for component in sorted(score.components, key=lambda item: item.length)
    )
    return (
        0,
        score.total,
        score.weighted,
        *component_vector,
    )



def delta_total(state: BeamState) -> int | None:
    if state.parent_score is None:
        return None
    parent_total = state.parent_score.total
    child_total = state.score.total
    if parent_total is None or child_total is None:
        return None
    return child_total - parent_total


def components_text(score: GraphScore) -> str:
    return " ".join(
        f"C{component.length}={component.observed}"
        for component in sorted(score.components, key=lambda item: item.length)
    )


def save_graph(path: Path, state: BeamState) -> None:
    payload = {
        **state.score.graph.record(),
        "polish_depth": state.depth,
        "score": {
            "total": state.score.total,
            "weighted": state.score.weighted,
            "components": {
                str(component.length): {
                    "observed": component.observed,
                    "status": component.status,
                }
                for component in state.score.components
            },
        },
        "last_mutation": (
            None
            if state.mutation is None
            else {
                "family": state.mutation.family,
                "variant": state.mutation.variant,
                "removed_edges": [
                    list(edge) for edge in state.mutation.removed_edges
                ],
                "added_edges": [
                    list(edge) for edge in state.mutation.added_edges
                ],
            }
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def lineage_path(state: BeamState) -> list[BeamState]:
    path: list[BeamState] = []
    current: BeamState | None = state
    while current is not None:
        path.append(current)
        current = current.parent_state
    path.reverse()
    return path


def save_lineage(path: Path, state: BeamState) -> None:
    lineage = lineage_path(state)
    payload = {
        "schema_version": "graphoratory.heg_fixed_order_polish.v1",
        "order": state.score.graph.order,
        "root_hash": lineage[0].score.graph.graph_hash,
        "final_hash": state.score.graph.graph_hash,
        "states": [],
    }

    for item in lineage:
        payload["states"].append(
            {
                "depth": item.depth,
                "graph_hash": item.score.graph.graph_hash,
                "score": {
                    "total": item.score.total,
                    "weighted": item.score.weighted,
                    "components": {
                        str(component.length): component.observed
                        for component in item.score.components
                    },
                },
                "mutation": (
                    None
                    if item.mutation is None
                    else {
                        "family": item.mutation.family,
                        "variant": item.mutation.variant,
                        "removed_edges": [
                            list(edge) for edge in item.mutation.removed_edges
                        ],
                        "added_edges": [
                            list(edge) for edge in item.mutation.added_edges
                        ],
                    }
                ),
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )



def augment_saved_graph_with_structural(
    path: Path,
    *,
    state: BeamState,
    metrics: StructuralMetrics | None,
) -> None:
    if metrics is None or not path.exists():
        return

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["structural"] = {
        "complete": metrics.complete,
        "tau_edge": metrics.tau_edge,
        "tau_vertex": metrics.tau_vertex,
        "forbidden_edge_union": metrics.forbidden_edge_union,
        "forbidden_vertex_union": metrics.forbidden_vertex_union,
        "max_cycles_per_edge": metrics.max_cycles_per_edge,
        "max_cycles_per_vertex": metrics.max_cycles_per_vertex,
        "hitting_edges": [list(edge) for edge in metrics.hitting_edges],
        "hitting_vertices": list(metrics.hitting_vertices),
        "analyzed_cycle_count": metrics.analyzed_cycle_count,
        "skipped_cycle_count": metrics.skipped_cycle_count,
        "cycle_count": metrics.cycle_count,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )



def maybe_save_hit(args: argparse.Namespace, state: BeamState) -> Path | None:
    if not state.score.fully_exact or state.score.total != 0:
        return None

    args.hit_dir.mkdir(parents=True, exist_ok=True)
    path = args.hit_dir / (
        f"heg-zero-fixed-order-{state.score.graph.order}-"
        f"{state.score.graph.graph_hash[:8]}.json"
    )
    save_graph(path, state)
    return path


def print_final_lineage(state: BeamState) -> None:
    lineage = lineage_path(state)

    console.print()
    console.print(
        f"[bold]FINAL POLISH LINEAGE[/bold] "
        f"order={state.score.graph.order} depth={state.depth}"
    )
    console.print(
        f"{'DEPTH':>5} {'FAMILY':>18} {'TOTAL':>5} {'Δ':>4} {'WEIGHTED':>8} "
        f"{'COMPONENTS':<35} {'HASH':>8}"
    )

    for item in lineage:
        delta = delta_total(item)
        delta_text = "-" if delta is None else f"{delta:+d}"
        family = "-" if item.mutation is None else item.mutation.family
        console.print(
            f"{item.depth:>5} "
            f"{family:>18} "
            f"{str(item.score.total):>5} "
            f"{delta_text:>4} "
            f"{str(item.score.weighted):>8} "
            f"{components_text(item.score):<35} "
            f"{item.score.graph.graph_hash[:8]:>8}"
        )
        if item.mutation is not None:
            console.print(f"       {item.mutation.label()}")


def main() -> int:
    args = parse_args()
    start_graph = load_start_graph(args)

    console.print(
        f"[bold]HEG fixed-order structural escape[/bold] "
        f"order={start_graph.order} beam={args.beam_width} "
        f"lanes={args.main_lanes}+{args.structural_lanes}+{args.escape_lanes} "
        f"escape_height={args.escape_height} structural_pool={args.structural_pool} "
        f"(escape_reserved={args.escape_structural_pool}) "
        f"structural_workers={args.structural_workers} "
        f"structural_C<={args.max_structural_cycle_length} "
        f"max_depth={args.max_depth} workers={args.workers} "
        f"families={','.join(args.families)}"
    )
    console.print(
        f"step_budget={args.step_seconds or 'unlimited'}s "
        f"total_budget={args.total_seconds or 'unlimited'}s "
        f"node_budget={args.node_budget:,} cap={args.witness_cap:,}"
    )

    started = time.perf_counter()
    total_deadline = (
        started + args.total_seconds
        if args.total_seconds > 0
        else math.inf
    )
    max_inflight = args.workers * args.inflight_per_worker

    spawn_context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=args.structural_workers,
        mp_context=spawn_context,
    ) as structural_executor, ThreadPoolExecutor(
        max_workers=args.workers,
        initializer=_thread_worker_init,
        thread_name_prefix="heg-polish-score",
    ) as executor:
        initial_score = executor.submit(
            score_graph,
            start_graph,
            witness_cap=args.witness_cap,
            node_budget=args.node_budget,
        ).result()

        if not initial_score.fully_exact:
            raise RuntimeError(
                "starting graph did not score exactly: "
                f"{score_status(initial_score)}"
            )

        root = BeamState(
            score=initial_score,
            parent_state=None,
            mutation=None,
            depth=0,
        )
        beam = [root]
        best = root
        visited: set[str] = {start_graph.graph_hash}
        structural_cache: dict[str, StructuralMetrics] = {}

        root_metrics = compute_structural_metrics(
            root,
            max_length=args.max_structural_cycle_length,
        )
        structural_cache[start_graph.graph_hash] = root_metrics

        console.print(
            f"START total={initial_score.total} weighted={initial_score.weighted} "
            f"{components_text(initial_score)} {root_metrics.compact()} "
            f"hash={start_graph.graph_hash[:8]}"
        )

        if args.start_graph is None and args.preset == PRESET_REWIRE2_N26:
            expected = {4: 7, 8: 1, 16: 0}
            actual = {
                component.length: component.observed
                for component in initial_score.components
            }
            if any(actual.get(length) != value for length, value in expected.items()):
                raise RuntimeError(
                    f"preset score mismatch: got {actual}, expected at least {expected}"
                )
            console.print(
                "[dim]Preset verified: expected n=26 C4=7 C8=1 C16=0.[/dim]"
            )

        hit = maybe_save_hit(args, root)
        if hit is not None:
            console.print(
                f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] saved: {hit}"
            )
            return 0

        for depth in range(1, args.max_depth + 1):
            now = time.perf_counter()
            if now >= total_deadline:
                console.print("[yellow]Total time budget exhausted.[/yellow]")
                break

            step_started = now
            step_deadline = min(
                total_deadline,
                (
                    step_started + args.step_seconds
                    if args.step_seconds > 0
                    else math.inf
                ),
            )

            buckets = build_buckets(
                beam,
                families=args.families,
                depth=depth,
                seed=args.seed,
                limit_per_family_parent=args.max_mutations_per_family_parent,
            )
            proposal_space = sum(len(bucket.mutations) for bucket in buckets)
            stream = interleaved_candidates(
                buckets,
                families=args.families,
            )

            metadata: dict[str, tuple[BeamState, Mutation]] = {}
            inflight: dict[Future[GraphScore], str] = {}
            completed: list[BeamState] = []

            submitted = 0
            duplicate_or_visited = 0
            invalid = 0
            exhausted_stream = False

            def submit_until_full() -> None:
                nonlocal exhausted_stream
                nonlocal submitted
                nonlocal duplicate_or_visited
                nonlocal invalid

                while (
                    not exhausted_stream
                    and len(inflight) < max_inflight
                    and time.perf_counter() < step_deadline
                ):
                    try:
                        parent, mutation = next(stream)
                    except StopIteration:
                        exhausted_stream = True
                        break

                    try:
                        candidate = apply_mutation(parent.score.graph, mutation)
                    except (ValueError, RuntimeError):
                        invalid += 1
                        continue

                    graph_hash = candidate.graph_hash
                    if graph_hash in visited or graph_hash in metadata:
                        duplicate_or_visited += 1
                        continue

                    future = executor.submit(
                        score_graph,
                        candidate,
                        witness_cap=args.witness_cap,
                        node_budget=args.node_budget,
                    )
                    metadata[graph_hash] = (parent, mutation)
                    inflight[future] = graph_hash
                    submitted += 1

            submit_until_full()

            while inflight:
                timeout = max(0.0, step_deadline - time.perf_counter())
                if timeout == 0.0:
                    break

                done, _ = wait(
                    tuple(inflight),
                    timeout=timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    break

                for future in done:
                    graph_hash = inflight.pop(future)
                    parent, mutation = metadata.pop(graph_hash)
                    score = future.result()
                    completed.append(
                        BeamState(
                            score=score,
                            parent_state=parent,
                            mutation=mutation,
                            depth=depth,
                        )
                    )

                submit_until_full()

            running_tail: list[Future[GraphScore]] = []
            for future in list(inflight):
                if not future.cancel():
                    running_tail.append(future)

            if running_tail:
                done_tail, _ = wait(tuple(running_tail))
                for future in done_tail:
                    graph_hash = inflight.pop(future)
                    parent, mutation = metadata.pop(graph_hash)
                    score = future.result()
                    completed.append(
                        BeamState(
                            score=score,
                            parent_state=parent,
                            mutation=mutation,
                            depth=depth,
                        )
                    )

            if not completed:
                console.print(
                    f"[yellow]Depth {depth}: no candidate score completed.[/yellow]"
                )
                break

            # Deduplicate within this generation by graph hash.
            unique: dict[str, BeamState] = {}
            for state in completed:
                graph_hash = state.score.graph.graph_hash
                incumbent = unique.get(graph_hash)
                if incumbent is None or rank_key(state) < rank_key(incumbent):
                    unique[graph_hash] = state
            pool = list(unique.values())

            exact_pool = [state for state in pool if state.score.fully_exact]
            if not exact_pool:
                console.print(
                    f"[yellow]Depth {depth}: no exact candidate scores.[/yellow]"
                )
                break

            improving = 0
            neutral = 0
            uphill = 0

            for state in exact_pool:
                parent_total = state.parent_score.total if state.parent_score else None
                child_total = state.score.total
                if parent_total is None or child_total is None:
                    continue

                delta = child_total - parent_total
                if delta < 0:
                    improving += 1
                elif delta == 0:
                    neutral += 1
                else:
                    uphill += 1

            # Exact raw TOTAL defines the scientific incumbent.
            depth_best_total = min(
                state.score.total
                for state in exact_pool
                if state.score.total is not None
            )
            assert depth_best_total is not None

            current_best_total = best.score.total
            if current_best_total is None:
                raise RuntimeError("global best unexpectedly became inexact")

            global_best_total = min(current_best_total, depth_best_total)
            barrier = global_best_total + args.escape_height

            # Detect zero before any structural shortlist can discard it.
            zero_hits = [state for state in exact_pool if state.score.total == 0]
            if zero_hits:
                hit_state = min(
                    zero_hits,
                    key=lambda state: state.score.graph.graph_hash,
                )
                hit_metrics = compute_structural_metrics(
                    hit_state,
                    max_length=args.max_structural_cycle_length,
                )
                structural_cache[hit_state.score.graph.graph_hash] = hit_metrics
                path = maybe_save_hit(args, hit_state)
                if path is not None:
                    augment_saved_graph_with_structural(
                        path,
                        state=hit_state,
                        metrics=hit_metrics,
                    )
                console.print(
                    f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] "
                    f"order={hit_state.score.graph.order} depth={depth} "
                    f"saved: {path}"
                )
                print_final_lineage(hit_state)
                if args.save_best is not None:
                    save_graph(args.save_best, hit_state)
                    augment_saved_graph_with_structural(
                        args.save_best,
                        state=hit_state,
                        metrics=hit_metrics,
                    )
                if args.save_lineage is not None:
                    save_lineage(args.save_lineage, hit_state)
                return 0

            shortlist, shortlist_best_count, shortlist_escape_count = (
                stratified_structural_shortlist(
                    exact_pool,
                    global_best_total=global_best_total,
                    escape_height=args.escape_height,
                    limit=args.structural_pool,
                    escape_quota=args.escape_structural_pool,
                )
            )

            missing = [
                state
                for state in shortlist
                if state.score.graph.graph_hash not in structural_cache
            ]
            if missing:
                structural_futures = {
                    structural_executor.submit(
                        compute_structural_metrics_payload,
                        structural_payload(
                            state,
                            max_length=args.max_structural_cycle_length,
                        ),
                    ): state
                    for state in missing
                }
                for future, state in structural_futures.items():
                    metrics = future.result()
                    structural_cache[state.score.graph.graph_hash] = metrics

            analyzed = [
                state
                for state in shortlist
                if state.score.graph.graph_hash in structural_cache
            ]

            if not analyzed:
                console.print(
                    f"[yellow]Depth {depth}: structural shortlist is empty.[/yellow]"
                )
                break

            # Update the global best state at minimum TOTAL using structural
            # concentration, not the historical weighted tie-break.
            best_candidates = [
                state
                for state in analyzed
                if state.score.total == global_best_total
            ]
            if best.score.total == global_best_total:
                best_candidates.append(best)

            # Ensure the previous incumbent has metrics even if it was not in the
            # current shortlist.
            if best.score.graph.graph_hash not in structural_cache:
                structural_cache[best.score.graph.graph_hash] = (
                    compute_structural_metrics(
                        best,
                        max_length=args.max_structural_cycle_length,
                    )
                )

            if best_candidates:
                best = min(
                    best_candidates,
                    key=lambda state: structural_rank_key(
                        state,
                        structural_cache[state.score.graph.graph_hash],
                    ),
                )

            beam, lane_by_hash = select_structural_escape_beam(
                analyzed,
                metrics_by_hash=structural_cache,
                beam_width=args.beam_width,
                main_lanes=args.main_lanes,
                structural_lanes=args.structural_lanes,
                escape_lanes=args.escape_lanes,
                escape_height=args.escape_height,
                global_best_total=global_best_total,
            )

            # Mark scored graphs visited only after selection calculations.
            visited.update(state.score.graph.graph_hash for state in exact_pool)

            best_child = min(
                analyzed,
                key=lambda state: structural_rank_key(
                    state,
                    structural_cache[state.score.graph.graph_hash],
                ),
            )
            best_child_metrics = structural_cache[
                best_child.score.graph.graph_hash
            ]
            best_metrics = structural_cache[best.score.graph.graph_hash]

            elapsed = time.perf_counter() - step_started
            best_delta = delta_total(best_child)

            lane_counts = Counter(lane_by_hash.values())

            console.print(
                f"DEPTH {depth:>2} "
                f"scored={len(pool)}/{proposal_space} "
                f"exact={len(exact_pool)} "
                f"improving={improving} neutral={neutral} uphill={uphill} "
                f"structural={len(analyzed)}/{len(shortlist)} "
                f"(best={shortlist_best_count},escape={shortlist_escape_count}) "
                f"best_child_total={best_child.score.total} "
                f"Δ={best_delta:+d} "
                f"global_best={best.score.total} "
                f"barrier<={barrier} "
                f"time={elapsed:.2f}s"
            )
            console.print(
                f"     best child {components_text(best_child.score)} "
                f"{best_child_metrics.compact()} "
                f"{best_child.mutation.label() if best_child.mutation else '-'} "
                f"hash={best_child.score.graph.graph_hash[:8]}"
            )
            console.print(
                f"     global best {components_text(best.score)} "
                f"{best_metrics.compact()} "
                f"hash={best.score.graph.graph_hash[:8]}"
            )
            console.print(
                f"     next beam={len(beam)} "
                f"main={lane_counts.get('main', 0)} "
                f"structural={lane_counts.get('structural', 0)} "
                f"escape={lane_counts.get('escape', 0)} "
                f"fill={lane_counts.get('fill', 0)}"
            )

            # Show the structurally best state for each component profile present
            # in the selected beam. This makes (6,1,0) vs (7,0,0) visible.
            profile_best: dict[tuple[int, ...], BeamState] = {}
            for state in beam:
                profile = component_profile(state.score)
                incumbent = profile_best.get(profile)
                if incumbent is None:
                    profile_best[profile] = state
                    continue
                if structural_rank_key(
                    state, structural_cache[state.score.graph.graph_hash]
                ) < structural_rank_key(
                    incumbent,
                    structural_cache[incumbent.score.graph.graph_hash],
                ):
                    profile_best[profile] = state

            for profile, state in sorted(profile_best.items()):
                metrics = structural_cache[state.score.graph.graph_hash]
                lane = lane_by_hash.get(state.score.graph.graph_hash, "?")
                console.print(
                    f"       profile={profile} lane={lane:<10} "
                    f"{metrics.compact()} "
                    f"hash={state.score.graph.graph_hash[:8]}"
                )

            depth_stats = family_depth_stats(exact_pool, args.families)
            for family in args.families:
                stat = depth_stats[family]
                best_pair = stat["best_pair"]
                if best_pair is None:
                    best_text = "n/a"
                else:
                    family_best_delta, family_best_state = best_pair
                    best_text = (
                        f"{family_best_delta:+d} "
                        f"total={family_best_state.score.total} "
                        f"hash={family_best_state.score.graph.graph_hash[:8]}"
                    )
                console.print(
                    f"       {family:<18} "
                    f"scored={stat['scored']:<6} "
                    f"exact={stat['exact']:<6} "
                    f"improving={stat['improving']:<5} "
                    f"neutral={stat['neutral']:<5} "
                    f"bestΔ={best_text}"
                )

            if invalid or duplicate_or_visited:
                console.print(
                    f"     invalid={invalid} "
                    f"duplicate/visited={duplicate_or_visited}"
                )

            if len(pool) < proposal_space:
                console.print(
                    f"     completed unique scores {len(pool)}/{proposal_space} "
                    "proposal slots before deadline/dedupe"
                )
                if submitted != len(pool):
                    console.print(
                        f"     submitted={submitted}; "
                        f"{submitted-len(pool)} did not contribute a final unique score"
                    )

            if not beam:
                console.print(
                    f"[yellow]Depth {depth}: no analyzed state remains inside "
                    f"barrier <= {barrier}.[/yellow]"
                )
                break

    if args.save_best is not None:
        save_graph(args.save_best, best)
        best_metrics = structural_cache.get(best.score.graph.graph_hash)
        augment_saved_graph_with_structural(
            args.save_best,
            state=best,
            metrics=best_metrics,
        )
        console.print(f"Best graph saved: {args.save_best}")

    if args.save_lineage is not None:
        save_lineage(args.save_lineage, best)
        console.print(f"Best lineage saved: {args.save_lineage}")

    print_final_lineage(best)

    final_metrics = structural_cache.get(best.score.graph.graph_hash)
    structural_text = (
        final_metrics.compact() if final_metrics is not None else "structural=n/a"
    )
    console.print(
        f"[bold]Done[/bold] order={best.score.graph.order} "
        f"best_total={best.score.total} "
        f"{components_text(best.score)} {structural_text} "
        f"depth={best.depth} elapsed={time.perf_counter()-started:.2f}s "
        f"hash={best.score.graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
