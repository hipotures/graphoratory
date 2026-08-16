#!/usr/bin/env python3
"""
HEG stronger local-rewiring growth probe.

This experiment extends the earlier one-edge split+spoke probe with two stronger
order-increasing rewrites. All enabled families have the SAME coarse cost:

    Δorder = +1
    Δedges = +2

so differences are mostly topological rather than a trivial density effect.

Families
========

1. split_spoke  (baseline)
   remove: (u,v)
   add:    (u,x), (x,v), (x,w)

2. double_hub   (new two-edge surgery)
   choose two vertex-disjoint old edges (a,b), (c,d)
   remove both
   add: (a,x), (b,x), (c,x), (d,x)

   Old endpoint degrees are preserved; x has degree 4.

3. switch_spoke (new two-edge surgery)
   choose two vertex-disjoint old edges (a,b), (c,d)
   perform one 2-switch pairing, keep one new cross-edge directly, and subdivide
   the other cross-edge through x; add one extra x-spoke to an endpoint of the
   directly-kept cross-edge.

   Example:
       remove: (a,b), (c,d)
       add:    (a,c), (b,x), (x,d), (x,a)

   Again Δorder=+1 and Δedges=+2.

All candidates are validated as simple, connected graphs with minimum degree >=3.
Because old edges are removed, ΔC4/ΔC8/ΔC16/... may be negative.

The coherent beam primarily minimises the ABSOLUTE forbidden-cycle objective
(total by default). Independently of beam selection, every scored pool reports:

- how many exact candidates have ΔTOTAL < 0,
- the best exact ΔTOTAL globally,
- the same statistics per mutation family,
- how many exact candidates have ΔCk <= 0 for each forbidden length.

Thus a useful negative-delta candidate cannot be hidden merely because it was not
selected into the next beam.

Recommended first run (stop before C32 becomes active):

    uv run python scripts/heg_growth_rewire2_probe.py \
      --start-order 16 \
      --target-order 30 \
      --beam-width 16 \
      --step-seconds 120 \
      --total-seconds 1200 \
      --workers 16 \
      --families split_spoke,double_hub,switch_spoke \
      --objective total \
      --node-budget 10000000 \
      --witness-cap 1000000

The bundled HEG scorer currently supports order <= 128.
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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

SUPPORTED_FAMILIES = ("split_spoke", "double_hub", "switch_spoke")

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

    @property
    def true_lower(self) -> int:
        return self.observed

    @property
    def true_upper(self) -> int | None:
        return self.observed if self.exact else None


@dataclass(frozen=True, slots=True)
class GraphScore:
    graph: Graph
    components: tuple[ComponentScore, ...]
    elapsed_seconds: float

    @property
    def fully_exact(self) -> bool:
        return all(component.exact for component in self.components)

    @property
    def total_lower(self) -> int:
        return sum(component.true_lower for component in self.components)

    @property
    def total_upper(self) -> int | None:
        if not self.fully_exact:
            return None
        return sum(component.observed for component in self.components)

    @property
    def weighted_lower(self) -> int:
        return sum(
            weight(component.length) * component.true_lower
            for component in self.components
        )

    @property
    def weighted_upper(self) -> int | None:
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
    new_neighbors: tuple[int, ...]
    added_old_edges: tuple[Edge, ...] = ()
    variant: str = ""

    def label(self) -> str:
        removed = ",".join(f"{u}-{v}" for u, v in self.removed_edges)
        neighbors = ",".join(str(v) for v in self.new_neighbors)
        suffix = f":{self.variant}" if self.variant else ""
        return f"{self.family}{suffix} rm[{removed}] x[{neighbors}]"


@dataclass(frozen=True, slots=True)
class BeamState:
    score: GraphScore
    parent_state: "BeamState | None"
    mutation: Mutation | None

    @property
    def parent_score(self) -> GraphScore | None:
        return None if self.parent_state is None else self.parent_state.score


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
    # Preserve user order, remove duplicates.
    return tuple(dict.fromkeys(names))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Grow HEG graphs with one-edge and two-edge local rewiring while "
            "measuring whether true forbidden-cycle deltas can become negative."
        )
    )
    parser.add_argument("--start-order", type=int, default=16)
    parser.add_argument(
        "--start-graph",
        type=Path,
        default=None,
        help="Optional JSON with {order, edges}; overrides generated start graph.",
    )

    target = parser.add_mutually_exclusive_group()
    target.add_argument("--target-order", type=int, default=None)
    target.add_argument("--steps", type=int, default=None)

    parser.add_argument("--beam-width", type=int, default=16)
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
        "--objective",
        choices=("total", "weighted", "delta"),
        default="total",
        help=(
            "total: minimise absolute total forbidden cycles; "
            "weighted: minimise short-cycle-weighted absolute score; "
            "delta: minimise current exact ΔTOTAL first (exploratory)."
        ),
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=120.0,
        help="Wall-clock budget per order increment; 0 = unlimited.",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=1200.0,
        help="Whole-run wall-clock budget; 0 = unlimited.",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--max-children-per-family-parent",
        type=int,
        default=0,
        help=(
            "0 = no explicit proposal cap; otherwise sample at most this many "
            "mutations from EACH family for EACH beam parent."
        ),
    )
    parser.add_argument(
        "--inflight-per-worker",
        type=int,
        default=1,
        help="Maximum submitted/running scorer calls per worker.",
    )
    parser.add_argument("--seed", type=int, default=4001)
    parser.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET)
    parser.add_argument("--witness-cap", type=int, default=DEFAULT_WITNESS_CAP)
    parser.add_argument("--save-final", type=Path, default=None)
    parser.add_argument("--save-lineage", type=Path, default=None)
    parser.add_argument(
        "--hit-dir",
        type=Path,
        default=Path("growth_hits"),
        help="Directory created only for an exact zero-forbidden-cycle hit.",
    )

    args = parser.parse_args()

    if args.start_order < 4:
        parser.error("--start-order must be >= 4")
    if args.beam_width < 1:
        parser.error("--beam-width must be >= 1")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.inflight_per_worker < 1:
        parser.error("--inflight-per-worker must be >= 1")
    if args.max_children_per_family_parent < 0:
        parser.error("--max-children-per-family-parent must be >= 0")
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.step_seconds < 0 or args.total_seconds < 0:
        parser.error("time budgets must be >= 0")

    if args.target_order is None and args.steps is None:
        args.target_order = 30

    if args.steps is not None:
        if args.steps < 0:
            parser.error("--steps must be >= 0")
        args.target_order = args.start_order + args.steps

    if args.target_order is None:
        parser.error("could not resolve target order")
    if args.target_order < args.start_order:
        parser.error("--target-order must be >= --start-order")
    if args.target_order > MAX_ORDER:
        parser.error(f"bundled HEG scorer supports order <= {MAX_ORDER}")

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


def load_or_generate_start_graph(args: argparse.Namespace) -> Graph:
    if args.start_graph is not None:
        payload = json.loads(args.start_graph.read_text(encoding="utf-8"))
        graph = Graph.from_edges(
            int(payload["order"]),
            ((int(edge[0]), int(edge[1])) for edge in payload["edges"]),
        )
        if graph.order != args.start_order:
            raise ValueError(
                f"--start-graph order={graph.order}, expected {args.start_order}"
            )
        graph.validate_scientific_invariants(max_order=MAX_ORDER)
        return graph

    config = GraphConfig(
        generator="cycle_matching_stub_pairing",
        workspace_graph_count=1,
        line_graph_count=1,
        min_order=args.start_order,
        max_order=args.start_order,
        seed=args.seed,
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


def apply_mutation(graph: Graph, mutation: Mutation) -> Graph:
    edge_set = set(graph.edges)

    for edge in mutation.removed_edges:
        edge = norm_edge(*edge)
        if edge not in edge_set:
            raise ValueError(f"removed edge {edge} does not exist")

    for edge in mutation.removed_edges:
        edge_set.remove(norm_edge(*edge))

    for edge in mutation.added_old_edges:
        edge = norm_edge(*edge)
        if edge in edge_set:
            raise ValueError(f"added old edge {edge} already exists")
        edge_set.add(edge)

    x = graph.order
    neighbors = tuple(dict.fromkeys(mutation.new_neighbors))
    if len(neighbors) != len(mutation.new_neighbors):
        raise ValueError("new vertex neighbor list contains duplicates")
    if len(neighbors) < 3:
        raise ValueError("new vertex degree would be < 3")
    if any(v < 0 or v >= graph.order for v in neighbors):
        raise ValueError("new vertex neighbor out of range")

    for v in neighbors:
        edge_set.add(norm_edge(v, x))

    candidate = Graph.from_edges(graph.order + 1, edge_set)
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)

    # Controlled density: every supported mutation should add exactly two edges.
    if len(candidate.edges) != len(graph.edges) + 2:
        raise ValueError(
            f"{mutation.family} violated Δedges=+2: "
            f"{len(graph.edges)} -> {len(candidate.edges)}"
        )

    return candidate


def disjoint_edge_pairs(edges: tuple[Edge, ...]) -> Iterator[tuple[Edge, Edge]]:
    for i, first in enumerate(edges):
        first_vertices = set(first)
        for second in edges[i + 1 :]:
            if first_vertices.isdisjoint(second):
                yield first, second


def split_spoke_mutations(graph: Graph) -> list[Mutation]:
    result: list[Mutation] = []
    for edge in graph.edges:
        u, v = edge
        for w in range(graph.order):
            if w == u or w == v:
                continue
            result.append(
                Mutation(
                    family="split_spoke",
                    removed_edges=(edge,),
                    new_neighbors=(u, v, w),
                )
            )
    return result


def double_hub_mutations(graph: Graph) -> list[Mutation]:
    result: list[Mutation] = []
    for first, second in disjoint_edge_pairs(graph.edges):
        a, b = first
        c, d = second
        result.append(
            Mutation(
                family="double_hub",
                removed_edges=(first, second),
                new_neighbors=(a, b, c, d),
            )
        )
    return result


def switch_spoke_mutations(graph: Graph) -> list[Mutation]:
    """
    For disjoint (a,b),(c,d), enumerate both 2-switch pairings.

    For each pairing (cross1, cross2), consider both choices of which cross-edge
    remains an old-old edge and which one is subdivided through x. The spoke goes
    from x to either endpoint of the directly-kept cross-edge.

    Up to 8 variants per disjoint edge pair.
    """
    result: list[Mutation] = []
    edge_set = set(graph.edges)

    for first, second in disjoint_edge_pairs(graph.edges):
        a, b = first
        c, d = second

        pairings = (
            (norm_edge(a, c), norm_edge(b, d), "ac_bd"),
            (norm_edge(a, d), norm_edge(b, c), "ad_bc"),
        )

        for cross1, cross2, pairing_name in pairings:
            for kept, subdivided, role_name in (
                (cross1, cross2, "keep1"),
                (cross2, cross1, "keep2"),
            ):
                # A genuine simple 2-switch cannot directly add an already
                # existing cross-edge.
                if kept in edge_set:
                    continue

                s, t = subdivided
                for spoke in kept:
                    # All four old endpoints are distinct for disjoint source
                    # edges, so spoke differs from s,t.
                    result.append(
                        Mutation(
                            family="switch_spoke",
                            removed_edges=(first, second),
                            new_neighbors=(s, t, spoke),
                            added_old_edges=(kept,),
                            variant=f"{pairing_name}/{role_name}/sp{spoke}",
                        )
                    )

    return result


def mutations_for_family(graph: Graph, family: str) -> list[Mutation]:
    if family == "split_spoke":
        return split_spoke_mutations(graph)
    if family == "double_hub":
        return double_hub_mutations(graph)
    if family == "switch_spoke":
        return switch_spoke_mutations(graph)
    raise ValueError(f"unsupported family {family}")


@dataclass(slots=True)
class CandidateBucket:
    parent: BeamState
    family: str
    mutations: list[Mutation]


def build_candidate_buckets(
    beam: list[BeamState],
    *,
    families: tuple[str, ...],
    step: int,
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
                ^ (step << 20)
                ^ parent_seed
                ^ (family_index * 0x9E3779B97F4A7C15)
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


def interleaved_bucket_candidates(
    buckets: list[CandidateBucket],
    *,
    families: tuple[str, ...],
) -> Iterator[tuple[BeamState, Mutation]]:
    """
    Round-robin by family, then parent, then candidate index.

    This prevents the much larger switch_spoke family from monopolising an early
    time-limited prefix.
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
            f"scorer returned lengths {sorted(by_length)}, expected {list(lengths)}"
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


