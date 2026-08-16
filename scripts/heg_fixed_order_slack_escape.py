#!/usr/bin/env python3
"""
HEG fixed-order slack-escape experiment.

Purpose
-------
The Markstroem n=24 seed reaches an exact plateau

    C4=3, C8=0, C16=0, TOTAL=3

inside the cubic m=36 manifold. With n=24 and delta>=3, m=36 forces every
vertex to have degree exactly three, so endpoint_relocate and the existing
vertex-surgery families have no degree slack.

This experiment deliberately permits a bounded edge-count excess and adds
atomic slack-assisted rewrites that can leave the cubic manifold while
preserving the scientific invariants at every scored state.

New mutation families
---------------------
slack_rewire1:
    remove one forbidden-cycle edge (fixed, donor), add a non-edge incident to
    donor to protect its degree, and relocate fixed to a new target. Net +1
    edge. This creates degree slack without passing through an invalid degree-2
    intermediate graph.

slack_rewire2:
    choose two forbidden-cycle edges from distinct covered cycles, orient them
    with distinct donors, add one donor-donor support edge, remove both selected
    edges, and reconnect both fixed endpoints to new targets. Net +1 edge. The
    move can attack two distinct forbidden cycles atomically.

excess_edge_remove:
    when edge excess exists, remove an edge whose two endpoints both have
    degree >=4. Net -1 edge; full connectivity/min-degree validation remains
    authoritative.

The search is bounded by --max-edge-excess (default 1), so for n=24 it explores
m=36 and m=37 only unless explicitly changed. The exact C4 admissible gate and
full HEG ScoreWorker remain authoritative.

Recommended Markstroem plateau run
----------------------------------

    uv run python scripts/heg_fixed_order_slack_escape.py \
      --start-graph markstroem_polish_best_n24.json \
      --beam-width 32 --main-lanes 16 --structural-lanes 8 --escape-lanes 8 \
      --escape-height 3 --max-edge-excess 1 \
      --structural-pool 128 --escape-structural-pool 64 \
      --structural-workers 4 --max-structural-cycle-length 8 \
      --max-depth 200 --step-seconds 120 --total-seconds 900 \
      --workers 16 --score-batch-size 64 --inflight-per-worker 4 \
      --families slack_rewire2,slack_rewire1,endpoint_relocate,two_switch,vertex_rewire2,coupled_switch2,excess_edge_remove \
      --guided-limit-per-parent 512 --coupled-scan-per-parent 4096 \
      --slack-limit-per-parent 4096 --hot-vertices-per-parent 8 \
      --node-budget 10000000 --witness-cap 1000000 \
      --save-best markstroem_slack_best_n24.json \
      --save-lineage markstroem_slack_lineage_n24.json
"""

from __future__ import annotations

