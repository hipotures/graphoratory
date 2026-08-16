#!/usr/bin/env python3
"""
HEG delta-beam growth probe.

A small in-memory experiment for the Erdős–Gyárfás search:

- start from one valid minimum-degree-3 graph,
- increase the order by exactly one at every growth step,
- attach the new vertex to exactly three existing vertices,
- evaluate many children in parallel with the existing HEG score worker,
- retain a beam of several promising graph states instead of one greedy path.

The script intentionally does NOT use Graphoratory workspaces/SQLite/artifacts.
It only reuses Graphoratory's graph model/generator and the bundled HEG scorer.

Default selection mode is "delta-portfolio": the next beam mixes candidates
that minimise the *increment* caused by one order increase: min ΔC4, min ΔC8,
min ΔC16, ..., min ΣΔCk, and min weighted ΣΔCk. Absolute cycle counts are used
only as tie-breakers. This directly measures the marginal forbidden-cycle cost
of growing the graph by one vertex.

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
        "--objective",
        choices=("delta-portfolio", "delta-total", "delta-weighted", "delta-lex"),
        default="delta-portfolio",
        help=(
            "delta-portfolio keeps specialists for min ΣΔCk, weighted ΣΔCk, "
            "and min ΔC4, ΔC8, ΔC16, ...; delta-total minimises ΣΔCk; "
            "delta-weighted emphasises short-cycle increments; delta-lex "
            "minimises ΔC4 then ΔC8 then ΔC16, ..."
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


def delta_interval(state: BeamState, length: int) -> tuple[int, int]:
    """
    Conservative interval for ΔC_length = C_length(child) - C_length(parent).

    For a newly activated forbidden length (e.g. C16 at order 16), the parent
    has no such component and is treated as exact zero.

    Pure addition cannot destroy existing cycles, so the true delta is >= 0.
    We clamp the conservative lower bound accordingly.
    """
    child = state.score.component_map().get(length)
    if child is None:
        return (0, 0)

    parent = _parent_component(state, length)
    if parent is None:
        parent_lower = parent_upper = 0
    else:
        parent_lower = parent.lower
        parent_upper = parent.upper

    lower = max(0, child.lower - parent_upper)
    upper = max(0, child.upper - parent_lower)
    return (lower, upper)


def delta_exact(state: BeamState, length: int) -> bool:
    child = state.score.component_map().get(length)
    if child is None:
        return True
    parent = _parent_component(state, length)
    return child.exact and (parent is None or parent.exact)


def delta_non_exact_components(state: BeamState) -> int:
    return sum(
        not delta_exact(state, component.length)
        for component in state.score.components
    )


def delta_total_interval(state: BeamState) -> tuple[int, int]:
    intervals = [delta_interval(state, component.length) for component in state.score.components]
    return (
        sum(lower for lower, _ in intervals),
        sum(upper for _, upper in intervals),
    )


def delta_weighted_interval(state: BeamState) -> tuple[int, int]:
    intervals = [
        (component.length, delta_interval(state, component.length))
        for component in state.score.components
    ]
    return (
        sum(weight(length) * lower for length, (lower, _) in intervals),
        sum(weight(length) * upper for length, (_, upper) in intervals),
    )


def rank_key(state: BeamState, objective: str) -> tuple[int, ...]:
    """
    Rank by marginal growth cost, not by absolute cycle counts.

    Inexact deltas are ranked conservatively by their upper bound first, so a
    budget-limited child is never rewarded merely because only a small partial
    count was observed.
    """
    delta_vector = tuple(
        delta_interval(state, component.length)
        for component in state.score.components
    )
    upper_vector = tuple(upper for _, upper in delta_vector)
    lower_vector = tuple(lower for lower, _ in delta_vector)
    delta_total_lower, delta_total_upper = delta_total_interval(state)
    delta_weighted_lower, delta_weighted_upper = delta_weighted_interval(state)
    non_exact = delta_non_exact_components(state)

    if objective == "delta-lex":
        return (
            *upper_vector,
            non_exact,
            *lower_vector,
            delta_total_upper,
            delta_weighted_upper,
            state.score.upper_total,
            state.score.upper_weighted,
        )

    if objective == "delta-weighted":
        return (
            delta_weighted_upper,
            delta_total_upper,
            non_exact,
            *upper_vector,
            delta_weighted_lower,
            delta_total_lower,
            state.score.upper_weighted,
            state.score.upper_total,
        )

    # delta-total and final tie-break for delta-portfolio.
    return (
        delta_total_upper,
        delta_weighted_upper,
        non_exact,
        *upper_vector,
        delta_total_lower,
        delta_weighted_lower,
        state.score.upper_total,
        state.score.upper_weighted,
    )


def _ranking_for_component(
    states: list[BeamState],
    length: int,
) -> list[BeamState]:
    return sorted(
        states,
        key=lambda state: (
            delta_interval(state, length)[1],
            not delta_exact(state, length),
            delta_interval(state, length)[0],
            state.score.upper_total,
            state.score.upper_weighted,
            state.score.graph.graph_hash,
        ),
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

    rankings: list[list[BeamState]] = [
        # Specialist: smallest total marginal cost ΣΔCk.
        sorted(
            states,
            key=lambda state: (
                delta_total_interval(state)[1],
                delta_non_exact_components(state),
                delta_total_interval(state)[0],
                state.score.upper_total,
                state.score.upper_weighted,
                state.score.graph.graph_hash,
            ),
        ),
        # Specialist: smallest short-cycle-weighted marginal cost.
        sorted(
            states,
            key=lambda state: (
                delta_weighted_interval(state)[1],
                delta_non_exact_components(state),
                delta_weighted_interval(state)[0],
                state.score.upper_weighted,
                state.score.upper_total,
                state.score.graph.graph_hash,
            ),
        ),
    ]
    # Specialists: min ΔC4, min ΔC8, min ΔC16, ...
    rankings.extend(_ranking_for_component(states, length) for length in lengths)

    # Round-robin keeps several distinct marginal-growth strategies alive.
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
        lower, upper = delta_interval(state, length)
        if delta_exact(state, length):
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
    def _best_for_delta(
        states: list[BeamState],
        length: int,
    ) -> BeamState:
        return min(
            states,
            key=lambda state: (
                delta_interval(state, length)[1],
                not delta_exact(state, length),
                delta_interval(state, length)[0],
                component_upper(state, length),
                component_lower(state, length),
                state.score.upper_total,
                state.score.upper_weighted,
                state.score.graph.graph_hash,
            ),
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
            f"{'STEP':>4} {'N':>3} {'TYPE':>7} {'TRIED':>9} {'UNIQ':>9} "
            f"{'BEAM':>4} {cycle_headers} {'ΔSUM':>10} {'TIME':>8}  STATE"
        )

    def print_step(
        self,
        *,
        step: int,
        leader: BeamState,
        pool: list[BeamState] | None,
        tried: int | None,
        unique: int | None,
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

        tried_text = "-" if tried is None else str(tried)
        unique_text = "-" if unique is None else str(unique)

        leader_cells = [
            f"{self._leader_cell(leader, length):>19}"
            for length in lengths
        ]
        delta_sum_lower, delta_sum_upper = delta_total_interval(leader)
        leader_delta_sum = (
            f"+{delta_sum_lower}"
            if delta_sum_lower == delta_sum_upper
            else f"[+{delta_sum_lower},+{delta_sum_upper}]"
        )

        console.print(
            f"{step:>4} {leader.score.graph.order:>3} {'LEADER':>7} "
            f"{tried_text:>9} {unique_text:>9} {beam_size:>4} "
            + " ".join(leader_cells)
            + f" {leader_delta_sum:>10} {elapsed:>7.2f}s  "
            f"{score_status(leader.score)}"
        )

        if pool:
            min_cells = [
                f"{self._min_delta_cell(pool, length):>19}"
                for length in lengths
            ]

            best_sum = min(
                pool,
                key=lambda state: (
                    delta_total_interval(state)[1],
                    delta_non_exact_components(state),
                    delta_total_interval(state)[0],
                    state.score.upper_total,
                    state.score.upper_weighted,
                    state.score.graph.graph_hash,
                ),
            )
            min_sum_lower, min_sum_upper = delta_total_interval(best_sum)
            min_sum_text = (
                f"+{min_sum_lower}"
                if min_sum_lower == min_sum_upper
                else f"[+{min_sum_lower},+{min_sum_upper}]"
            )

            console.print(
                f"{'':>4} {'':>3} {'MIN':>7} "
                f"{'':>9} {'':>9} {'':>4} "
                + " ".join(min_cells)
                + f" {min_sum_text:>10} {'':>8}  "
                "independent per-cycle minima"
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
                "lower": delta_interval(state, component.length)[0],
                "upper": delta_interval(state, component.length)[1],
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
        f"[bold]HEG delta-beam growth probe[/bold] "
        f"start={args.start_order} target={args.target_order} "
        f"beam={args.beam_width} workers={args.workers} objective={args.objective}"
    )
    console.print(
        f"step_budget={args.step_seconds or 'unlimited'}s "
        f"total_budget={args.total_seconds or 'unlimited'}s "
        f"node_budget={args.node_budget:,} cap={args.witness_cap:,}"
    )
    console.print(
        "[dim]Selection is based on marginal growth cost ΔCk. Absolute Ck values "
        "are retained only as tie-breakers and for reporting.[/dim]"
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
            )
        ]
        ledger.print_step(
            step=0,
            leader=beam[0],
            pool=None,
            tried=None,
            unique=None,
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

            beam = select_beam(
                pool,
                beam_width=args.beam_width,
                objective=args.objective,
            )
            leader = min(
                beam,
                key=lambda state: (
                    rank_key(
                        state,
                        "delta-total" if args.objective == "delta-portfolio" else args.objective,
                    ),
                    state.score.graph.graph_hash,
                ),
            )

            elapsed = time.perf_counter() - step_started
            ledger.print_step(
                step=step,
                leader=leader,
                pool=pool,
                tried=tried,
                unique=len(pool),
                beam_size=len(beam),
                elapsed=elapsed,
            )

            inexact = sum(not state.score.fully_exact for state in beam)
            if inexact:
                console.print(
                    f"     beam health: {inexact}/{len(beam)} states contain "
                    "cap/budget-limited components"
                )

            if tried < full_space:
                console.print(
                    f"     sampled {tried}/{full_space} candidate attachments "
                    "before the step deadline"
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

    # Pick the final global leader according to the requested objective.
    final_leader = min(
        beam,
        key=lambda state: (
            rank_key(
                state,
                "delta-total" if args.objective == "delta-portfolio" else args.objective,
            ),
            state.score.graph.graph_hash,
        ),
    )

    if args.save_final is not None:
        save_graph(args.save_final, final_leader)
        console.print(f"Final leader saved: {args.save_final}")

    final_delta_lower, final_delta_upper = delta_total_interval(final_leader)
    final_delta_text = (
        f"+{final_delta_lower}"
        if final_delta_lower == final_delta_upper
        else f"[+{final_delta_lower},+{final_delta_upper}]"
    )
    console.print(
        f"[bold]Done[/bold] order={final_leader.score.graph.order} "
        f"beam={len(beam)} last_delta_sum={final_delta_text} "
        f"absolute_total_upper={final_leader.score.upper_total} "
        f"elapsed={time.perf_counter() - started:.2f}s "
        f"hash={final_leader.score.graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
