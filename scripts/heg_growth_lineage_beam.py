#!/usr/bin/env python3
"""
HEG coherent-lineage beam growth probe.

A small in-memory experiment for the Erdős–Gyárfás search:

- start from one valid minimum-degree-3 graph,
- increase the order by exactly one at every growth step,
- attach the new vertex to exactly three existing vertices,
- evaluate many children in parallel with the existing HEG score worker,
- retain a beam of several promising graph states instead of one greedy path.

The script intentionally does NOT use Graphoratory workspaces/SQLite/artifacts.
It only reuses Graphoratory's graph model/generator and the bundled HEG scorer.

This variant follows ONE dedicated coherent lineage objective per run. For
component lineages (c4/c8/c16/...) the beam first preserves the smallest
absolute target count, which is equivalent to the smallest cumulative target
growth because the experiment is pure addition from one common root. The
current-step ΔCk is the next tie-breaker. For sum/weighted lineages the beam
optimises the corresponding marginal delta directly. The final report
reconstructs one actual root-to-leader ancestry, rather than stitching
independent per-order minima together.

Run from the Graphoratory repository/environment, e.g.:

    uv run python scripts/heg_growth_beam.py \
      --start-order 16 \
      --steps 40 \
      --beam-width 16 \
      --step-seconds 60 \
      --total-seconds 600 \
      --workers 16

The bundled HEG scorer currently supports order <= 128.
"""

from __future__ import annotations

import argparse
import atexit
import itertools
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
DEFAULT_WITNESS_CAP = 250_000
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
    lower: int
    upper: int
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
    def lower_total(self) -> int:
        return sum(c.lower for c in self.components)

    @property
    def upper_total(self) -> int:
        return sum(c.upper for c in self.components)

    @property
    def lower_weighted(self) -> int:
        return sum(weight(c.length) * c.lower for c in self.components)

    @property
    def upper_weighted(self) -> int:
        return sum(weight(c.length) * c.upper for c in self.components)

    @property
    def non_exact_components(self) -> int:
        return sum(not c.exact for c in self.components)

    def component_map(self) -> dict[int, ComponentScore]:
        return {c.length: c for c in self.components}


@dataclass(frozen=True, slots=True)
class BeamState:
    score: GraphScore
    parent_hash: str | None
    attachment: tuple[int, int, int] | None
    parent_score: GraphScore | None
    parent_state: BeamState | None = None


def weight(length: int) -> int:
    # Same short-cycle weighting used by the current Graphoratory evaluator.
    return max(1, 64 // length)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Grow HEG graphs one vertex at a time with beam search over "
            "3-neighbour attachments."
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
        help="Number of order increments. Example 16 -> 20 means --steps 4.",
    )

    parser.add_argument("--beam-width", type=int, default=16)
    parser.add_argument(
        "--lineage",
        choices=("c4", "c8", "c16", "c32", "c64", "sum", "weighted"),
        default="c4",
        help=(
            "Dedicated coherent lineage objective. c4/c8/c16/... preserve the "
            "smallest cumulative growth of that cycle count and then its current "
            "ΔCk. sum minimises current ΣΔCk; weighted minimises the current "
            "short-cycle-weighted delta."
        ),
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=60.0,
        help="Wall-clock candidate-search budget per order increment; 0 = unlimited.",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=600.0,
        help="Whole-run wall-clock budget; 0 = unlimited.",
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--max-children-per-parent",
        type=int,
        default=0,
        help=(
            "Optional sampled attachment limit for each beam parent at one order; "
            "0 = enumerate all C(n,3) attachments."
        ),
    )
    parser.add_argument(
        "--inflight-per-worker",
        type=int,
        default=1,
        help="Maximum queued/running scoring jobs per worker.",
    )
    parser.add_argument("--seed", type=int, default=4001)
    parser.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET)
    parser.add_argument(
        "--witness-cap",
        type=int,
        default=DEFAULT_WITNESS_CAP,
        help=(
            "Emergency per-length cap. Budget-exhausted results are represented "
            "as [observed, cap] and ranked conservatively."
        ),
    )
    parser.add_argument(
        "--save-final",
        type=Path,
        default=None,
        help="Optional JSON path for the final beam leader.",
    )
    parser.add_argument(
        "--save-lineage",
        type=Path,
        default=None,
        help="Optional JSON path containing the complete final root-to-leader lineage.",
    )
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
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.step_seconds < 0 or args.total_seconds < 0:
        parser.error("time budgets must be >= 0")
    if args.max_children_per_parent < 0:
        parser.error("--max-children-per-parent must be >= 0")

    if args.target_order is None and args.steps is None:
        args.steps = 20

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

    if args.lineage.startswith("c"):
        target_length = int(args.lineage[1:])
        if args.target_order < target_length:
            parser.error(
                f"--lineage {args.lineage} never becomes active before "
                f"target order {args.target_order}"
            )

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