def component_for(score: GraphScore, length: int) -> ComponentScore | None:
    return score.component_map().get(length)


def component_delta_bounds(
    state: BeamState,
    length: int,
) -> tuple[int | None, int | None]:
    if state.parent_score is None:
        return (None, None)

    child = component_for(state.score, length)
    parent = component_for(state.parent_score, length)

    # If the length was not active in the parent, its true parent count is 0.
    parent_lower = 0 if parent is None else parent.true_lower
    parent_upper = 0 if parent is None else parent.true_upper

    child_lower = 0 if child is None else child.true_lower
    child_upper = 0 if child is None else child.true_upper

    lower = None if parent_upper is None else child_lower - parent_upper
    upper = None if child_upper is None else child_upper - parent_lower
    return (lower, upper)


def total_delta_bounds(state: BeamState) -> tuple[int | None, int | None]:
    if state.parent_score is None:
        return (None, None)

    lengths = sorted(
        set(state.score.component_map()) | set(state.parent_score.component_map())
    )
    bounds = [component_delta_bounds(state, length) for length in lengths]

    lower = (
        None
        if any(item_lower is None for item_lower, _ in bounds)
        else sum(int(item_lower) for item_lower, _ in bounds)
    )
    upper = (
        None
        if any(item_upper is None for _, item_upper in bounds)
        else sum(int(item_upper) for _, item_upper in bounds)
    )
    return (lower, upper)