import argparse
import multiprocessing
import atexit
import json
import math
import random
import threading
from itertools import combinations
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
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
SUPPORTED_FAMILIES = (
    "slack_rewire2",
    "slack_rewire1",
    "endpoint_relocate",
    "two_switch",
    "vertex_rewire2",
    "vertex_rewire3",
    "coupled_switch2",
    "excess_edge_remove",
)

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
    edge_cycle_masks: tuple[tuple[Edge, int], ...]
    vertex_cycle_masks: tuple[tuple[int, int], ...]
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
        "--guided-limit-per-parent",
        type=int,
        default=512,
        help=(
            "Maximum vertex_rewire2 proposals AND vertex_rewire3 proposals "
            "generated for each beam parent."
        ),
    )
    parser.add_argument(
        "--coupled-scan-per-parent",
        type=int,
        default=8192,
        help=(
            "Maximum coupled_switch2 mutation descriptors generated per beam "
            "parent before exact-C4 pruning in score processes."
        ),
    )
    parser.add_argument(
        "--slack-limit-per-parent",
        type=int,
        default=4096,
        help=(
            "Maximum slack_rewire1 proposals AND slack_rewire2 proposals "
            "generated per beam parent."
        ),
    )
    parser.add_argument(
        "--max-edge-excess",
        type=int,
        default=1,
        help=(
            "Maximum edges above ceil(3*n/2) allowed in scored states. "
            "For n=24, value 1 restricts the search to m=36 or m=37."
        ),
    )
    parser.add_argument(
        "--hot-vertices-per-parent",
        type=int,
        default=6,
        help="How many highest forbidden-cycle-load vertices may seed surgery.",
    )
    parser.add_argument(
        "--score-batch-size",
        type=int,
        default=64,
        help=(
            "Mutations scored sequentially by one persistent score-process lane "
            "per ProcessPool task."
        ),
    )
    parser.add_argument(
        "--inflight-per-worker",
        type=int,
        default=4,
        help="Queued score batches per scoring process.",
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
    if args.escape_lanes == 0 and args.escape_height == 0:
        # Explicit direct/neutral search: zero escape quota is expected.
        pass
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
    if args.slack_limit_per_parent < 0:
        parser.error("--slack-limit-per-parent must be >= 0")
    if args.max_edge_excess < 0:
        parser.error("--max-edge-excess must be >= 0")
    if args.guided_limit_per_parent < 1:
        parser.error("--guided-limit-per-parent must be >= 1")
    if args.coupled_scan_per_parent < 1:
        parser.error("--coupled-scan-per-parent must be >= 1")
    if args.hot_vertices_per_parent < 1:
        parser.error("--hot-vertices-per-parent must be >= 1")
    if args.score_batch_size < 1:
        parser.error("--score-batch-size must be >= 1")
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



def _other_endpoint(edge: Edge, vertex: int) -> int:
    u, v = edge
    if u == vertex:
        return v
    if v == vertex:
        return u
    raise ValueError(f"edge {edge} is not incident to {vertex}")


def _guided_removal_plans(
    graph: Graph,
    metrics: StructuralMetrics,
    *,
    arity: int,
    hot_vertices_limit: int,
) -> list[tuple[int, tuple[Edge, ...], int, int]]:
    """
    Return plans:
        (central_vertex, removed_edges, distinct_cycle_coverage, vertex_load)

    Only forbidden-cycle edges are removed. A donor endpoint must be degree>=4.
    Removal sets are ranked by how many distinct scorer-proved forbidden cycles
    they hit, not merely by edge count.
    """
    if arity not in (2, 3):
        raise ValueError("guided surgery arity must be 2 or 3")

    degrees = vertex_degrees(graph)
    edge_masks = dict(metrics.edge_cycle_masks)
    vertex_masks = dict(metrics.vertex_cycle_masks)

    hot_vertices = sorted(
        vertex_masks,
        key=lambda vertex: (
            -vertex_masks[vertex].bit_count(),
            vertex,
        ),
    )[:hot_vertices_limit]

    plans: list[tuple[int, tuple[Edge, ...], int, int]] = []

    for center in hot_vertices:
        incident: list[Edge] = []
        for edge, cycle_mask in edge_masks.items():
            if cycle_mask == 0 or center not in edge:
                continue
            donor = _other_endpoint(edge, center)
            if degrees[donor] < 4:
                continue
            incident.append(edge)

        if len(incident) < arity:
            continue

        for removed in combinations(sorted(incident), arity):
            coverage_mask = 0
            for edge in removed:
                coverage_mask |= edge_masks[edge]
            coverage = coverage_mask.bit_count()

            # The point of a multi-edge surgery is to attack multiple distinct
            # forbidden cycles atomically. Reject degenerate plans that spend
            # several removals on only one cycle.
            if coverage < arity:
                continue

            plans.append(
                (
                    center,
                    tuple(removed),
                    coverage,
                    vertex_masks.get(center, 0).bit_count(),
                )
            )

    plans.sort(
        key=lambda item: (
            -item[2],  # distinct forbidden cycles covered
            -item[3],  # hot-vertex cycle load
            item[0],
            item[1],
        )
    )
    return plans


def guided_vertex_rewire_mutations(
    graph: Graph,
    metrics: StructuralMetrics,
    *,
    arity: int,
    limit: int,
    hot_vertices_limit: int,
    seed: int,
) -> list[Mutation]:
    family = f"vertex_rewire{arity}"
    edge_set = set(graph.edges)
    adjacency: list[set[int]] = [set() for _ in range(graph.order)]
    for u, v in graph.edges:
        adjacency[u].add(v)
        adjacency[v].add(u)

    plans = _guided_removal_plans(
        graph,
        metrics,
        arity=arity,
        hot_vertices_limit=hot_vertices_limit,
    )
    if not plans:
        return []

    # Build a deterministic, diverse target stream for each high-value removal
    # plan, then round-robin plans so one hot vertex cannot monopolize the cap.
    per_plan_targets: list[
        tuple[int, tuple[Edge, ...], int, int, list[tuple[int, ...]]]
    ] = []

    for plan_index, (center, removed, coverage, vertex_load) in enumerate(plans):
        donors = {_other_endpoint(edge, center) for edge in removed}
        targets = [
            vertex
            for vertex in range(graph.order)
            if vertex != center
            and vertex not in adjacency[center]
            and vertex not in donors
        ]
        if len(targets) < arity:
            continue

        target_sets = list(combinations(targets, arity))
        rng = random.Random(
            seed
            ^ int(graph.graph_hash[:16], 16)
            ^ ((center + 1) << 17)
            ^ ((arity + 1) << 29)
            ^ plan_index
        )
        rng.shuffle(target_sets)

        per_plan_targets.append(
            (center, removed, coverage, vertex_load, target_sets)
        )

    mutations: list[Mutation] = []
    target_index = 0
    active = per_plan_targets

    while active and len(mutations) < limit:
        next_active = []
        for center, removed, coverage, vertex_load, target_sets in active:
            if target_index >= len(target_sets):
                continue

            targets = target_sets[target_index]
            added = tuple(
                sorted(norm_edge(center, target) for target in targets)
            )

            # All target edges are non-edges in the parent by construction.
            if any(edge in edge_set for edge in added):
                continue

            mutations.append(
                Mutation(
                    family=family,
                    removed_edges=removed,
                    added_edges=added,
                    variant=(
                        f"v={center}/cover={coverage}/vload={vertex_load}"
                    ),
                )
            )
            if len(mutations) >= limit:
                break

            if target_index + 1 < len(target_sets):
                next_active.append(
                    (center, removed, coverage, vertex_load, target_sets)
                )

        target_index += 1
        active = next_active

    return mutations



def _center_oriented_edge(edge: Edge, center: int) -> tuple[int, int]:
    """Return (center, other_endpoint) for an edge incident to center."""
    u, v = edge
    if u == center:
        return center, v
    if v == center:
        return center, u
    raise ValueError(f"edge {edge} is not incident to center={center}")


def guided_coupled_switch2_mutations(
    graph: Graph,
    metrics: StructuralMetrics,
    *,
    limit: int,
    hot_vertices_limit: int,
    seed: int,
) -> list[Mutation]:
    """
    Generate a broad deterministic sample of atomic coupled 2-switches.

    The exact-C4 gate is intentionally NOT computed here; doing that in the
    parent process would recreate the previous single-process bottleneck.

    Plan ordering mixes:
      - exploitation: high distinct forbidden-cycle removal coverage;
      - exploration: seeded sampling across all legal partner-edge plans.

    The expensive scientific decision happens later in score processes:
      exact C4 -> admissible prune -> full HEG scorer.
    """
    if limit <= 0:
        return []

    edge_set = set(graph.edges)
    edge_masks = dict(metrics.edge_cycle_masks)
    vertex_masks = dict(metrics.vertex_cycle_masks)

    hot_vertices = sorted(
        vertex_masks,
        key=lambda vertex: (
            -vertex_masks[vertex].bit_count(),
            vertex,
        ),
    )[:hot_vertices_limit]

    # Each plan:
    #   (coverage_key, random_key, center, hot1, hot2, partner1, partner2)
    plans: list[
        tuple[
            tuple[int, int, int, int],
            int,
            int,
            Edge,
            Edge,
            Edge,
            Edge,
        ]
    ] = []

    graph_seed = int(graph.graph_hash[:16], 16)
    rng = random.Random(seed ^ graph_seed)

    for center in hot_vertices:
        hot_edges = sorted(
            edge
            for edge, mask in edge_masks.items()
            if mask and center in edge
        )
        if len(hot_edges) < 2:
            continue

        center_load = vertex_masks.get(center, 0).bit_count()

        for hot_1, hot_2 in combinations(hot_edges, 2):
            hot_mask = edge_masks.get(hot_1, 0) | edge_masks.get(hot_2, 0)
            hot_coverage = hot_mask.bit_count()
            if hot_coverage < 2:
                continue

            _, donor_1 = _center_oriented_edge(hot_1, center)
            _, donor_2 = _center_oriented_edge(hot_2, center)
            blocked = {center, donor_1, donor_2}

            partner_edges = [
                edge
                for edge in graph.edges
                if edge not in (hot_1, hot_2)
                and blocked.isdisjoint(edge)
            ]

            for partner_1, partner_2 in combinations(partner_edges, 2):
                if not set(partner_1).isdisjoint(partner_2):
                    continue

                partner_mask = (
                    edge_masks.get(partner_1, 0)
                    | edge_masks.get(partner_2, 0)
                )
                removal_mask = hot_mask | partner_mask

                coverage_key = (
                    -removal_mask.bit_count(),
                    -hot_coverage,
                    -center_load,
                    -partner_mask.bit_count(),
                )
                plans.append(
                    (
                        coverage_key,
                        rng.randrange(1 << 62),
                        center,
                        hot_1,
                        hot_2,
                        partner_1,
                        partner_2,
                    )
                )

    if not plans:
        return []

    coverage_order = sorted(
        range(len(plans)),
        key=lambda index: (plans[index][0], plans[index][1]),
    )
    exploration_order = sorted(
        range(len(plans)),
        key=lambda index: plans[index][1],
    )

    # Interleave one high-coverage plan with one whole-space exploration plan.
    # Duplicates are removed. This prevents the proposal cap from being spent
    # entirely on one removal-coverage stratum.
    plan_order: list[int] = []
    used_plan_indices: set[int] = set()

    max_len = max(len(coverage_order), len(exploration_order))
    for index in range(max_len):
        if index < len(coverage_order):
            plan_index = coverage_order[index]
            if plan_index not in used_plan_indices:
                plan_order.append(plan_index)
                used_plan_indices.add(plan_index)

        if index < len(exploration_order):
            plan_index = exploration_order[index]
            if plan_index not in used_plan_indices:
                plan_order.append(plan_index)
                used_plan_indices.add(plan_index)

    mutations: list[Mutation] = []
    seen_signatures: set[
        tuple[tuple[Edge, ...], tuple[Edge, ...]]
    ] = set()

    for plan_index in plan_order:
        if len(mutations) >= limit:
            break

        (
            coverage_key,
            _random_key,
            center,
            hot_1,
            hot_2,
            partner_1,
            partner_2,
        ) = plans[plan_index]

        _, donor_1 = _center_oriented_edge(hot_1, center)
        _, donor_2 = _center_oriented_edge(hot_2, center)

        assignments = (
            (partner_1, partner_2, "ab"),
            (partner_2, partner_1, "ba"),
        )

        removed = tuple(sorted((hot_1, hot_2, partner_1, partner_2)))
        removed_set = set(removed)
        remaining = edge_set - removed_set

        total_coverage = -coverage_key[0]
        hot_coverage = -coverage_key[1]
        partner_coverage = -coverage_key[3]

        # Seeded variant ordering adds diversity when the cap stops part-way
        # through a plan.
        variants: list[
            tuple[str, int, int, tuple[Edge, ...]]
        ] = []

        for first_partner, second_partner, assignment_name in assignments:
            first_options = (
                (
                    norm_edge(center, first_partner[0]),
                    norm_edge(donor_1, first_partner[1]),
                ),
                (
                    norm_edge(center, first_partner[1]),
                    norm_edge(donor_1, first_partner[0]),
                ),
            )
            second_options = (
                (
                    norm_edge(center, second_partner[0]),
                    norm_edge(donor_2, second_partner[1]),
                ),
                (
                    norm_edge(center, second_partner[1]),
                    norm_edge(donor_2, second_partner[0]),
                ),
            )

            for first_variant, first_added in enumerate(first_options):
                for second_variant, second_added in enumerate(second_options):
                    added = tuple(sorted(first_added + second_added))

                    if len(set(added)) != 4:
                        continue
                    if any(u == v for u, v in added):
                        continue
                    if any(edge in removed_set for edge in added):
                        continue
                    if any(edge in remaining for edge in added):
                        continue

                    variants.append(
                        (
                            assignment_name,
                            first_variant,
                            second_variant,
                            added,
                        )
                    )

        variant_rng = random.Random(
            seed
            ^ graph_seed
            ^ plan_index
            ^ (center << 19)
        )
        variant_rng.shuffle(variants)

        for (
            assignment_name,
            first_variant,
            second_variant,
            added,
        ) in variants:
            signature = (removed, added)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            mutations.append(
                Mutation(
                    family="coupled_switch2",
                    removed_edges=removed,
                    added_edges=added,
                    variant=(
                        f"v={center}"
                        f"/cover={total_coverage}"
                        f"/hotcover={hot_coverage}"
                        f"/partnercover={partner_coverage}"
                        f"/assign={assignment_name}"
                        f"/orient={first_variant}{second_variant}"
                    ),
                )
            )

            if len(mutations) >= limit:
                return mutations

    return mutations



def minimum_edges_for_delta3(order: int) -> int:
    """Information-theoretic minimum edge count under minimum degree >=3."""
    return (3 * order + 1) // 2


def graph_edge_excess(graph: Graph) -> int:
    return len(graph.edges) - minimum_edges_for_delta3(graph.order)


def _slack_target_vertices(
    graph: Graph,
    *,
    fixed: int,
    donor: int,
    edge_set: set[Edge],
) -> list[int]:
    return [
        vertex
        for vertex in range(graph.order)
        if vertex != fixed
        and vertex != donor
        and norm_edge(fixed, vertex) not in edge_set
    ]


def guided_slack_rewire1_mutations(
    graph: Graph,
    metrics: StructuralMetrics,
    *,
    limit: int,
    max_edge_excess: int,
    seed: int,
) -> list[Mutation]:
    """
    Atomic +1-edge relocation from a forbidden-cycle edge.

    Parent edge fixed-donor is removed. A new donor-support edge protects the
    donor degree, and fixed-target relocates the removed incidence. On a cubic
    parent all vertices therefore remain degree >=3 and two units of degree
    slack are created without an invalid intermediate state.
    """
    if limit <= 0 or graph_edge_excess(graph) >= max_edge_excess:
        return []

    edge_set = set(graph.edges)
    edge_masks = dict(metrics.edge_cycle_masks)
    vertex_masks = dict(metrics.vertex_cycle_masks)
    if not edge_masks:
        return []

    plans: list[tuple[tuple[int, int, int], int, int, int, Edge]] = []
    graph_seed = int(graph.graph_hash[:16], 16)
    rng = random.Random(seed ^ graph_seed ^ 0x51A6C1)

    for edge, mask in sorted(edge_masks.items()):
        if not mask:
            continue
        coverage = mask.bit_count()
        for fixed, donor in (edge, (edge[1], edge[0])):
            load = vertex_masks.get(donor, 0).bit_count()
            plans.append(
                ((-coverage, -load, rng.randrange(1 << 30)), fixed, donor, coverage, edge)
            )

    plans.sort(key=lambda item: item[0])
    streams: list[tuple[int, int, int, Edge, list[tuple[int, int]]]] = []

    for plan_index, (_key, fixed, donor, coverage, removed) in enumerate(plans):
        supports = [
            vertex
            for vertex in range(graph.order)
            if vertex not in (fixed, donor)
            and norm_edge(donor, vertex) not in edge_set
        ]
        targets = _slack_target_vertices(
            graph,
            fixed=fixed,
            donor=donor,
            edge_set=edge_set,
        )
        if not supports or not targets:
            continue

        pairs = [(support, target) for support in supports for target in targets]
        local_rng = random.Random(seed ^ graph_seed ^ (plan_index << 13) ^ donor)
        local_rng.shuffle(pairs)
        streams.append((fixed, donor, coverage, removed, pairs))

    mutations: list[Mutation] = []
    index = 0
    active = streams
    seen: set[tuple[tuple[Edge, ...], tuple[Edge, ...]]] = set()

    while active and len(mutations) < limit:
        next_active = []
        for fixed, donor, coverage, removed, pairs in active:
            if index >= len(pairs):
                continue
            support, target = pairs[index]
            added = tuple(
                sorted(
                    (
                        norm_edge(donor, support),
                        norm_edge(fixed, target),
                    )
                )
            )
            removed_tuple = (removed,)
            removed_set = {removed}
            remaining = edge_set - removed_set

            if len(set(added)) != 2:
                continue
            if any(edge in removed_set for edge in added):
                continue
            if any(edge in remaining for edge in added):
                continue

            signature = (removed_tuple, added)
            if signature not in seen:
                seen.add(signature)
                mutations.append(
                    Mutation(
                        family="slack_rewire1",
                        removed_edges=removed_tuple,
                        added_edges=added,
                        variant=(
                            f"fixed={fixed}/donor={donor}/support={support}"
                            f"/target={target}/cover={coverage}"
                        ),
                    )
                )
                if len(mutations) >= limit:
                    break

            if index + 1 < len(pairs):
                next_active.append((fixed, donor, coverage, removed, pairs))

        index += 1
        active = next_active

    return mutations


def guided_slack_rewire2_mutations(
    graph: Graph,
    metrics: StructuralMetrics,
    *,
    limit: int,
    max_edge_excess: int,
    seed: int,
) -> list[Mutation]:
    """
    Atomic +1-edge two-cycle surgery.

    Select two forbidden-cycle edges from at least two distinct covered cycles,
    orient them with distinct donors d1,d2, add support edge d1-d2, remove both
    selected edges, then reconnect the two fixed endpoints to new targets.

    Net edge change: -2 +3 = +1. On a cubic parent, each donor loses one edge and
    gains the support edge, so no degree-2 intermediate/final vertex is needed.
    """
    if limit <= 0 or graph_edge_excess(graph) >= max_edge_excess:
        return []

    edge_set = set(graph.edges)
    edge_masks = dict(metrics.edge_cycle_masks)
    vertex_masks = dict(metrics.vertex_cycle_masks)
    if len(edge_masks) < 2:
        return []

    oriented: list[tuple[Edge, int, int, int]] = []
    for edge, mask in sorted(edge_masks.items()):
        if not mask:
            continue
        for fixed, donor in (edge, (edge[1], edge[0])):
            oriented.append((edge, fixed, donor, mask))

    graph_seed = int(graph.graph_hash[:16], 16)
    plan_rng = random.Random(seed ^ graph_seed ^ 0x51A6C2)

    plans: list[
        tuple[tuple[int, int, int, int], int, Edge, int, int, Edge, int, int]
    ] = []

    for idx, first in enumerate(oriented):
        edge1, fixed1, donor1, mask1 = first
        for edge2, fixed2, donor2, mask2 in oriented[idx + 1 :]:
            if edge1 == edge2 or donor1 == donor2:
                continue
            coverage = (mask1 | mask2).bit_count()
            if coverage < 2:
                continue

            support = norm_edge(donor1, donor2)
            if support in edge_set:
                continue

            donor_load = (
                vertex_masks.get(donor1, 0).bit_count()
                + vertex_masks.get(donor2, 0).bit_count()
            )
            plans.append(
                (
                    (-coverage, -donor_load, -mask1.bit_count() - mask2.bit_count(), plan_rng.randrange(1 << 30)),
                    coverage,
                    edge1,
                    fixed1,
                    donor1,
                    edge2,
                    fixed2,
                    donor2,
                )
            )

    if not plans:
        return []
    plans.sort(key=lambda item: item[0])

    streams: list[
        tuple[int, Edge, int, int, Edge, int, int, list[tuple[int, int]]]
    ] = []

    # Limit plan materialization as well as final proposal count. The first plans
    # are coverage-first with a seeded tie-break, so this stays deterministic.
    plan_cap = max(64, min(len(plans), limit // 8 if limit >= 8 else len(plans)))
    for plan_index, plan in enumerate(plans[:plan_cap]):
        (_key, coverage, edge1, fixed1, donor1, edge2, fixed2, donor2) = plan
        targets1 = _slack_target_vertices(
            graph,
            fixed=fixed1,
            donor=donor1,
            edge_set=edge_set,
        )
        targets2 = _slack_target_vertices(
            graph,
            fixed=fixed2,
            donor=donor2,
            edge_set=edge_set,
        )
        if not targets1 or not targets2:
            continue

        target_pairs = [(a, b) for a in targets1 for b in targets2]
        local_rng = random.Random(
            seed ^ graph_seed ^ (plan_index << 17) ^ (donor1 << 8) ^ donor2
        )
        local_rng.shuffle(target_pairs)
        streams.append(
            (
                coverage,
                edge1,
                fixed1,
                donor1,
                edge2,
                fixed2,
                donor2,
                target_pairs,
            )
        )

    mutations: list[Mutation] = []
    index = 0
    active = streams
    seen: set[tuple[tuple[Edge, ...], tuple[Edge, ...]]] = set()

    while active and len(mutations) < limit:
        next_active = []
        for (
            coverage,
            edge1,
            fixed1,
            donor1,
            edge2,
            fixed2,
            donor2,
            target_pairs,
        ) in active:
            if index >= len(target_pairs):
                continue

            target1, target2 = target_pairs[index]
            removed = tuple(sorted((edge1, edge2)))
            removed_set = set(removed)
            remaining = edge_set - removed_set
            added = tuple(
                sorted(
                    (
                        norm_edge(donor1, donor2),
                        norm_edge(fixed1, target1),
                        norm_edge(fixed2, target2),
                    )
                )
            )

            if len(set(added)) != 3:
                continue
            if any(edge in removed_set for edge in added):
                continue
            if any(edge in remaining for edge in added):
                continue

            signature = (removed, added)
            if signature not in seen:
                seen.add(signature)
                mutations.append(
                    Mutation(
                        family="slack_rewire2",
                        removed_edges=removed,
                        added_edges=added,
                        variant=(
                            f"d1={donor1}/d2={donor2}/t1={target1}/t2={target2}"
                            f"/cover={coverage}"
                        ),
                    )
                )
                if len(mutations) >= limit:
                    break

            if index + 1 < len(target_pairs):
                next_active.append(
                    (
                        coverage,
                        edge1,
                        fixed1,
                        donor1,
                        edge2,
                        fixed2,
                        donor2,
                        target_pairs,
                    )
                )

        index += 1
        active = next_active

    return mutations


def excess_edge_remove_mutations(graph: Graph) -> list[Mutation]:
    """Return -1-edge moves that cannot violate minimum degree locally."""
    if graph_edge_excess(graph) <= 0:
        return []
    degrees = vertex_degrees(graph)
    return [
        Mutation(
            family="excess_edge_remove",
            removed_edges=(edge,),
            added_edges=(),
            variant=f"u={edge[0]}/v={edge[1]}",
        )
        for edge in graph.edges
        if degrees[edge[0]] >= 4 and degrees[edge[1]] >= 4
    ]


def mutations_for_family(
    graph: Graph,
    family: str,
    *,
    metrics: StructuralMetrics | None,
    guided_limit: int,
    coupled_limit: int,
    slack_limit: int,
    max_edge_excess: int,
    hot_vertices_limit: int,
    seed: int,
) -> list[Mutation]:
    if family == "slack_rewire2":
        if metrics is None:
            return []
        return guided_slack_rewire2_mutations(
            graph,
            metrics,
            limit=slack_limit,
            max_edge_excess=max_edge_excess,
            seed=seed,
        )
    if family == "slack_rewire1":
        if metrics is None:
            return []
        return guided_slack_rewire1_mutations(
            graph,
            metrics,
            limit=slack_limit,
            max_edge_excess=max_edge_excess,
            seed=seed,
        )
    if family == "excess_edge_remove":
        return excess_edge_remove_mutations(graph)
    if family == "two_switch":
        return two_switch_mutations(graph)
    if family == "endpoint_relocate":
        return endpoint_relocate_mutations(graph)
    if family == "vertex_rewire2":
        if metrics is None:
            return []
        return guided_vertex_rewire_mutations(
            graph,
            metrics,
            arity=2,
            limit=guided_limit,
            hot_vertices_limit=hot_vertices_limit,
            seed=seed,
        )
    if family == "vertex_rewire3":
        if metrics is None:
            return []
        return guided_vertex_rewire_mutations(
            graph,
            metrics,
            arity=3,
            limit=guided_limit,
            hot_vertices_limit=hot_vertices_limit,
            seed=seed,
        )
    if family == "coupled_switch2":
        if metrics is None:
            return []
        return guided_coupled_switch2_mutations(
            graph,
            metrics,
            limit=coupled_limit,
            hot_vertices_limit=hot_vertices_limit,
            seed=seed,
        )
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
    return candidate


def _fast_fixed_order_candidate_edges(
    *,
    order: int,
    parent_edges: tuple[Edge, ...],
    mutation: Mutation,
    max_edge_excess: int,
) -> tuple[tuple[Edge, ...], tuple[int, ...]] | None:
    """
    Fast fixed-order invariant check used in score-process workers.

    Mutation generators already guarantee no self-loop and intended edge-count
    preservation. We recheck those properties, minimum degree, and connectivity
    without calling the much more general scientific Graph validator.
    """
    edge_set = set(parent_edges)

    removed = tuple(norm_edge(*edge) for edge in mutation.removed_edges)
    added = tuple(norm_edge(*edge) for edge in mutation.added_edges)

    if len(set(removed)) != len(removed):
        return None
    if len(set(added)) != len(added):
        return None
    if any(edge not in edge_set for edge in removed):
        return None

    for edge in removed:
        edge_set.remove(edge)

    if any(edge in edge_set for edge in added):
        return None
    for edge in added:
        edge_set.add(edge)

    min_edges = minimum_edges_for_delta3(order)
    if not (min_edges <= len(edge_set) <= min_edges + max_edge_excess):
        return None

    degrees = [0] * order
    adjacency_masks = [0] * order

    for u, v in edge_set:
        if u == v or not (0 <= u < order and 0 <= v < order):
            return None
        degrees[u] += 1
        degrees[v] += 1
        adjacency_masks[u] |= 1 << v
        adjacency_masks[v] |= 1 << u

    if min(degrees, default=0) < 3:
        return None

    # Bitset BFS/closure, n<=128.
    seen = 1
    frontier = 1
    while frontier:
        neighbors = 0
        scan = frontier
        while scan:
            low = scan & -scan
            vertex = low.bit_length() - 1
            neighbors |= adjacency_masks[vertex]
            scan ^= low
        new_frontier = neighbors & ~seen
        seen |= new_frontier
        frontier = new_frontier

    if seen.bit_count() != order:
        return None

    return tuple(sorted(edge_set)), tuple(adjacency_masks)


def exact_c4_from_adjacency_masks(
    adjacency_masks: tuple[int, ...],
    *,
    ceiling: int | None = None,
) -> int:
    """
    Exact C4 count for a simple undirected graph.

        C4 = 1/2 * sum_{u<v} choose(common_neighbors(u,v), 2)

    If ceiling is supplied, return ceiling+1 as soon as the accumulated doubled
    count proves C4 > ceiling. This preserves exact accept/reject semantics while
    avoiding unnecessary work on obviously bad candidates.
    """
    doubled_c4 = 0
    reject_threshold = None if ceiling is None else 2 * ceiling

    order = len(adjacency_masks)
    for u in range(order):
        mask_u = adjacency_masks[u]
        for v in range(u + 1, order):
            common = (mask_u & adjacency_masks[v]).bit_count()
            if common >= 2:
                doubled_c4 += common * (common - 1) // 2
                if (
                    reject_threshold is not None
                    and doubled_c4 > reject_threshold
                ):
                    return ceiling + 1

    if doubled_c4 % 2:
        raise RuntimeError(
            f"C4 formula produced odd doubled count {doubled_c4}"
        )
    return doubled_c4 // 2


@dataclass(frozen=True, slots=True)
class CandidateScorePayload:
    parent_hash: str
    mutation: Mutation
    order: int
    edges: tuple[Edge, ...]
    graph_hash: str
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


@dataclass(frozen=True, slots=True)
class ScoreBatchPayload:
    family: str
    results: tuple[CandidateScorePayload, ...]
    invalid: int
    c4_tested: int
    c4_pruned: int
    c4_passed: int
    min_c4_seen: int | None


def score_candidate_batch_payload(
    parent_hash: str,
    order: int,
    parent_edges: tuple[Edge, ...],
    mutations: tuple[Mutation, ...],
    witness_cap: int,
    node_budget: int,
    c4_ceiling: int,
    max_edge_excess: int,
) -> ScoreBatchPayload:
    results: list[CandidateScorePayload] = []
    invalid = 0
    c4_tested = 0
    c4_pruned = 0
    c4_passed = 0
    min_c4_seen: int | None = None
    batch_seen: set[str] = set()

    family = mutations[0].family if mutations else "-"

    # Survivors are sorted by exact final C4 before the expensive scorer.
    survivors: list[
        tuple[int, Mutation, tuple[Edge, ...]]
    ] = []

    for mutation in mutations:
        prepared = _fast_fixed_order_candidate_edges(
            order=order,
            parent_edges=parent_edges,
            mutation=mutation,
            max_edge_excess=max_edge_excess,
        )
        if prepared is None:
            invalid += 1
            continue

        candidate_edges, adjacency_masks = prepared
        c4_tested += 1

        exact_c4 = exact_c4_from_adjacency_masks(
            adjacency_masks,
            ceiling=c4_ceiling,
        )

        if exact_c4 > c4_ceiling:
            c4_pruned += 1
            continue

        if min_c4_seen is None or exact_c4 < min_c4_seen:
            min_c4_seen = exact_c4

        c4_passed += 1
        survivors.append((exact_c4, mutation, candidate_edges))

    survivors.sort(
        key=lambda item: (
            item[0],
            item[1].variant,
            item[1].removed_edges,
            item[1].added_edges,
        )
    )

    for exact_c4, mutation, candidate_edges in survivors:
        graph = Graph.from_edges(order, candidate_edges)
        graph_hash = graph.graph_hash

        if graph_hash in batch_seen:
            continue
        batch_seen.add(graph_hash)

        score = score_graph(
            graph,
            witness_cap=witness_cap,
            node_budget=node_budget,
        )

        scorer_c4 = next(
            (
                component
                for component in score.components
                if component.length == 4
            ),
            None,
        )
        if (
            scorer_c4 is not None
            and scorer_c4.exact
            and scorer_c4.observed != exact_c4
        ):
            raise RuntimeError(
                "exact C4 prefilter/scorer mismatch: "
                f"fast={exact_c4}, scorer={scorer_c4.observed}, "
                f"graph={graph_hash}"
            )

        results.append(
            CandidateScorePayload(
                parent_hash=parent_hash,
                mutation=mutation,
                order=order,
                edges=candidate_edges,
                graph_hash=graph_hash,
                components=score.components,
                elapsed_seconds=score.elapsed_seconds,
            )
        )

    return ScoreBatchPayload(
        family=family,
        results=tuple(results),
        invalid=invalid,
        c4_tested=c4_tested,
        c4_pruned=c4_pruned,
        c4_passed=c4_passed,
        min_c4_seen=min_c4_seen,
    )


def score_graph_once(
    graph: Graph,
    *,
    witness_cap: int,
    node_budget: int,
) -> GraphScore:
    """
    One parent-process score used only for the root before score processes start.
    """
    worker = ScoreWorker()
    worker.__enter__()
    previous = getattr(_tls, "score_worker", None)
    _tls.score_worker = worker
    try:
        return score_graph(
            graph,
            witness_cap=witness_cap,
            node_budget=node_budget,
        )
    finally:
        _tls.score_worker = previous
        worker.close()



@dataclass(slots=True)
class CandidateBucket:
    parent: BeamState
    family: str
    mutations: list[Mutation]


def build_buckets(
    beam: list[BeamState],
    *,
    families: tuple[str, ...],
    metrics_by_hash: dict[str, StructuralMetrics],
    depth: int,
    seed: int,
    limit_per_family_parent: int,
    guided_limit_per_parent: int,
    coupled_scan_per_parent: int,
    slack_limit_per_parent: int,
    max_edge_excess: int,
    hot_vertices_per_parent: int,
) -> list[CandidateBucket]:
    buckets: list[CandidateBucket] = []

    for parent in beam:
        parent_seed = int(parent.score.graph.graph_hash[:16], 16)
        parent_metrics = metrics_by_hash.get(parent.score.graph.graph_hash)

        for family_index, family in enumerate(families):
            family_seed = (
                seed
                ^ (depth << 24)
                ^ parent_seed
                ^ ((family_index + 1) * 0x9E3779B97F4A7C15)
            )

            mutations = mutations_for_family(
                parent.score.graph,
                family,
                metrics=parent_metrics,
                guided_limit=guided_limit_per_parent,
                coupled_limit=coupled_scan_per_parent,
                slack_limit=slack_limit_per_parent,
                max_edge_excess=max_edge_excess,
                hot_vertices_limit=hot_vertices_per_parent,
                seed=family_seed,
            )

            # Broad families keep their original deterministic randomized
            # ordering. Guided families already produce coverage/diversity-aware
            # order and should retain it.
            if family in ("two_switch", "endpoint_relocate", "excess_edge_remove"):
                rng = random.Random(family_seed)
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


def interleaved_candidate_batches(
    buckets: list[CandidateBucket],
    *,
    families: tuple[str, ...],
    batch_size: int,
) -> Iterator[tuple[BeamState, tuple[Mutation, ...]]]:
    """
    Round-robin batches by family and parent.

    Each batch uses one parent graph, so parent edges cross the process boundary
    once for many candidate scores instead of once per candidate.
    """
    by_family: dict[str, list[CandidateBucket]] = {
        family: [] for family in families
    }
    for bucket in buckets:
        by_family[bucket.family].append(bucket)

    chunks_by_bucket: dict[int, list[tuple[Mutation, ...]]] = {}
    maximum_chunks = 0

    for bucket in buckets:
        chunks = [
            tuple(bucket.mutations[index : index + batch_size])
            for index in range(0, len(bucket.mutations), batch_size)
        ]
        chunks_by_bucket[id(bucket)] = chunks
        maximum_chunks = max(maximum_chunks, len(chunks))

    for chunk_index in range(maximum_chunks):
        for family in families:
            for bucket in by_family[family]:
                chunks = chunks_by_bucket[id(bucket)]
                if chunk_index < len(chunks):
                    yield bucket.parent, chunks[chunk_index]



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
            edge_cycle_masks=(),
            vertex_cycle_masks=(),
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
        edge_cycle_masks=tuple(sorted(edge_masks.items())),
        vertex_cycle_masks=tuple(sorted(vertex_masks.items())),
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
        tuple[tuple[int, ...], int, str, str],
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
            (component_profile(state.score), len(state.score.graph.edges), family, parent_hash)
        ].append(state)

    for bucket in buckets.values():
        bucket.sort(key=lambda state: state.score.graph.graph_hash)

    keys = sorted(buckets)
    chosen: list[BeamState] = []
    index = 0

    while keys and len(chosen) < count:
        next_keys: list[tuple[tuple[int, ...], int, str, str]] = []
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
            return (component_profile(state.score), len(state.score.graph.edges))
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
                len(state.score.graph.edges),
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
        "schema_version": "graphoratory.heg_fixed_order_slack_escape.v1",
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
                "edge_count": len(item.score.graph.edges),
                "edge_excess": graph_edge_excess(item.score.graph),
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
        "edge_cycle_masks": [
            [list(edge), mask] for edge, mask in metrics.edge_cycle_masks
        ],
        "vertex_cycle_masks": [
            [vertex, mask] for vertex, mask in metrics.vertex_cycle_masks
        ],
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
    start_excess = graph_edge_excess(start_graph)
    if start_excess < 0:
        raise RuntimeError(
            f"start graph has fewer than ceil(3n/2) edges: excess={start_excess}"
        )
    if start_excess > args.max_edge_excess:
        raise RuntimeError(
            f"start graph edge excess {start_excess} exceeds "
            f"--max-edge-excess={args.max_edge_excess}"
        )

    console.print(
        f"[bold]HEG fixed-order slack escape + exact C4 gate[/bold] "
        f"order={start_graph.order} beam={args.beam_width} "
        f"lanes={args.main_lanes}+{args.structural_lanes}+{args.escape_lanes} "
        f"escape_height={args.escape_height} structural_pool={args.structural_pool} "
        f"(escape_reserved={args.escape_structural_pool}) "
        f"structural_workers={args.structural_workers} "
        f"structural_C<={args.max_structural_cycle_length} "
        f"max_depth={args.max_depth} score_processes={args.workers} "
        f"batch={args.score_batch_size}x{args.inflight_per_worker} "
        f"guided_limit={args.guided_limit_per_parent} "
        f"coupled_scan={args.coupled_scan_per_parent} "
        f"slack_limit={args.slack_limit_per_parent} "
        f"max_edge_excess={args.max_edge_excess} "
        f"hot_vertices={args.hot_vertices_per_parent} "
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
    max_inflight_batches = args.workers * args.inflight_per_worker

    spawn_context = multiprocessing.get_context("spawn")
    initial_score = score_graph_once(
        start_graph,
        witness_cap=args.witness_cap,
        node_budget=args.node_budget,
    )

    with ProcessPoolExecutor(
        max_workers=args.structural_workers,
        mp_context=spawn_context,
    ) as structural_executor, ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=spawn_context,
        initializer=_thread_worker_init,
    ) as score_executor:

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
            f"m={len(start_graph.edges)} excess={graph_edge_excess(start_graph)} "
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
                metrics_by_hash=structural_cache,
                depth=depth,
                seed=args.seed,
                limit_per_family_parent=args.max_mutations_per_family_parent,
                guided_limit_per_parent=args.guided_limit_per_parent,
                coupled_scan_per_parent=args.coupled_scan_per_parent,
                slack_limit_per_parent=args.slack_limit_per_parent,
                max_edge_excess=args.max_edge_excess,
                hot_vertices_per_parent=args.hot_vertices_per_parent,
            )
            proposal_space = sum(len(bucket.mutations) for bucket in buckets)
            batch_stream = interleaved_candidate_batches(
                buckets,
                families=args.families,
                batch_size=args.score_batch_size,
            )

            beam_by_hash = {
                state.score.graph.graph_hash: state for state in beam
            }

            incumbent_total_before_depth = best.score.total
            if incumbent_total_before_depth is None:
                raise RuntimeError("global incumbent is unexpectedly inexact")
            c4_ceiling = incumbent_total_before_depth + args.escape_height

            inflight_batches: dict[Future[ScoreBatchPayload], int] = {}
            raw_results: list[CandidateScorePayload] = []

            submitted = 0
            duplicate_or_visited = 0
            invalid = 0
            c4_tested_total = 0
            c4_pruned_total = 0
            c4_passed_total = 0
            c4_stats_by_family: dict[str, dict[str, int | None]] = {
                family: {
                    "tested": 0,
                    "pruned": 0,
                    "passed": 0,
                    "min": None,
                }
                for family in args.families
            }
            exhausted_stream = False
            batch_serial = 0

            def submit_batches_until_full() -> None:
                nonlocal exhausted_stream
                nonlocal submitted
                nonlocal batch_serial

                while (
                    not exhausted_stream
                    and len(inflight_batches) < max_inflight_batches
                    and time.perf_counter() < step_deadline
                ):
                    try:
                        parent, mutation_batch = next(batch_stream)
                    except StopIteration:
                        exhausted_stream = True
                        break

                    future = score_executor.submit(
                        score_candidate_batch_payload,
                        parent.score.graph.graph_hash,
                        parent.score.graph.order,
                        parent.score.graph.edges,
                        mutation_batch,
                        args.witness_cap,
                        args.node_budget,
                        c4_ceiling,
                        args.max_edge_excess,
                    )
                    inflight_batches[future] = batch_serial
                    batch_serial += 1
                    submitted += len(mutation_batch)

            def absorb_batch_payload(
                batch_payload: ScoreBatchPayload,
            ) -> None:
                nonlocal invalid
                nonlocal c4_tested_total
                nonlocal c4_pruned_total
                nonlocal c4_passed_total

                invalid += batch_payload.invalid
                c4_tested_total += batch_payload.c4_tested
                c4_pruned_total += batch_payload.c4_pruned
                c4_passed_total += batch_payload.c4_passed
                raw_results.extend(batch_payload.results)

                stat = c4_stats_by_family.setdefault(
                    batch_payload.family,
                    {
                        "tested": 0,
                        "pruned": 0,
                        "passed": 0,
                        "min": None,
                    },
                )
                stat["tested"] = int(stat["tested"] or 0) + batch_payload.c4_tested
                stat["pruned"] = int(stat["pruned"] or 0) + batch_payload.c4_pruned
                stat["passed"] = int(stat["passed"] or 0) + batch_payload.c4_passed

                if batch_payload.min_c4_seen is not None:
                    prior_min = stat["min"]
                    if prior_min is None or batch_payload.min_c4_seen < prior_min:
                        stat["min"] = batch_payload.min_c4_seen

            submit_batches_until_full()

            while inflight_batches:
                timeout = max(0.0, step_deadline - time.perf_counter())
                if timeout == 0.0:
                    break

                done, _ = wait(
                    tuple(inflight_batches),
                    timeout=timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    break

                for future in done:
                    inflight_batches.pop(future)
                    batch_payload = future.result()
                    absorb_batch_payload(batch_payload)

                submit_batches_until_full()

            running_tail: list[Future[ScoreBatchPayload]] = []
            for future in list(inflight_batches):
                if not future.cancel():
                    running_tail.append(future)

            if running_tail:
                done_tail, _ = wait(tuple(running_tail))
                for future in done_tail:
                    inflight_batches.pop(future)
                    batch_payload = future.result()
                    absorb_batch_payload(batch_payload)

            # Dedupe after scoring. Cross-batch duplicate scoring is intentionally
            # tolerated because it is rare (~1%) and removing Graph/hash work from
            # the parent hot path is much more valuable for scorer utilization.
            unique_payloads: dict[str, CandidateScorePayload] = {}
            for payload in raw_results:
                if payload.graph_hash in visited:
                    duplicate_or_visited += 1
                    continue
                if payload.graph_hash in unique_payloads:
                    duplicate_or_visited += 1
                    continue
                unique_payloads[payload.graph_hash] = payload

            completed: list[BeamState] = []
            for payload in unique_payloads.values():
                parent = beam_by_hash.get(payload.parent_hash)
                if parent is None:
                    raise RuntimeError(
                        f"score payload refers to missing parent "
                        f"{payload.parent_hash[:8]}"
                    )

                graph = Graph.from_edges(payload.order, payload.edges)
                score = GraphScore(
                    graph=graph,
                    components=payload.components,
                    elapsed_seconds=payload.elapsed_seconds,
                )
                completed.append(
                    BeamState(
                        score=score,
                        parent_state=parent,
                        mutation=payload.mutation,
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
                f"full_scored={len(pool)}/{proposal_space} "
                f"c4_gate={c4_passed_total}/{c4_tested_total} "
                f"pruned={c4_pruned_total} "
                f"exact={len(exact_pool)} "
                f"improving={improving} neutral={neutral} uphill={uphill} "
                f"structural={len(analyzed)}/{len(shortlist)} "
                f"(best={shortlist_best_count},escape={shortlist_escape_count}) "
                f"best_child_total={best_child.score.total} "
                f"Δ={best_delta:+d} "
                f"global_best={best.score.total} "
                f"barrier<={barrier} "
                f"c4_ceiling={c4_ceiling} "
                f"time={elapsed:.2f}s"
            )
            console.print(
                f"     best child {components_text(best_child.score)} "
                f"m={len(best_child.score.graph.edges)} "
                f"excess={graph_edge_excess(best_child.score.graph)} "
                f"{best_child_metrics.compact()} "
                f"{best_child.mutation.label() if best_child.mutation else '-'} "
                f"hash={best_child.score.graph.graph_hash[:8]}"
            )
            console.print(
                f"     global best {components_text(best.score)} "
                f"m={len(best.score.graph.edges)} "
                f"excess={graph_edge_excess(best.score.graph)} "
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

            family_space = {
                family: sum(
                    len(bucket.mutations)
                    for bucket in buckets
                    if bucket.family == family
                )
                for family in args.families
            }
            console.print(
                "     proposal space "
                + " ".join(
                    f"{family}={family_space[family]}"
                    for family in args.families
                )
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
                c4_stat = c4_stats_by_family.get(
                    family,
                    {"tested": 0, "pruned": 0, "passed": 0, "min": None},
                )
                c4_min_text = (
                    "n/a" if c4_stat["min"] is None else str(c4_stat["min"])
                )
                console.print(
                    f"       {family:<18} "
                    f"c4={c4_stat['passed']}/{c4_stat['tested']} "
                    f"pruned={c4_stat['pruned']} minPassC4={c4_min_text} "
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
                    f"     full unique scores {len(pool)}/{proposal_space} "
                    "proposal slots after C4 pruning/deadline/dedupe"
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