def attach_new_vertex(graph: Graph, attachment: tuple[int, int, int]) -> Graph:
    new_vertex = graph.order
    edges = list(graph.edges)
    edges.extend((vertex, new_vertex) for vertex in attachment)
    candidate = Graph.from_edges(graph.order + 1, edges)
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
            observed = lower = upper = witness_cap
            status = STATUS_SATURATED
        elif bool(result.complete):
            observed = lower = upper = raw_count
            status = STATUS_EXACT
        else:
            observed = lower = raw_count
            upper = witness_cap
            status = STATUS_BUDGET

        components.append(
            ComponentScore(
                length=length,
                observed=observed,
                lower=lower,
                upper=upper,
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


def evaluate_child(
    parent: BeamState,
    attachment: tuple[int, int, int],
    witness_cap: int,
    node_budget: int,
) -> BeamState:
    candidate = attach_new_vertex(parent.score.graph, attachment)
    score = score_graph(
        candidate,
        witness_cap=witness_cap,
        node_budget=node_budget,
    )
    return BeamState(
        score=score,
        parent_hash=parent.score.graph.graph_hash,
        attachment=attachment,
        parent_score=parent.score,
        parent_state=parent,
    )


def component_upper(state: BeamState, length: int) -> int:
    component = state.score.component_map().get(length)
    return 0 if component is None else component.upper


def component_lower(state: BeamState, length: int) -> int:
    component = state.score.component_map().get(length)
    return 0 if component is None else component.lower


def _parent_component(state: BeamState, length: int) -> ComponentScore | None:
    if state.parent_score is None:
        return None
    return state.parent_score.component_map().get(length)


def _true_count_lower(component: ComponentScore) -> int:
    # EXACT: true count == observed.
    # SATURATED: true count >= cap.
    # BUDGET: true count >= observed.
    return component.observed


def delta_bounds(state: BeamState, length: int) -> tuple[int, int | None]:
    """
    Sound bounds for the *true* marginal cycle count:

        ΔC_length = C_length(child) - C_length(parent)

    upper=None means that no finite upper bound is proved.

    Important: SATURATED_AT_CAP and SEARCH_BUDGET_EXHAUSTED do NOT provide
    finite upper bounds for the true number of cycles. The scorer's cap bounds
    only the capped objective, not the real C_length.

    Because this experiment only adds a vertex/edges and never removes old
    structure, true cycle counts are monotone: child >= parent.
    """
    child = state.score.component_map().get(length)
    if child is None:
        return (0, 0)

    parent = _parent_component(state, length)

    # A forbidden length that becomes active at this order had exact count 0 in
    # the parent because parent.order < length.
    if parent is None:
        if child.exact:
            return (child.observed, child.observed)
        return (_true_count_lower(child), None)

    parent_lower = _true_count_lower(parent)
    child_lower = _true_count_lower(child)

    # Exact child gives a finite ceiling on the parent by monotonicity.
    if child.exact:
        child_value = child.observed
        if parent_lower > child_value:
            raise RuntimeError(
                f"monotone growth invariant violated for C{length}: "
                f"parent lower bound {parent_lower} exceeds exact child "
                f"count {child_value}"
            )

        if parent.exact:
            parent_value = parent.observed
            if child_value < parent_value:
                raise RuntimeError(
                    f"monotone growth invariant violated for C{length}: "
                    f"{child_value} < {parent_value}"
                )
            delta = child_value - parent_value
            return (delta, delta)

        # Parent is only lower-bounded, but parent <= exact child.
        # Therefore 0 <= delta <= child - parent_lower.
        return (0, child_value - parent_lower)

    # Child is not exact: its true count has no finite upper bound.
    if parent.exact:
        # parent is known exactly, so the child's lower bound yields a genuine
        # lower bound on the delta.
        return (max(0, child_lower - parent.observed), None)

    # Both parent and child are only lower-bounded. They may in reality be equal,
    # or the child may be arbitrarily larger.
    return (0, None)


def delta_exact(state: BeamState, length: int) -> bool:
    lower, upper = delta_bounds(state, length)
    return upper is not None and lower == upper


def delta_non_exact_components(state: BeamState) -> int:
    return sum(
        not delta_exact(state, component.length)
        for component in state.score.components
    )


def delta_total_bounds(state: BeamState) -> tuple[int, int | None]:
    bounds = [
        delta_bounds(state, component.length)
        for component in state.score.components
    ]
    lower = sum(item_lower for item_lower, _ in bounds)
    if any(item_upper is None for _, item_upper in bounds):
        return (lower, None)
    return (lower, sum(int(item_upper) for _, item_upper in bounds))


def delta_weighted_bounds(state: BeamState) -> tuple[int, int | None]:
    bounds = [
        (component.length, delta_bounds(state, component.length))
        for component in state.score.components
    ]
    lower = sum(
        weight(length) * item_lower
        for length, (item_lower, _) in bounds
    )
    if any(item_upper is None for _, (_, item_upper) in bounds):
        return (lower, None)
    return (
        lower,
        sum(
            weight(length) * int(item_upper)
            for length, (_, item_upper) in bounds
        ),
    )


def _bounded_upper_key(upper: int | None) -> tuple[int, int]:
    # Proven finite upper bounds always outrank unknown upper bounds.
    return (1, 0) if upper is None else (0, upper)


def rank_key(state: BeamState, objective: str) -> tuple[int, ...]:
    """
    Conservative marginal-growth ranking.

    A candidate with an unknown true-delta upper bound cannot beat a candidate
    with a proved finite upper bound merely because the scorer hit a cap/budget.
    """
    delta_vector = tuple(
        delta_bounds(state, component.length)
        for component in state.score.components
    )
    delta_total_lower, delta_total_upper = delta_total_bounds(state)
    delta_weighted_lower, delta_weighted_upper = delta_weighted_bounds(state)
    non_exact = delta_non_exact_components(state)

    if objective == "delta-lex":
        flattened: list[int] = []
        for lower, upper in delta_vector:
            unknown, upper_value = _bounded_upper_key(upper)
            flattened.extend((unknown, upper_value, lower))
        return (
            *flattened,
            non_exact,
            *_bounded_upper_key(delta_total_upper),
            delta_total_lower,
            *_bounded_upper_key(delta_weighted_upper),
            delta_weighted_lower,
            state.score.upper_total,
            state.score.upper_weighted,
        )

    if objective == "delta-weighted":
        return (
            *_bounded_upper_key(delta_weighted_upper),
            delta_weighted_lower,
            *_bounded_upper_key(delta_total_upper),
            delta_total_lower,
            non_exact,
            state.score.upper_weighted,
            state.score.upper_total,
        )

    # delta-total and final tie-break for delta-portfolio.
    return (
        *_bounded_upper_key(delta_total_upper),
        delta_total_lower,
        *_bounded_upper_key(delta_weighted_upper),
        delta_weighted_lower,
        non_exact,
        state.score.upper_total,
        state.score.upper_weighted,
    )


def _ranking_for_component(
    states: list[BeamState],
    length: int,
) -> list[BeamState]:
    def key(state: BeamState) -> tuple[int, ...]:
        lower, upper = delta_bounds(state, length)
        return (
            *_bounded_upper_key(upper),
            lower,
            0 if delta_exact(state, length) else 1,
            component_upper(state, length),
            component_lower(state, length),
            state.score.upper_total,
            state.score.upper_weighted,
        )

    return sorted(
        states,
        key=lambda state: (*key(state), state.score.graph.graph_hash),
    )


def select_beam(
    states: list[BeamState],
    *,
    beam_width: int,
    objective: str,
) -> list[BeamState]:
    if len(states) <= beam_width:
        return sorted(
            states,
            key=lambda state: (rank_key(state, objective), state.score.graph.graph_hash),
        )

    if objective != "delta-portfolio":
        return sorted(
            states,
            key=lambda state: (rank_key(state, objective), state.score.graph.graph_hash),
        )[:beam_width]

    lengths = tuple(component.length for component in states[0].score.components)

    def total_key(state: BeamState) -> tuple[int, ...]:
        lower, upper = delta_total_bounds(state)
        return (
            *_bounded_upper_key(upper),
            lower,
            delta_non_exact_components(state),
            state.score.upper_total,
            state.score.upper_weighted,
        )

    def weighted_key(state: BeamState) -> tuple[int, ...]:
        lower, upper = delta_weighted_bounds(state)
        return (
            *_bounded_upper_key(upper),
            lower,
            delta_non_exact_components(state),
            state.score.upper_weighted,
            state.score.upper_total,
        )

    rankings: list[list[BeamState]] = [
        sorted(
            states,
            key=lambda state: (*total_key(state), state.score.graph.graph_hash),
        ),
        sorted(
            states,
            key=lambda state: (*weighted_key(state), state.score.graph.graph_hash),
        ),
    ]
    rankings.extend(_ranking_for_component(states, length) for length in lengths)

    selected: list[BeamState] = []
    selected_hashes: set[str] = set()
    cursors = [0] * len(rankings)

    while len(selected) < beam_width:
        made_progress = False
        for index, ranking in enumerate(rankings):
            cursor = cursors[index]
            while cursor < len(ranking):
                candidate = ranking[cursor]
                cursor += 1
                graph_hash = candidate.score.graph.graph_hash
                if graph_hash in selected_hashes:
                    continue
                selected.append(candidate)
                selected_hashes.add(graph_hash)
                made_progress = True
                break
            cursors[index] = cursor
            if len(selected) >= beam_width:
                break
        if not made_progress:
            break

    if len(selected) < beam_width:
        for candidate in sorted(
            states,
            key=lambda state: (
                rank_key(state, "delta-total"),
                state.score.graph.graph_hash,
            ),
        ):
            graph_hash = candidate.score.graph.graph_hash
            if graph_hash in selected_hashes:
                continue
            selected.append(candidate)
            selected_hashes.add(graph_hash)
            if len(selected) >= beam_width:
                break

    return selected



def lineage_target_length(lineage: str) -> int | None:
    if not lineage.startswith("c"):
        return None
    return int(lineage[1:])


def component_true_count_key(state: BeamState, length: int) -> tuple[int, int, int]:
    """
    Conservative key for the current *true* C_length.

    (0, value, value) means exact finite count.
    (1, lower, lower) means only a lower bound is known; exact finite values are
    always preferred to unknown-upper values.
    """
    component = state.score.component_map().get(length)
    if component is None:
        return (0, 0, 0)
    if component.exact:
        return (0, component.observed, component.observed)
    return (1, _true_count_lower(component), _true_count_lower(component))


def component_delta_key(state: BeamState, length: int) -> tuple[int, ...]:
    lower, upper = delta_bounds(state, length)
    return (
        *_bounded_upper_key(upper),
        lower,
        0 if delta_exact(state, length) else 1,
    )


def total_delta_key(state: BeamState) -> tuple[int, ...]:
    lower, upper = delta_total_bounds(state)
    return (
        *_bounded_upper_key(upper),
        lower,
        delta_non_exact_components(state),
    )


def weighted_delta_key(state: BeamState) -> tuple[int, ...]:
    lower, upper = delta_weighted_bounds(state)
    return (
        *_bounded_upper_key(upper),
        lower,
        delta_non_exact_components(state),
    )


def lineage_rank_key(state: BeamState, lineage: str) -> tuple[int, ...]:
    """
    Rank one coherent beam.

    For a component lineage, all candidates descend from the same root and the
    search is monotone. Therefore minimising absolute Ck at order n is exactly
    minimising cumulative ΔCk from the root. Current-step ΔCk is secondary.
    This prevents a branch that paid a large Ck cost earlier from looking good
    merely because its latest step happened to have ΔCk=0.
    """
    target_length = lineage_target_length(lineage)

    if target_length is not None:
        return (
            *component_true_count_key(state, target_length),
            *component_delta_key(state, target_length),
            *total_delta_key(state),
            *weighted_delta_key(state),
            state.score.upper_total,
            state.score.upper_weighted,
        )

    if lineage == "weighted":
        return (
            *weighted_delta_key(state),
            *total_delta_key(state),
            state.score.upper_weighted,
            state.score.upper_total,
        )

    # lineage == "sum"
    return (
        *total_delta_key(state),
        *weighted_delta_key(state),
        state.score.upper_total,
        state.score.upper_weighted,
    )


def select_lineage_beam(
    states: list[BeamState],
    *,
    beam_width: int,
    lineage: str,
) -> list[BeamState]:
    return sorted(
        states,
        key=lambda state: (
            lineage_rank_key(state, lineage),
            state.score.graph.graph_hash,
        ),
    )[:beam_width]


def lineage_path(state: BeamState) -> list[BeamState]:
    path: list[BeamState] = []
    current: BeamState | None = state
    while current is not None:
        path.append(current)
        current = current.parent_state
    path.reverse()
    return path


def delta_text_for_report(state: BeamState, length: int) -> str:
    lower, upper = delta_bounds(state, length)
    if upper is None:
        return f"[+{lower},?]"
    if lower == upper:
        return f"+{lower}"
    return f"[+{lower},+{upper}]"


def total_delta_text_for_report(state: BeamState) -> str:
    lower, upper = delta_total_bounds(state)
    if upper is None:
        return f"[+{lower},?]"
    if lower == upper:
        return f"+{lower}"
    return f"[+{lower},+{upper}]"


def print_final_lineage(final_state: BeamState, lineage: str) -> None:
    path = lineage_path(final_state)
    final_order = final_state.score.graph.order
    lengths = tuple(int(length) for length in forbidden_lengths(final_order))
    target_length = lineage_target_length(lineage)

    console.print()
    console.print(
        f"[bold]FINAL COHERENT LINEAGE[/bold] target={lineage} "
        f"steps={len(path) - 1} order={path[0].score.graph.order}->{final_order}"
    )

    cycle_headers = " ".join(
        f"{('ΔC'+str(length)+' [C]'):>19}"
        for length in lengths
    )
    console.print(
        f"{'N':>3} {cycle_headers} {'ΔSUM':>12} {'ATTACH':>15} {'HASH':>8} STATE"
    )

    for index, state in enumerate(path):
        cells: list[str] = []
        component_map = state.score.component_map()
        for length in lengths:
            component = component_map.get(length)
            if component is None:
                cells.append(f"{'-':>19}")
                continue
            absolute = compact_value(component)
            if index == 0:
                value = f"- [{absolute}]"
            else:
                value = f"{delta_text_for_report(state, length)} [{absolute}]"
            cells.append(f"{value:>19}")

        delta_sum = "-" if index == 0 else total_delta_text_for_report(state)
        attachment = "-" if state.attachment is None else str(state.attachment)
        console.print(
            f"{state.score.graph.order:>3} "
            + " ".join(cells)
            + f" {delta_sum:>12} {attachment:>15} "
            f"{state.score.graph.graph_hash[:8]:>8} {score_status(state.score)}"
        )

    if target_length is not None:
        active_steps = [
            state
            for state in path[1:]
            if state.score.graph.order >= target_length
        ]
        zero_prefix = 0
        exact_zero_steps = 0
        all_exact = True

        for state in active_steps:
            lower, upper = delta_bounds(state, target_length)
            exact = upper is not None and lower == upper
            if exact and lower == 0:
                exact_zero_steps += 1
            else:
                all_exact = all_exact and exact

        for state in active_steps:
            lower, upper = delta_bounds(state, target_length)
            if upper is not None and lower == upper == 0:
                zero_prefix += 1
            else:
                break

        root_component = path[0].score.component_map().get(target_length)
        final_component = final_state.score.component_map().get(target_length)
        root_count = (
            0 if root_component is None else
            root_component.observed if root_component.exact else None
        )
        final_count = (
            0 if final_component is None else
            final_component.observed if final_component.exact else None
        )

        growth_text = "unknown"
        if root_count is not None and final_count is not None:
            growth_text = f"+{final_count - root_count}"

        console.print(
            f"Lineage summary: C{target_length} zero-prefix={zero_prefix}/"
            f"{len(active_steps)}, exact-zero-steps={exact_zero_steps}/"
            f"{len(active_steps)}, cumulative-growth={growth_text}"
        )


def save_lineage(path: Path, final_state: BeamState, lineage: str) -> None:
    states = lineage_path(final_state)
    payload = {
        "schema_version": "graphoratory.heg_growth_lineage.v1",
        "lineage": lineage,
        "root_order": states[0].score.graph.order,
        "final_order": states[-1].score.graph.order,
        "states": [],
    }

    for index, state in enumerate(states):
        components = state.score.component_map()
        record = {
            "order": state.score.graph.order,
            "graph_hash": state.score.graph.graph_hash,
            "parent_hash": state.parent_hash,
            "attachment": (
                list(state.attachment) if state.attachment is not None else None
            ),
            "components": {
                str(length): {
                    "observed": component.observed,
                    "lower": component.lower,
                    "upper": component.upper,
                    "status": component.status,
                    "delta_lower": (
                        None if index == 0 else delta_bounds(state, length)[0]
                    ),
                    "delta_upper": (
                        None if index == 0 else delta_bounds(state, length)[1]
                    ),
                }
                for length, component in sorted(components.items())
            },
            "delta_total": (
                None
                if index == 0
                else {
                    "lower": delta_total_bounds(state)[0],
                    "upper": delta_total_bounds(state)[1],
                }
            ),
        }
        payload["states"].append(record)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\\n",
        encoding="utf-8",
    )

def candidate_attachments(
    order: int,
    *,
    seed: int,
    limit: int,
) -> list[tuple[int, int, int]]:
    attachments = list(itertools.combinations(range(order), 3))
    rng = random.Random((seed << 32) ^ order ^ 0x9E3779B97F4A7C15)
    rng.shuffle(attachments)
    if limit:
        del attachments[limit:]
    return attachments


def interleaved_candidates(
    beam: list[BeamState],
    *,
    step: int,
    seed: int,
    max_children_per_parent: int,
) -> Iterator[tuple[BeamState, tuple[int, int, int]]]:
    per_parent = [
        candidate_attachments(
            state.score.graph.order,
            seed=seed ^ step ^ int(state.score.graph.graph_hash[:16], 16),
            limit=max_children_per_parent,
        )
        for state in beam
    ]

    maximum = max((len(items) for items in per_parent), default=0)
    for child_index in range(maximum):
        for parent, items in zip(beam, per_parent, strict=True):
            if child_index < len(items):
                yield parent, items[child_index]


def candidate_space_size(
    beam: list[BeamState],
    *,
    max_children_per_parent: int,
) -> int:
    total = 0
    for state in beam:
        space = math.comb(state.score.graph.order, 3)
        total += min(space, max_children_per_parent) if max_children_per_parent else space
    return total


def score_status(score: GraphScore) -> str:
    if score.fully_exact:
        return "OK"
    sat = [f"C{c.length}" for c in score.components if c.status == STATUS_SATURATED]
    budget = [f"C{c.length}" for c in score.components if c.status == STATUS_BUDGET]
    parts: list[str] = []
    if sat:
        parts.append("CAP:" + ",".join(sat))
    if budget:
        parts.append("BUD:" + ",".join(budget))
    return " ".join(parts)


def compact_value(component: ComponentScore | None) -> str:
    if component is None:
        return "-"
    if component.status == STATUS_EXACT:
        return str(component.observed)
    if component.status == STATUS_SATURATED:
        return f">={component.observed}"
    return f"[{component.lower},{component.upper}]"


class Ledger:
    def __init__(self) -> None:
        self._active_lengths: tuple[int, ...] | None = None

    @staticmethod
    def _delta_text(state: BeamState, length: int) -> str:
        lower, upper = delta_bounds(state, length)
        if upper is None:
            return f"[+{lower},?]"
        if lower == upper:
            return f"+{lower}"
        return f"[+{lower},+{upper}]"

    @staticmethod
    def _leader_cell(state: BeamState, length: int) -> str:
        component = state.score.component_map().get(length)
        if component is None:
            return "-"
        delta = Ledger._delta_text(state, length)
        absolute = compact_value(component)
        return f"{delta} [{absolute}]"

    @staticmethod
    def _root_cell(state: BeamState, length: int) -> str:
        component = state.score.component_map().get(length)
        if component is None:
            return "-"
        return f"- [{compact_value(component)}]"

    @staticmethod
    def _best_for_delta(
        states: list[BeamState],
        length: int,
    ) -> BeamState:
        def key(state: BeamState) -> tuple[int, ...]:
            lower, upper = delta_bounds(state, length)
            return (
                *_bounded_upper_key(upper),
                lower,
                0 if delta_exact(state, length) else 1,
                component_upper(state, length),
                component_lower(state, length),
                state.score.upper_total,
                state.score.upper_weighted,
            )

        return min(
            states,
            key=lambda state: (*key(state), state.score.graph.graph_hash),
        )

    @staticmethod
    def _min_delta_cell(
        states: list[BeamState],
        length: int,
    ) -> str:
        best = Ledger._best_for_delta(states, length)
        return Ledger._delta_text(best, length)

    def _print_header(self, lengths: tuple[int, ...]) -> None:
        cycle_headers = " ".join(
            f"{('ΔC'+str(length)+' [C]'):>19}"
            for length in lengths
        )
        console.print(
            f"{'STEP':>4} {'N':>3} {'TYPE':>7} {'SCORED/SPACE':>15} "
            f"{'BEAM':>4} {cycle_headers} {'ΔSUM':>12} {'TIME':>8}  STATE"
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
        lengths = tuple(
            int(length)
            for length in forbidden_lengths(leader.score.graph.order)
        )

        if lengths != self._active_lengths:
            if self._active_lengths is not None:
                console.print()
            self._print_header(lengths)
            self._active_lengths = lengths

        coverage_text = (
            "-"
            if scored is None or space is None
            else f"{scored}/{space}"
        )

        if step == 0:
            leader_cells = [
                f"{self._root_cell(leader, length):>19}"
                for length in lengths
            ]
            delta_sum_text = "-"
        else:
            leader_cells = [
                f"{self._leader_cell(leader, length):>19}"
                for length in lengths
            ]
            delta_sum_lower, delta_sum_upper = delta_total_bounds(leader)
            if delta_sum_upper is None:
                delta_sum_text = f"[+{delta_sum_lower},?]"
            elif delta_sum_lower == delta_sum_upper:
                delta_sum_text = f"+{delta_sum_lower}"
            else:
                delta_sum_text = f"[+{delta_sum_lower},+{delta_sum_upper}]"

        console.print(
            f"{step:>4} {leader.score.graph.order:>3} {'LEADER':>7} "
            f"{coverage_text:>15} {beam_size:>4} "
            + " ".join(leader_cells)
            + f" {delta_sum_text:>12} {elapsed:>7.2f}s  "
            f"{score_status(leader.score)}"
        )

        if pool and scored is not None and space is not None:
            exhaustive = scored >= space
            min_label = "MIN" if exhaustive else "MIN*"

            min_cells = [
                f"{self._min_delta_cell(pool, length):>19}"
                for length in lengths
            ]

            def total_key(state: BeamState) -> tuple[int, ...]:
                lower, upper = delta_total_bounds(state)
                return (
                    *_bounded_upper_key(upper),
                    lower,
                    delta_non_exact_components(state),
                    state.score.upper_total,
                    state.score.upper_weighted,
                )

            best_sum = min(
                pool,
                key=lambda state: (*total_key(state), state.score.graph.graph_hash),
            )
            min_sum_lower, min_sum_upper = delta_total_bounds(best_sum)
            if min_sum_upper is None:
                min_sum_text = f"[+{min_sum_lower},?]"
            elif min_sum_lower == min_sum_upper:
                min_sum_text = f"+{min_sum_lower}"
            else:
                min_sum_text = f"[+{min_sum_lower},+{min_sum_upper}]"

            note = (
                "independent per-cycle minima"
                if exhaustive
                else "* minima among scored candidates only"
            )

            console.print(
                f"{'':>4} {'':>3} {min_label:>7} "
                f"{'':>15} {'':>4} "
                + " ".join(min_cells)
                + f" {min_sum_text:>12} {'':>8}  {note}"
            )

        if leader.attachment is not None:
            console.print(
                f"     leader attach={leader.attachment} "
                f"hash={leader.score.graph.graph_hash[:8]} "
                f"parent={leader.parent_hash[:8] if leader.parent_hash else '-'}"
            )


def save_graph(path: Path, state: BeamState) -> None:
    payload = {
        **state.score.graph.record(),
        "parent_hash": state.parent_hash,
        "attachment": list(state.attachment) if state.attachment is not None else None,
        "forbidden_cycle_score": {
            str(component.length): {
                "observed": component.observed,
                "lower": component.lower,
                "upper": component.upper,
                "status": component.status,
            }
            for component in state.score.components
        },
        "forbidden_cycle_delta": {
            str(component.length): {
                "lower": delta_bounds(state, component.length)[0],
                "upper": delta_bounds(state, component.length)[1],
                "exact": delta_exact(state, component.length),
            }
            for component in state.score.components
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def maybe_save_hit(args: argparse.Namespace, state: BeamState) -> Path | None:
    if not state.score.fully_exact or state.score.lower_total != 0:
        return None
    args.hit_dir.mkdir(parents=True, exist_ok=True)
    path = args.hit_dir / (
        f"heg-zero-order-{state.score.graph.order}-"
        f"{state.score.graph.graph_hash[:8]}.json"
    )
    save_graph(path, state)
    return path


def main() -> int:
    args = parse_args()
    start_graph = load_or_generate_start_graph(args)

    console.print(
        f"[bold]HEG coherent-lineage beam probe[/bold] "
        f"start={args.start_order} target={args.target_order} "
        f"beam={args.beam_width} workers={args.workers} lineage={args.lineage}"
    )
    console.print(
        f"step_budget={args.step_seconds or 'unlimited'}s "
        f"total_budget={args.total_seconds or 'unlimited'}s "
        f"node_budget={args.node_budget:,} cap={args.witness_cap:,}"
    )
    console.print(
        "[dim]One dedicated beam is preserved across orders. Component lineages "
        "minimise cumulative target growth first, then current ΔCk; the final "
        "report reconstructs one actual ancestry.[/dim]"
    )

    started = time.perf_counter()
    total_deadline = (
        started + args.total_seconds if args.total_seconds > 0 else math.inf
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
                parent_hash=None,
                attachment=None,
                parent_score=None,
                parent_state=None,
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

        hit = maybe_save_hit(args, beam[0])
        if hit is not None:
            console.print(f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] saved: {hit}")
            return 0

        for step in range(1, args.target_order - args.start_order + 1):
            now = time.perf_counter()
            if now >= total_deadline:
                console.print("[yellow]Total time budget exhausted.[/yellow]")
                break

            step_started = now
            step_deadline = min(
                total_deadline,
                step_started + args.step_seconds if args.step_seconds > 0 else math.inf,
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

            seen_graph_hashes: set[str] = set()
            completed_states: list[BeamState] = []
            inflight: dict[Future[BeamState], str] = {}
            tried = 0
            exhausted_stream = False

            def submit_until_full() -> None:
                nonlocal exhausted_stream, tried
                while (
                    not exhausted_stream
                    and len(inflight) < max_inflight
                    and time.perf_counter() < step_deadline
                ):
                    try:
                        parent, attachment = next(stream)
                    except StopIteration:
                        exhausted_stream = True
                        break

                    candidate = attach_new_vertex(parent.score.graph, attachment)
                    graph_hash = candidate.graph_hash
                    if graph_hash in seen_graph_hashes:
                        continue
                    seen_graph_hashes.add(graph_hash)

                    # Submit the already materialized candidate graph through a small
                    # wrapper state so graph construction/hash dedupe stays in the
                    # coordinator while expensive scoring stays parallel.
                    future = executor.submit(
                        score_graph,
                        candidate,
                        witness_cap=args.witness_cap,
                        node_budget=args.node_budget,
                    )
                    # encode parent/attachment lookup in a side dict keyed by hash
                    metadata[graph_hash] = (parent, attachment)
                    inflight[future] = graph_hash
                    tried += 1

            metadata: dict[str, tuple[BeamState, tuple[int, int, int]]] = {}
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
                    parent, attachment = metadata.pop(graph_hash)
                    score = future.result()
                    completed_states.append(
                        BeamState(
                            score=score,
                            parent_hash=parent.score.graph.graph_hash,
                            attachment=attachment,
                            parent_score=parent.score,
                            parent_state=parent,
                        )
                    )
                submit_until_full()

            # Cleanly finish the level. Futures that have not started are
            # cancelled; already-running scorer calls are drained so work from an
            # old order never leaks into the next order. This can overrun the
            # nominal step budget by at most one in-flight scorer call per worker.
            running_tail: list[Future[GraphScore]] = []
            for future in list(inflight):
                if not future.cancel():
                    running_tail.append(future)

            if running_tail:
                done_tail, _ = wait(tuple(running_tail))
                for future in done_tail:
                    graph_hash = inflight.pop(future)
                    parent, attachment = metadata.pop(graph_hash)
                    score = future.result()
                    completed_states.append(
                        BeamState(
                            score=score,
                            parent_hash=parent.score.graph.graph_hash,
                            attachment=attachment,
                            parent_score=parent.score,
                            parent_state=parent,
                        )
                    )

            if not completed_states:
                console.print(
                    f"[yellow]Step {step}: no candidate score completed before "
                    "the step budget.[/yellow]"
                )
                break

            unique_states = {
                state.score.graph.graph_hash: state for state in completed_states
            }
            pool = list(unique_states.values())

            beam = select_lineage_beam(
                pool,
                beam_width=args.beam_width,
                lineage=args.lineage,
            )
            leader = min(
                beam,
                key=lambda state: (
                    lineage_rank_key(state, args.lineage),
                    state.score.graph.graph_hash,
                ),
            )

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
                    f"     scored {len(pool)}/{full_space} candidate attachments "
                    "before the step deadline"
                )
                if tried != len(pool):
                    console.print(
                        f"     submitted={tried}; "
                        f"{tried - len(pool)} did not contribute a completed unique score"
                    )

            hits = [
                state
                for state in beam
                if state.score.fully_exact and state.score.lower_total == 0
            ]
            if hits:
                hit_state = min(hits, key=lambda s: s.score.graph.graph_hash)
                path = maybe_save_hit(args, hit_state)
                console.print(
                    f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] "
                    f"order={hit_state.score.graph.order} saved: {path}"
                )
                return 0

    # Pick the final leader of this dedicated coherent lineage beam.
    final_leader = min(
        beam,
        key=lambda state: (
            lineage_rank_key(state, args.lineage),
            state.score.graph.graph_hash,
        ),
    )

    if args.save_final is not None:
        save_graph(args.save_final, final_leader)
        console.print(f"Final leader saved: {args.save_final}")

    if args.save_lineage is not None:
        save_lineage(args.save_lineage, final_leader, args.lineage)
        console.print(f"Final lineage saved: {args.save_lineage}")

    print_final_lineage(final_leader, args.lineage)

    final_delta_lower, final_delta_upper = delta_total_bounds(final_leader)
    if final_delta_upper is None:
        final_delta_text = f"[+{final_delta_lower},?]"
    elif final_delta_lower == final_delta_upper:
        final_delta_text = f"+{final_delta_lower}"
    else:
        final_delta_text = f"[+{final_delta_lower},+{final_delta_upper}]"
    console.print(
        f"[bold]Done[/bold] lineage={args.lineage} order={final_leader.score.graph.order} "
        f"beam={len(beam)} last_delta_sum={final_delta_text} "
        f"absolute_total_upper={final_leader.score.upper_total} "
        f"elapsed={time.perf_counter() - started:.2f}s "
        f"hash={final_leader.score.graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