def weighted_delta_bounds(state: BeamState) -> tuple[int | None, int | None]:
    if state.parent_score is None:
        return (None, None)

    lengths = sorted(
        set(state.score.component_map()) | set(state.parent_score.component_map())
    )
    bounds = [
        (length, component_delta_bounds(state, length))
        for length in lengths
    ]

    lower = (
        None
        if any(item_lower is None for _, (item_lower, _) in bounds)
        else sum(
            weight(length) * int(item_lower)
            for length, (item_lower, _) in bounds
        )
    )
    upper = (
        None
        if any(item_upper is None for _, (_, item_upper) in bounds)
        else sum(
            weight(length) * int(item_upper)
            for length, (_, item_upper) in bounds
        )
    )
    return (lower, upper)


def known_upper_key(value: int | None) -> tuple[int, int]:
    return (1, 0) if value is None else (0, value)


def exact_delta_value(state: BeamState) -> int | None:
    lower, upper = total_delta_bounds(state)
    if lower is None or upper is None or lower != upper:
        return None
    return lower


def score_rank_key(state: BeamState, objective: str) -> tuple[int, ...]:
    total_delta_lower, total_delta_upper = total_delta_bounds(state)
    weighted_delta_lower, weighted_delta_upper = weighted_delta_bounds(state)

    if objective == "delta":
        return (
            *known_upper_key(total_delta_upper),
            0 if total_delta_lower is None else total_delta_lower,
            *known_upper_key(state.score.total_upper),
            state.score.total_lower,
            *known_upper_key(state.score.weighted_upper),
            state.score.weighted_lower,
        )

    if objective == "weighted":
        return (
            *known_upper_key(state.score.weighted_upper),
            state.score.weighted_lower,
            *known_upper_key(state.score.total_upper),
            state.score.total_lower,
            *known_upper_key(weighted_delta_upper),
            0 if weighted_delta_lower is None else weighted_delta_lower,
            *known_upper_key(total_delta_upper),
            0 if total_delta_lower is None else total_delta_lower,
        )

    return (
        *known_upper_key(state.score.total_upper),
        state.score.total_lower,
        *known_upper_key(total_delta_upper),
        0 if total_delta_lower is None else total_delta_lower,
        *known_upper_key(state.score.weighted_upper),
        state.score.weighted_lower,
    )


