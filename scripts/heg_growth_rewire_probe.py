#!/usr/bin/env python3
"""
HEG local-rewiring growth probe.

Purpose
-------
The earlier growth probes only added a new degree-3 vertex. Therefore every old
cycle remained present and true cycle counts were monotone non-decreasing.

This probe uses one atomic order-increasing local rewrite that can DESTROY old
forbidden cycles:

    split_edge_plus_spoke

For an existing edge (u, v) and a third old vertex w:

    remove: (u, v)
    add:    (u, x), (x, v), (x, w)

where x is the new vertex.

Properties:
- order increases by exactly 1,
- edge count increases by exactly 2,
- x has degree 3,
- degrees of u and v are unchanged,
- degree of w increases by 1,
- a valid connected min-degree>=3 parent remains connected and min-degree>=3,
- every old cycle using (u, v) is destroyed as a cycle of the same length
  (the subdivided counterpart has length +1),
- the extra spoke can create new cycles.

So, unlike pure addition, ΔC4 / ΔC8 / ΔC16 / ... may be NEGATIVE.

The search keeps a coherent beam and primarily minimises the current absolute
total number of forbidden cycles. For states scored exactly, this is equivalent
to minimising cumulative ΔTOTAL from the common root. Current-step ΔTOTAL and
the short-cycle-weighted total are tie-breakers.

The script is intentionally standalone/in-memory and reuses Graphoratory's
existing Graph model/generator and HEG ScoreWorker. It does not use workspace
SQLite or persist ordinary intermediate states.

Example:

    uv run python scripts/heg_growth_rewire_probe.py \
      --start-order 16 \
      --steps 30 \
      --beam-width 16 \
      --step-seconds 120 \
      --total-seconds 1200 \
      --workers 16 \
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


MAX_ORDER = 128
DEFAULT_WITNESS_CAP = 1_000_000
DEFAULT_NODE_BUDGET = 10_000_000

STATUS_EXACT = "EXACT"
STATUS_SATURATED = "SATURATED_AT_CAP"
STATUS_BUDGET = "SEARCH_BUDGET_EXHAUSTED"

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
        # EXACT: exact true count.
        # SATURATED/BUDGET: only a lower bound on the true count.
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
    removed_edge: tuple[int, int]
    spoke: int

    def label(self) -> str:
        u, v = self.removed_edge
        return f"split({u},{v})+{self.spoke}"


@dataclass(frozen=True, slots=True)
class BeamState:
    score: GraphScore
    parent_state: BeamState | None
    mutation: Mutation | None

    @property
    def parent_score(self) -> GraphScore | None:
        return None if self.parent_state is None else self.parent_state.score


def weight(length: int) -> int:
    return max(1, 64 // length)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Increase graph order with edge-subdivision + spoke rewiring so "
            "forbidden-cycle deltas may become negative."
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
    target.add_argument(
        "--steps",
        type=int,
        default=None,
        help="Number of order increments.",
    )

    parser.add_argument("--beam-width", type=int, default=16)
    parser.add_argument(
        "--objective",
        choices=("total", "weighted"),
        default="total",
        help=(
            "total: minimise absolute sum C4+C8+C16+...; "
            "weighted: minimise current Graphoratory short-cycle-weighted sum."
        ),
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=120.0,
        help="Wall-clock search budget per order increment; 0 = unlimited.",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=1200.0,
        help="Whole-run wall-clock budget; 0 = unlimited.",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--max-children-per-parent",
        type=int,
        default=0,
        help=(
            "0 = exhaustively try every edge/spoke rewrite for each beam parent; "
            "otherwise sample at most this many rewrites per parent."
        ),
    )
    parser.add_argument(
        "--inflight-per-worker",
        type=int,
        default=1,
        help="Maximum submitted/running scorer jobs per worker.",
    )
    parser.add_argument("--seed", type=int, default=4001)
    parser.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET)
    parser.add_argument("--witness-cap", type=int, default=DEFAULT_WITNESS_CAP)
    parser.add_argument(
        "--save-final",
        type=Path,
        default=None,
        help="Optional JSON path for final leader graph + score.",
    )
    parser.add_argument(
        "--save-lineage",
        type=Path,
        default=None,
        help="Optional JSON path for the complete final coherent lineage.",
    )
    parser.add_argument(
        "--hit-dir",
        type=Path,
        default=Path("growth_hits"),
        help="Directory used only when an exact zero-forbidden-cycle hit appears.",
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
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.step_seconds < 0 or args.total_seconds < 0:
        parser.error("time budgets must be >= 0")
    if args.max_children_per_parent < 0:
        parser.error("--max-children-per-parent must be >= 0")

    if args.target_order is None and args.steps is None:
        args.steps = 30

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
                f"--start-graph has order {graph.order}, "
                f"but --start-order={args.start_order}"
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


def apply_split_edge_plus_spoke(graph: Graph, mutation: Mutation) -> Graph:
    """
    Replace old edge (u,v) by u-x-v and add x-w.

    This construction preserves connectedness and old minimum degrees:
    - u, v keep their degrees,
    - w gains one,
    - new x has degree 3.
    """
    u, v = mutation.removed_edge
    w = mutation.spoke
    if w == u or w == v:
        raise ValueError("spoke must differ from both removed-edge endpoints")

    old_edge = (min(u, v), max(u, v))
    edge_set = set(graph.edges)
    if old_edge not in edge_set:
        raise ValueError(f"removed edge {old_edge} does not exist")

    edge_set.remove(old_edge)
    x = graph.order
    edge_set.add((min(u, x), max(u, x)))
    edge_set.add((min(v, x), max(v, x)))
    edge_set.add((min(w, x), max(w, x)))

    candidate = Graph.from_edges(graph.order + 1, edge_set)
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)
    return candidate


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
            f"scorer returned lengths {sorted(by_length)} but expected {list(lengths)}"
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
    """
    Sound true-count bounds for child Ck - parent Ck.

    None means unbounded/unknown on that side.

    Unlike the add-only probes, no monotonicity assumption is used here because
    local rewiring may make ΔCk negative.
    """
    child = component_for(state.score, length)
    parent_score = state.parent_score
    if parent_score is None:
        return (None, None)

    parent = component_for(parent_score, length)

    # Newly active forbidden length: parent order < length, so true parent count
    # is exactly zero.
    parent_lower = 0 if parent is None else parent.true_lower
    parent_upper = 0 if parent is None else parent.true_upper

    if child is None:
        child_lower = child_upper = 0
    else:
        child_lower = child.true_lower
        child_upper = child.true_upper

    lower: int | None
    upper: int | None

    # lower(child-parent) = child_lower - parent_upper
    lower = None if parent_upper is None else child_lower - parent_upper

    # upper(child-parent) = child_upper - parent_lower
    upper = None if child_upper is None else child_upper - parent_lower

    return (lower, upper)


def component_delta_exact(state: BeamState, length: int) -> bool:
    lower, upper = component_delta_bounds(state, length)
    return lower is not None and upper is not None and lower == upper


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


def bound_key(value: int | None, *, prefer_small: bool = True) -> tuple[int, int]:
    # Known finite values always outrank unknown values.
    if value is None:
        return (1, 0)
    return (0, value if prefer_small else -value)


def score_rank_key(state: BeamState, objective: str) -> tuple[int, ...]:
    score = state.score
    delta_lower, delta_upper = total_delta_bounds(state)
    weighted_delta_lower, weighted_delta_upper = weighted_delta_bounds(state)

    if objective == "weighted":
        return (
            *bound_key(score.weighted_upper),
            score.weighted_lower,
            *bound_key(score.total_upper),
            score.total_lower,
            *bound_key(weighted_delta_upper),
            0 if weighted_delta_lower is None else weighted_delta_lower,
            *bound_key(delta_upper),
            0 if delta_lower is None else delta_lower,
        )

    return (
        *bound_key(score.total_upper),
        score.total_lower,
        *bound_key(delta_upper),
        0 if delta_lower is None else delta_lower,
        *bound_key(score.weighted_upper),
        score.weighted_lower,
        *bound_key(weighted_delta_upper),
        0 if weighted_delta_lower is None else weighted_delta_lower,
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


def mutation_candidates(
    graph: Graph,
    *,
    seed: int,
    limit: int,
) -> list[Mutation]:
    """
    Enumerate all labelled split-edge-plus-spoke rewrites.

    For each existing edge (u,v), every old vertex w != u,v is one legal
    structural proposal. Candidate graph validity is guaranteed by construction.
    """
    candidates = [
        Mutation(removed_edge=edge, spoke=w)
        for edge in graph.edges
        for w in range(graph.order)
        if w not in edge
    ]

    rng = random.Random(seed)
    rng.shuffle(candidates)

    if limit:
        del candidates[limit:]
    return candidates


def candidate_space_size(
    beam: list[BeamState],
    *,
    max_children_per_parent: int,
) -> int:
    total = 0
    for state in beam:
        graph = state.score.graph
        space = len(graph.edges) * max(0, graph.order - 2)
        total += (
            min(space, max_children_per_parent)
            if max_children_per_parent
            else space
        )
    return total


def interleaved_candidates(
    beam: list[BeamState],
    *,
    step: int,
    seed: int,
    max_children_per_parent: int,
) -> Iterator[tuple[BeamState, Mutation]]:
    per_parent = [
        mutation_candidates(
            state.score.graph,
            seed=(
                seed
                ^ step
                ^ int(state.score.graph.graph_hash[:16], 16)
                ^ 0x9E3779B97F4A7C15
            ),
            limit=max_children_per_parent,
        )
        for state in beam
    ]

    maximum = max((len(items) for items in per_parent), default=0)
    for child_index in range(maximum):
        for parent, items in zip(beam, per_parent, strict=True):
            if child_index < len(items):
                yield parent, items[child_index]


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
    lower_text = "?" if lower is None else f"{lower:+d}"
    upper_text = "?" if upper is None else f"{upper:+d}"
    return f"[{lower_text},{upper_text}]"


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


def exact_negative_total_delta(state: BeamState) -> bool:
    lower, upper = total_delta_bounds(state)
    return (
        lower is not None
        and upper is not None
        and lower == upper
        and upper < 0
    )


def exact_nonpositive_component_delta(state: BeamState, length: int) -> bool:
    lower, upper = component_delta_bounds(state, length)
    return (
        lower is not None
        and upper is not None
        and lower == upper
        and upper <= 0
    )


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
            f"{cycle_headers} {'TOTAL(Δ)':>22} {'TIME':>8}  STATE"
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
    ) -> None:
        lengths = tuple(int(length) for length in forbidden_lengths(leader.score.graph.order))
        if lengths != self._lengths:
            if self._lengths is not None:
                console.print()
            self._print_header(lengths)
            self._lengths = lengths

        coverage = "-" if scored is None or space is None else f"{scored}/{space}"
        cells = [
            f"{cycle_cell(leader, length, root=(step == 0)):>18}"
            for length in lengths
        ]

        console.print(
            f"{step:>4} {leader.score.graph.order:>3} {coverage:>15} "
            f"{beam_size:>4} "
            + " ".join(cells)
            + f" {total_cell(leader, root=(step == 0)):>22} "
            f"{elapsed:>7.2f}s  {score_status(leader.score)}"
        )

        if leader.mutation is not None:
            console.print(
                f"     leader {leader.mutation.label()} "
                f"hash={leader.score.graph.graph_hash[:8]}"
            )

        if pool:
            exact_negative = sum(exact_negative_total_delta(state) for state in pool)

            exact_delta_states = []
            for state in pool:
                lower, upper = total_delta_bounds(state)
                if lower is not None and upper is not None and lower == upper:
                    exact_delta_states.append((lower, state))

            best_delta_text = "unknown"
            if exact_delta_states:
                best_delta, best_state = min(
                    exact_delta_states,
                    key=lambda item: (item[0], item[1].score.graph.graph_hash),
                )
                best_delta_text = (
                    f"{best_delta:+d} via {best_state.mutation.label() if best_state.mutation else '-'}"
                )

            nonincrease_parts = []
            for length in lengths:
                count = sum(
                    exact_nonpositive_component_delta(state, length)
                    for state in pool
                )
                nonincrease_parts.append(f"C{length}:{count}")

            console.print(
                f"     negative exact ΔTOTAL: {exact_negative}/{len(pool)}; "
                f"best exact ΔTOTAL={best_delta_text}"
            )
            console.print(
                "     exact ΔCk<=0 candidates: " + " ".join(nonincrease_parts)
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
    final_order = final_state.score.graph.order
    lengths = tuple(int(length) for length in forbidden_lengths(final_order))

    console.print()
    console.print(
        f"[bold]FINAL REWIRING LINEAGE[/bold] "
        f"steps={len(path)-1} order={path[0].score.graph.order}->{final_order}"
    )

    cycle_headers = " ".join(
        f"{('C'+str(length)+'(Δ)'):>18}"
        for length in lengths
    )
    console.print(
        f"{'N':>3} {cycle_headers} {'TOTAL(Δ)':>22} "
        f"{'MUTATION':>20} {'HASH':>8} STATE"
    )

    negative_steps = 0
    for index, state in enumerate(path):
        root = index == 0
        cells = [
            f"{cycle_cell(state, length, root=root):>18}"
            for length in lengths
        ]
        if not root and exact_negative_total_delta(state):
            negative_steps += 1
        mutation = "-" if state.mutation is None else state.mutation.label()

        console.print(
            f"{state.score.graph.order:>3} "
            + " ".join(cells)
            + f" {total_cell(state, root=root):>22} "
            f"{mutation:>20} {state.score.graph.graph_hash[:8]:>8} "
            f"{score_status(state.score)}"
        )

    root_total = path[0].score.total_upper
    final_total = final_state.score.total_upper
    cumulative = "unknown"
    if root_total is not None and final_total is not None:
        cumulative = f"{final_total-root_total:+d}"

    console.print(
        f"Lineage summary: exact negative-ΔTOTAL steps={negative_steps}/"
        f"{len(path)-1}; cumulative TOTAL change={cumulative}"
    )


def save_graph(path: Path, state: BeamState) -> None:
    payload = {
        **state.score.graph.record(),
        "mutation": (
            None
            if state.mutation is None
            else {
                "family": "split_edge_plus_spoke",
                "removed_edge": list(state.mutation.removed_edge),
                "spoke": state.mutation.spoke,
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
        "schema_version": "graphoratory.heg_rewire_growth_lineage.v1",
        "mutation_family": "split_edge_plus_spoke",
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
                        "removed_edge": list(state.mutation.removed_edge),
                        "spoke": state.mutation.spoke,
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
        f"heg-zero-rewire-order-{state.score.graph.order}-"
        f"{state.score.graph.graph_hash[:8]}.json"
    )
    save_graph(path, state)
    return path


def main() -> int:
    args = parse_args()
    start_graph = load_or_generate_start_graph(args)

    console.print(
        f"[bold]HEG local-rewiring growth probe[/bold] "
        f"start={args.start_order} target={args.target_order} "
        f"beam={args.beam_width} workers={args.workers} objective={args.objective}"
    )
    console.print(
        f"mutation=split_edge_plus_spoke "
        f"step_budget={args.step_seconds or 'unlimited'}s "
        f"total_budget={args.total_seconds or 'unlimited'}s "
        f"node_budget={args.node_budget:,} cap={args.witness_cap:,}"
    )
    console.print(
        "[dim]Each step removes one old edge, subdivides it through the new "
        "degree-3 vertex, and adds one extra spoke. Existing forbidden cycles "
        "may therefore be destroyed and ΔCk may be negative.[/dim]"
    )

    started = time.perf_counter()
    total_deadline = (
        started + args.total_seconds
        if args.total_seconds > 0
        else math.inf
    )
    ledger = Ledger()
    max_inflight = args.workers * args.inflight_per_worker

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
        )

        initial_hit = maybe_save_hit(args, beam[0])
        if initial_hit is not None:
            console.print(
                f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] saved: {initial_hit}"
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

            full_space = candidate_space_size(
                beam,
                max_children_per_parent=args.max_children_per_parent,
            )
            stream = interleaved_candidates(
                beam,
                step=step,
                seed=args.seed,
                max_children_per_parent=args.max_children_per_parent,
            )

            seen_hashes: set[str] = set()
            metadata: dict[
                str, tuple[BeamState, Mutation]
            ] = {}
            inflight: dict[Future[GraphScore], str] = {}
            completed_states: list[BeamState] = []
            submitted = 0
            exhausted_stream = False

            def submit_until_full() -> None:
                nonlocal exhausted_stream, submitted
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

                    candidate = apply_split_edge_plus_spoke(
                        parent.score.graph,
                        mutation,
                    )
                    graph_hash = candidate.graph_hash
                    if graph_hash in seen_hashes:
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

            # Cleanly drain already-running calls; cancel only work that did not
            # start. This can overrun the nominal step budget by roughly one
            # scorer call per worker.
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

            # Counterexample detection happens over the entire scored pool, not
            # only after beam truncation.
            exact_hits = [
                state
                for state in pool
                if state.score.fully_exact and state.score.total_upper == 0
            ]
            if exact_hits:
                hit_state = min(
                    exact_hits,
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
                space=full_space,
                beam_size=len(beam),
                elapsed=elapsed,
            )

            inexact = sum(not state.score.fully_exact for state in beam)
            if inexact:
                console.print(
                    f"     beam health: {inexact}/{len(beam)} states contain "
                    "cap/budget-limited components"
                )

            if len(pool) < full_space:
                console.print(
                    f"     scored {len(pool)}/{full_space} rewrites "
                    "before the step deadline"
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

    console.print(
        f"[bold]Done[/bold] order={final_leader.score.graph.order} "
        f"beam={len(beam)} total="
        f"{final_leader.score.total_upper if final_leader.score.total_upper is not None else '>='+str(final_leader.score.total_lower)} "
        f"elapsed={time.perf_counter()-started:.2f}s "
        f"hash={final_leader.score.graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