def select_beam(
    states: list[BeamState],
    *,
    beam_width: int,
    objective: str,
) -> list[BeamState]:
    return sorted(
        states,
        key=lambda state: (
            score_rank_key(state, objective),
            state.score.graph.graph_hash,
        ),
    )[:beam_width]


def score_status(score: GraphScore) -> str:
    if score.fully_exact:
        return "OK"

    saturated = [
        f"C{component.length}"
        for component in score.components
        if component.status == STATUS_SATURATED
    ]
    budget = [
        f"C{component.length}"
        for component in score.components
        if component.status == STATUS_BUDGET
    ]

    parts: list[str] = []
    if saturated:
        parts.append("CAP:" + ",".join(saturated))
    if budget:
        parts.append("BUD:" + ",".join(budget))
    return " ".join(parts)


def absolute_component_text(component: ComponentScore | None) -> str:
    if component is None:
        return "-"
    if component.exact:
        return str(component.observed)
    if component.status == STATUS_SATURATED:
        return f">={component.observed}"
    return f">={component.observed}?"


def delta_text(bounds: tuple[int | None, int | None]) -> str:
    lower, upper = bounds
    if lower is not None and upper is not None and lower == upper:
        return f"{lower:+d}"
    return (
        f"[{'?' if lower is None else f'{lower:+d}'},"
        f"{'?' if upper is None else f'{upper:+d}'}]"
    )


def cycle_cell(state: BeamState, length: int, *, root: bool = False) -> str:
    component = component_for(state.score, length)
    absolute = absolute_component_text(component)
    if root:
        return absolute
    return f"{absolute}({delta_text(component_delta_bounds(state, length))})"


def total_cell(state: BeamState, *, root: bool = False) -> str:
    absolute = (
        str(state.score.total_upper)
        if state.score.total_upper is not None
        else f">={state.score.total_lower}?"
    )
    if root:
        return absolute
    return f"{absolute}({delta_text(total_delta_bounds(state))})"


def exact_nonpositive_component_delta(state: BeamState, length: int) -> bool:
    lower, upper = component_delta_bounds(state, length)
    return (
        lower is not None
        and upper is not None
        and lower == upper
        and upper <= 0
    )


def family_stats(
    pool: list[BeamState],
    families: tuple[str, ...],
) -> dict[str, tuple[int, int, int | None, BeamState | None]]:
    """
    family -> (scored, exact_negative_count, best_exact_delta, best_state)
    """
    result: dict[str, tuple[int, int, int | None, BeamState | None]] = {}

    for family in families:
        states = [
            state
            for state in pool
            if state.mutation is not None and state.mutation.family == family
        ]

        exact_pairs = [
            (value, state)
            for state in states
            if (value := exact_delta_value(state)) is not None
        ]
        negative_count = sum(value < 0 for value, _ in exact_pairs)

        if exact_pairs:
            best_value, best_state = min(
                exact_pairs,
                key=lambda pair: (pair[0], pair[1].score.graph.graph_hash),
            )
        else:
            best_value, best_state = None, None

        result[family] = (
            len(states),
            negative_count,
            best_value,
            best_state,
        )

    return result


class Ledger:
    def __init__(self) -> None:
        self._lengths: tuple[int, ...] | None = None

    def _print_header(self, lengths: tuple[int, ...]) -> None:
        cycle_headers = " ".join(
            f"{('C'+str(length)+'(Δ)'):>18}"
            for length in lengths
        )
        console.print(
            f"{'STEP':>4} {'N':>3} {'SCORED/SPACE':>15} {'BEAM':>4} "
            f"{'FAMILY':>13} {cycle_headers} {'TOTAL(Δ)':>22} "
            f"{'TIME':>8} STATE"
        )

    def print_step(
        self,
        *,
        step: int,
        leader: BeamState,
        pool: list[BeamState] | None,
        scored: int | None,
        space: int | None,
        beam_size: int,
        elapsed: float,
        families: tuple[str, ...],
    ) -> None:
        lengths = tuple(
            int(length)
            for length in forbidden_lengths(leader.score.graph.order)
        )
        if lengths != self._lengths:
            if self._lengths is not None:
                console.print()
            self._print_header(lengths)
            self._lengths = lengths

        coverage = "-" if scored is None or space is None else f"{scored}/{space}"
        family = "-" if leader.mutation is None else leader.mutation.family
        cells = [
            f"{cycle_cell(leader, length, root=(step == 0)):>18}"
            for length in lengths
        ]

        console.print(
            f"{step:>4} {leader.score.graph.order:>3} {coverage:>15} "
            f"{beam_size:>4} {family:>13} "
            + " ".join(cells)
            + f" {total_cell(leader, root=(step == 0)):>22} "
            f"{elapsed:>7.2f}s {score_status(leader.score)}"
        )

        if leader.mutation is not None:
            console.print(
                f"     leader {leader.mutation.label()} "
                f"hash={leader.score.graph.graph_hash[:8]}"
            )

        if not pool:
            return

        exact_pairs = [
            (value, state)
            for state in pool
            if (value := exact_delta_value(state)) is not None
        ]
        exact_negative = sum(value < 0 for value, _ in exact_pairs)

        if exact_pairs:
            best_value, best_state = min(
                exact_pairs,
                key=lambda pair: (pair[0], pair[1].score.graph.graph_hash),
            )
            best_text = (
                f"{best_value:+d} {best_state.mutation.family} "
                f"{best_state.mutation.label()}"
            )
        else:
            best_text = "unknown"

        console.print(
            f"     GLOBAL exact ΔTOTAL<0: {exact_negative}/{len(pool)}; "
            f"best exact ΔTOTAL={best_text}"
        )

        stats = family_stats(pool, families)
        for family_name in families:
            family_scored, family_negative, best_delta, best_state = stats[family_name]
            best_family = (
                "unknown"
                if best_delta is None or best_state is None
                else f"{best_delta:+d} {best_state.mutation.label()}"
            )
            console.print(
                f"       {family_name:<13} scored={family_scored:<7} "
                f"negative={family_negative:<6} best={best_family}"
            )

        nonincrease = []
        for length in lengths:
            count = sum(
                exact_nonpositive_component_delta(state, length)
                for state in pool
            )
            nonincrease.append(f"C{length}:{count}")

        console.print(
            "     exact ΔCk<=0 candidates: " + " ".join(nonincrease)
        )


def lineage_path(state: BeamState) -> list[BeamState]:
    path: list[BeamState] = []
    current: BeamState | None = state
    while current is not None:
        path.append(current)
        current = current.parent_state
    path.reverse()
    return path


def print_final_lineage(final_state: BeamState) -> None:
    path = lineage_path(final_state)
    lengths = tuple(
        int(length)
        for length in forbidden_lengths(final_state.score.graph.order)
    )

    console.print()
    console.print(
        f"[bold]FINAL STRONG-REWIRE LINEAGE[/bold] "
        f"steps={len(path)-1} "
        f"order={path[0].score.graph.order}->{final_state.score.graph.order}"
    )

    cycle_headers = " ".join(
        f"{('C'+str(length)+'(Δ)'):>18}"
        for length in lengths
    )
    console.print(
        f"{'N':>3} {'FAMILY':>13} {cycle_headers} {'TOTAL(Δ)':>22} "
        f"{'HASH':>8} STATE"
    )

    negative_steps = 0
    family_counts: dict[str, int] = {}

    for index, state in enumerate(path):
        root = index == 0
        family = "-" if state.mutation is None else state.mutation.family
        if state.mutation is not None:
            family_counts[family] = family_counts.get(family, 0) + 1

        delta = None if root else exact_delta_value(state)
        if delta is not None and delta < 0:
            negative_steps += 1

        cells = [
            f"{cycle_cell(state, length, root=root):>18}"
            for length in lengths
        ]
        console.print(
            f"{state.score.graph.order:>3} {family:>13} "
            + " ".join(cells)
            + f" {total_cell(state, root=root):>22} "
            f"{state.score.graph.graph_hash[:8]:>8} "
            f"{score_status(state.score)}"
        )

        if state.mutation is not None:
            console.print(f"     {state.mutation.label()}")

    root_total = path[0].score.total_upper
    final_total = final_state.score.total_upper
    cumulative = (
        "unknown"
        if root_total is None or final_total is None
        else f"{final_total-root_total:+d}"
    )

    family_summary = " ".join(
        f"{name}:{family_counts.get(name, 0)}"
        for name in SUPPORTED_FAMILIES
        if family_counts.get(name, 0)
    )

    console.print(
        f"Lineage summary: negative exact ΔTOTAL steps={negative_steps}/"
        f"{len(path)-1}; cumulative TOTAL change={cumulative}; "
        f"families={family_summary or '-'}"
    )


def save_graph(path: Path, state: BeamState) -> None:
    payload = {
        **state.score.graph.record(),
        "mutation": (
            None
            if state.mutation is None
            else {
                "family": state.mutation.family,
                "removed_edges": [list(edge) for edge in state.mutation.removed_edges],
                "new_neighbors": list(state.mutation.new_neighbors),
                "added_old_edges": [
                    list(edge) for edge in state.mutation.added_old_edges
                ],
                "variant": state.mutation.variant,
            }
        ),
        "forbidden_cycle_score": {
            str(component.length): {
                "observed": component.observed,
                "status": component.status,
                "true_lower": component.true_lower,
                "true_upper": component.true_upper,
            }
            for component in state.score.components
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_lineage(path: Path, final_state: BeamState) -> None:
    states = lineage_path(final_state)
    payload = {
        "schema_version": "graphoratory.heg_strong_rewire_lineage.v1",
        "root_order": states[0].score.graph.order,
        "final_order": states[-1].score.graph.order,
        "states": [],
    }

    for index, state in enumerate(states):
        root = index == 0
        payload["states"].append(
            {
                "order": state.score.graph.order,
                "graph_hash": state.score.graph.graph_hash,
                "mutation": (
                    None
                    if state.mutation is None
                    else {
                        "family": state.mutation.family,
                        "removed_edges": [
                            list(edge) for edge in state.mutation.removed_edges
                        ],
                        "new_neighbors": list(state.mutation.new_neighbors),
                        "added_old_edges": [
                            list(edge) for edge in state.mutation.added_old_edges
                        ],
                        "variant": state.mutation.variant,
                    }
                ),
                "components": {
                    str(component.length): {
                        "observed": component.observed,
                        "status": component.status,
                        "delta_lower": (
                            None
                            if root
                            else component_delta_bounds(state, component.length)[0]
                        ),
                        "delta_upper": (
                            None
                            if root
                            else component_delta_bounds(state, component.length)[1]
                        ),
                    }
                    for component in state.score.components
                },
                "total": {
                    "lower": state.score.total_lower,
                    "upper": state.score.total_upper,
                    "delta_lower": None if root else total_delta_bounds(state)[0],
                    "delta_upper": None if root else total_delta_bounds(state)[1],
                },
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def maybe_save_hit(args: argparse.Namespace, state: BeamState) -> Path | None:
    if not state.score.fully_exact or state.score.total_upper != 0:
        return None

    args.hit_dir.mkdir(parents=True, exist_ok=True)
    path = args.hit_dir / (
        f"heg-zero-strong-rewire-order-{state.score.graph.order}-"
        f"{state.score.graph.graph_hash[:8]}.json"
    )
    save_graph(path, state)
    return path


def main() -> int:
    args = parse_args()
    start_graph = load_or_generate_start_graph(args)

    console.print(
        f"[bold]HEG stronger local-rewiring probe[/bold] "
        f"start={args.start_order} target={args.target_order} "
        f"beam={args.beam_width} workers={args.workers} objective={args.objective}"
    )
    console.print(
        f"families={','.join(args.families)} "
        f"step_budget={args.step_seconds or 'unlimited'}s "
        f"total_budget={args.total_seconds or 'unlimited'}s "
        f"node_budget={args.node_budget:,} cap={args.witness_cap:,}"
    )
    console.print(
        "[dim]All enabled families use Δorder=+1 and Δedges=+2. "
        "Two-edge surgeries can destroy multiple old forbidden cycles in one "
        "atomic move.[/dim]"
    )

    started = time.perf_counter()
    total_deadline = (
        started + args.total_seconds
        if args.total_seconds > 0
        else math.inf
    )
    max_inflight = args.workers * args.inflight_per_worker
    ledger = Ledger()

    with ThreadPoolExecutor(
        max_workers=args.workers,
        initializer=_thread_worker_init,
        thread_name_prefix="heg-score",
    ) as executor:
        initial_score = executor.submit(
            score_graph,
            start_graph,
            witness_cap=args.witness_cap,
            node_budget=args.node_budget,
        ).result()

        beam = [
            BeamState(
                score=initial_score,
                parent_state=None,
                mutation=None,
            )
        ]

        ledger.print_step(
            step=0,
            leader=beam[0],
            pool=None,
            scored=None,
            space=None,
            beam_size=1,
            elapsed=initial_score.elapsed_seconds,
            families=args.families,
        )

        hit = maybe_save_hit(args, beam[0])
        if hit is not None:
            console.print(
                f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] saved: {hit}"
            )
            return 0

        for step in range(1, args.target_order - args.start_order + 1):
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

            buckets = build_candidate_buckets(
                beam,
                families=args.families,
                step=step,
                seed=args.seed,
                limit_per_family_parent=args.max_children_per_family_parent,
            )
            generated_space = sum(len(bucket.mutations) for bucket in buckets)
            stream = interleaved_bucket_candidates(
                buckets,
                families=args.families,
            )

            seen_hashes: set[str] = set()
            metadata: dict[str, tuple[BeamState, Mutation]] = {}
            inflight: dict[Future[GraphScore], str] = {}
            completed_states: list[BeamState] = []

            submitted = 0
            invalid_structural = 0
            duplicate_graphs = 0
            exhausted_stream = False

            def submit_until_full() -> None:
                nonlocal exhausted_stream
                nonlocal submitted
                nonlocal invalid_structural
                nonlocal duplicate_graphs
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
                        invalid_structural += 1
                        continue

                    graph_hash = candidate.graph_hash
                    if graph_hash in seen_hashes:
                        duplicate_graphs += 1
                        continue
                    seen_hashes.add(graph_hash)

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
                    completed_states.append(
                        BeamState(
                            score=score,
                            parent_state=parent,
                            mutation=mutation,
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
                    completed_states.append(
                        BeamState(
                            score=score,
                            parent_state=parent,
                            mutation=mutation,
                        )
                    )

            if not completed_states:
                console.print(
                    f"[yellow]Step {step}: no candidate score completed.[/yellow]"
                )
                break

            unique_states = {
                state.score.graph.graph_hash: state
                for state in completed_states
            }
            pool = list(unique_states.values())

            hits = [
                state
                for state in pool
                if state.score.fully_exact and state.score.total_upper == 0
            ]
            if hits:
                hit_state = min(
                    hits,
                    key=lambda state: state.score.graph.graph_hash,
                )
                path = maybe_save_hit(args, hit_state)
                console.print(
                    f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] "
                    f"order={hit_state.score.graph.order} saved: {path}"
                )
                print_final_lineage(hit_state)
                return 0

            beam = select_beam(
                pool,
                beam_width=args.beam_width,
                objective=args.objective,
            )
            leader = beam[0]

            elapsed = time.perf_counter() - step_started
            ledger.print_step(
                step=step,
                leader=leader,
                pool=pool,
                scored=len(pool),
                space=generated_space,
                beam_size=len(beam),
                elapsed=elapsed,
                families=args.families,
            )

            inexact = sum(not state.score.fully_exact for state in beam)
            if inexact:
                console.print(
                    f"     beam health: {inexact}/{len(beam)} inexact"
                )

            if invalid_structural or duplicate_graphs:
                console.print(
                    f"     structural-invalid={invalid_structural} "
                    f"duplicate-graphs={duplicate_graphs}"
                )

            if len(pool) < generated_space:
                console.print(
                    f"     scored {len(pool)}/{generated_space} generated "
                    "mutations before deadline/dedupe"
                )
                if submitted != len(pool):
                    console.print(
                        f"     submitted={submitted}; "
                        f"{submitted-len(pool)} did not contribute a completed "
                        "unique score"
                    )

    final_leader = beam[0]

    if args.save_final is not None:
        save_graph(args.save_final, final_leader)
        console.print(f"Final leader saved: {args.save_final}")

    if args.save_lineage is not None:
        save_lineage(args.save_lineage, final_leader)
        console.print(f"Final lineage saved: {args.save_lineage}")

    print_final_lineage(final_leader)

    final_total = (
        str(final_leader.score.total_upper)
        if final_leader.score.total_upper is not None
        else f">={final_leader.score.total_lower}"
    )
    console.print(
        f"[bold]Done[/bold] order={final_leader.score.graph.order} "
        f"beam={len(beam)} total={final_total} "
        f"elapsed={time.perf_counter()-started:.2f}s "
        f"hash={final_leader.score.graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
