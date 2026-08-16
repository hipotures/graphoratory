#!/usr/bin/env python3
"""
HEG fixed-order polishing probe.

Goal
----
Take a promising fixed-order graph and try to eliminate the remaining forbidden
cycles using degree-preserving 2-switches:

    remove: (a,b), (c,d)
    add:    (a,c), (b,d)

or:

    add:    (a,d), (b,c)

The graph order, edge count, and every vertex degree are preserved. Candidate
graphs are still validated as simple, connected, and minimum-degree >= 3.

This is deliberately different from the growth probes: Δn = 0. We are now
trying to polish one promising graph all the way to

    C4 = C8 = C16 = ... = 0.

Default start
-------------
The default --preset rewire2-n26 reconstructs the exact n=26 state reported by
the strong-rewiring experiment:

    C4=7, C8=1, C16=0, TOTAL=8
    expected graph hash prefix: 5ba52272

It regenerates the deterministic order-16 root (seed 4001) and replays the
published mutation lineage through n=26. The script verifies the expected hash
prefix before starting the polish search.

You may instead provide:

    --start-graph PATH

with JSON containing at least {"order": ..., "edges": [[u,v], ...]}.

Search semantics
----------------
At each fixed-order depth:
- expand every graph in the current beam by legal 2-switches,
- deduplicate graphs by cryptographic graph hash,
- evaluate every scored candidate with the existing HEG ScoreWorker,
- keep only exact-scored candidates,
- reserve most beam slots for the best absolute states,
- reserve --escape-lanes slots for states above the current global best but no
  higher than global_best + --escape-height,
- keep those escape states across multiple depths, so paths such as
      8 -> 9 -> 9 -> 7
  are genuinely searchable rather than losing the 9-plateau immediately,
- rank by absolute TOTAL, then current weighted objective, then C4,C8,C16,...,
- immediately save and stop on an exact zero-forbidden-cycle graph.

With the n=26 preset, --escape-height 1 explores the barrier TOTAL=9 around the
known TOTAL=8 basin while still preventing uncontrolled uphill drift.

Recommended first run:

    uv run python scripts/heg_fixed_order_polish_escape.py \
      --preset rewire2-n26 \
      --beam-width 32 \
      --max-depth 30 \
      --step-seconds 120 \
      --total-seconds 1200 \
      --workers 16 \
      --escape-lanes 8 \
      --escape-height 1 \
      --node-budget 10000000 \
      --witness-cap 1000000 \
      --save-best polish_best_n26.json \
      --save-lineage polish_lineage_n26.json
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

PRESET_REWIRE2_N26 = "rewire2-n26"
PRESET_EXPECTED_HASH_PREFIX = "5ba52272"

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
class SwitchMutation:
    removed_edges: tuple[Edge, Edge]
    added_edges: tuple[Edge, Edge]
    variant: str

    def label(self) -> str:
        removed = ",".join(f"{u}-{v}" for u, v in self.removed_edges)
        added = ",".join(f"{u}-{v}" for u, v in self.added_edges)
        return f"2switch:{self.variant} rm[{removed}] add[{added}]"


@dataclass(frozen=True, slots=True)
class BeamState:
    score: GraphScore
    parent_state: "BeamState | None"
    mutation: SwitchMutation | None
    depth: int

    @property
    def parent_score(self) -> GraphScore | None:
        return None if self.parent_state is None else self.parent_state.score


def norm_edge(u: int, v: int) -> Edge:
    if u == v:
        raise ValueError("self-loop")
    return (u, v) if u < v else (v, u)


def weight(length: int) -> int:
    return max(1, 64 // length)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Polish one fixed-order HEG graph using degree-preserving 2-switches."
        )
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--preset",
        choices=(PRESET_REWIRE2_N26,),
        default=PRESET_REWIRE2_N26,
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
    parser.add_argument("--max-depth", type=int, default=30)
    parser.add_argument(
        "--escape-lanes",
        type=int,
        default=8,
        help=(
            "Beam slots reserved for bounded uphill/plateau escape states. "
            "The remaining beam slots are the best absolute states."
        ),
    )
    parser.add_argument(
        "--escape-height",
        type=int,
        default=1,
        help=(
            "Maximum absolute TOTAL above the current global best retained in "
            "escape lanes. Example: best=8,height=1 keeps TOTAL=9 escape states "
            "across multiple depths."
        ),
    )
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
        "--max-switches-per-parent",
        type=int,
        default=0,
        help="0 = enumerate all legal 2-switch proposals; otherwise sample this many.",
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
    if args.max_depth < 0:
        parser.error("--max-depth must be >= 0")
    if args.escape_lanes < 0:
        parser.error("--escape-lanes must be >= 0")
    if args.escape_lanes > args.beam_width:
        parser.error("--escape-lanes must be <= --beam-width")
    if args.escape_height < 0:
        parser.error("--escape-height must be >= 0")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.inflight_per_worker < 1:
        parser.error("--inflight-per-worker must be >= 1")
    if args.max_switches_per_parent < 0:
        parser.error("--max-switches-per-parent must be >= 0")
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.step_seconds < 0 or args.total_seconds < 0:
        parser.error("time budgets must be >= 0")

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


def two_switch_mutations(
    graph: Graph,
    *,
    seed: int,
    limit: int,
) -> list[SwitchMutation]:
    edge_set = set(graph.edges)
    mutations: list[SwitchMutation] = []

    for first, second in disjoint_edge_pairs(graph.edges):
        a, b = first
        c, d = second

        pairings = (
            (
                (norm_edge(a, c), norm_edge(b, d)),
                "ac_bd",
            ),
            (
                (norm_edge(a, d), norm_edge(b, c)),
                "ad_bc",
            ),
        )

        for added_edges, variant in pairings:
            if added_edges[0] == added_edges[1]:
                continue

            # Removed edges no longer exist in the candidate, so an added edge is
            # illegal only if it exists elsewhere in the old graph.
            remaining = edge_set - {first, second}
            if added_edges[0] in remaining or added_edges[1] in remaining:
                continue

            mutations.append(
                SwitchMutation(
                    removed_edges=(first, second),
                    added_edges=added_edges,
                    variant=variant,
                )
            )

    rng = random.Random(seed)
    rng.shuffle(mutations)

    if limit:
        del mutations[limit:]

    return mutations


def apply_two_switch(graph: Graph, mutation: SwitchMutation) -> Graph:
    edge_set = set(graph.edges)

    for edge in mutation.removed_edges:
        if edge not in edge_set:
            raise ValueError(f"missing removed edge {edge}")
    for edge in mutation.removed_edges:
        edge_set.remove(edge)

    for edge in mutation.added_edges:
        if edge in edge_set:
            raise ValueError(f"duplicate added edge {edge}")
        edge_set.add(edge)

    candidate = Graph.from_edges(graph.order, edge_set)

    # 2-switches preserve all degrees, but may disconnect the graph.
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)

    if candidate.order != graph.order:
        raise RuntimeError("2-switch changed graph order")
    if len(candidate.edges) != len(graph.edges):
        raise RuntimeError("2-switch changed edge count")

    return candidate


@dataclass(slots=True)
class CandidateBucket:
    parent: BeamState
    mutations: list[SwitchMutation]


def build_buckets(
    beam: list[BeamState],
    *,
    depth: int,
    seed: int,
    limit: int,
) -> list[CandidateBucket]:
    buckets: list[CandidateBucket] = []

    for parent in beam:
        mutations = two_switch_mutations(
            parent.score.graph,
            seed=(
                seed
                ^ (depth << 24)
                ^ int(parent.score.graph.graph_hash[:16], 16)
            ),
            limit=limit,
        )
        buckets.append(CandidateBucket(parent=parent, mutations=mutations))

    return buckets


def interleaved_candidates(
    buckets: list[CandidateBucket],
) -> Iterator[tuple[BeamState, SwitchMutation]]:
    maximum = max((len(bucket.mutations) for bucket in buckets), default=0)

    for index in range(maximum):
        for bucket in buckets:
            if index < len(bucket.mutations):
                yield bucket.parent, bucket.mutations[index]


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


def select_beam_with_escape(
    states: list[BeamState],
    *,
    beam_width: int,
    escape_lanes: int,
    escape_height: int,
    global_best_total: int,
) -> tuple[list[BeamState], set[str], int]:
    """
    Select a mixed beam with persistent bounded escape lanes.

    Main lanes:
        the best absolute admissible states.

    Escape lanes:
        states with
            global_best_total < TOTAL <= global_best_total + escape_height
        even when they are neutral children of an already-uphill parent.

    This is the key difference from a simple per-step --max-uphill rule: a path
    8 -> 9 -> 9 -> 7 remains alive because the second TOTAL=9 state is still
    inside the absolute barrier around the global best.

    Admissibility:
    - every exact non-worsening child (child TOTAL <= parent TOTAL), OR
    - every exact state inside the absolute escape barrier.

    The function returns:
        (beam, hashes selected specifically as reserved escape lanes, admissible_count)
    """
    exact = [
        state
        for state in states
        if state.score.fully_exact and state.score.total is not None
    ]
    barrier = global_best_total + escape_height

    admissible: list[BeamState] = []
    for state in exact:
        child_total = state.score.total
        parent_total = (
            None if state.parent_score is None else state.parent_score.total
        )
        if child_total is None:
            continue

        non_worsening = (
            parent_total is not None and child_total <= parent_total
        )
        inside_escape_barrier = child_total <= barrier

        if non_worsening or inside_escape_barrier:
            admissible.append(state)

    ordered = sorted(
        admissible,
        key=lambda state: (rank_key(state), state.score.graph.graph_hash),
    )

    main_slots = max(0, beam_width - escape_lanes)
    selected = ordered[:main_slots]
    selected_hashes = {state.score.graph.graph_hash for state in selected}

    escape_pool = [
        state
        for state in ordered
        if state.score.graph.graph_hash not in selected_hashes
        and state.score.total is not None
        and global_best_total < state.score.total <= barrier
    ]

    # Prefer parent diversity in the reserved escape lanes so all slots do not
    # collapse onto tiny variations of one parent state.
    escape_selected: list[BeamState] = []
    used_parents: set[str] = set()

    for state in escape_pool:
        if len(escape_selected) >= escape_lanes:
            break
        parent_hash = (
            ""
            if state.parent_score is None
            else state.parent_score.graph.graph_hash
        )
        if parent_hash in used_parents:
            continue
        escape_selected.append(state)
        used_parents.add(parent_hash)

    if len(escape_selected) < escape_lanes:
        escape_hashes_now = {
            state.score.graph.graph_hash for state in escape_selected
        }
        for state in escape_pool:
            if len(escape_selected) >= escape_lanes:
                break
            if state.score.graph.graph_hash in escape_hashes_now:
                continue
            escape_selected.append(state)
            escape_hashes_now.add(state.score.graph.graph_hash)

    selected.extend(escape_selected)
    selected_hashes.update(
        state.score.graph.graph_hash for state in escape_selected
    )

    # If either lane class did not have enough states, fill remaining beam slots
    # with the best admissible unselected states.
    if len(selected) < beam_width:
        for state in ordered:
            if len(selected) >= beam_width:
                break
            graph_hash = state.score.graph.graph_hash
            if graph_hash in selected_hashes:
                continue
            selected.append(state)
            selected_hashes.add(graph_hash)

    reserved_escape_hashes = {
        state.score.graph.graph_hash for state in escape_selected
    }
    return selected, reserved_escape_hashes, len(admissible)


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
                "family": "two_switch",
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
        f"{'DEPTH':>5} {'TOTAL':>5} {'Δ':>4} {'WEIGHTED':>8} "
        f"{'COMPONENTS':<35} {'HASH':>8}"
    )

    for item in lineage:
        delta = delta_total(item)
        delta_text = "-" if delta is None else f"{delta:+d}"
        console.print(
            f"{item.depth:>5} "
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
        f"[bold]HEG fixed-order polish[/bold] "
        f"order={start_graph.order} beam={args.beam_width} "
        f"max_depth={args.max_depth} escape_lanes={args.escape_lanes} "
        f"escape_height={args.escape_height} workers={args.workers}"
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

    with ThreadPoolExecutor(
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

        console.print(
            f"START total={initial_score.total} weighted={initial_score.weighted} "
            f"{components_text(initial_score)} hash={start_graph.graph_hash[:8]}"
        )

        if args.preset == PRESET_REWIRE2_N26:
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
                depth=depth,
                seed=args.seed,
                limit=args.max_switches_per_parent,
            )
            proposal_space = sum(len(bucket.mutations) for bucket in buckets)
            stream = interleaved_candidates(buckets)

            metadata: dict[str, tuple[BeamState, SwitchMutation]] = {}
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
                        candidate = apply_two_switch(parent.score.graph, mutation)
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

            # Counterexample check happens before acceptance filtering.
            zero_hits = [state for state in exact_pool if state.score.total == 0]
            if zero_hits:
                hit_state = min(
                    zero_hits,
                    key=lambda state: state.score.graph.graph_hash,
                )
                path = maybe_save_hit(args, hit_state)
                console.print(
                    f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] "
                    f"order={hit_state.score.graph.order} depth={depth} "
                    f"saved: {path}"
                )
                print_final_lineage(hit_state)
                if args.save_best is not None:
                    save_graph(args.save_best, hit_state)
                if args.save_lineage is not None:
                    save_lineage(args.save_lineage, hit_state)
                return 0

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

            # Mark every exact scored graph visited only after the generation has
            # completed, so equivalent proposals inside this depth can still be
            # deduplicated consistently.
            visited.update(state.score.graph.graph_hash for state in exact_pool)

            best_child = min(
                exact_pool,
                key=lambda state: (rank_key(state), state.score.graph.graph_hash),
            )

            if rank_key(best_child) < rank_key(best):
                best = best_child

            if best.score.total is None:
                raise RuntimeError("global best unexpectedly became inexact")

            beam, reserved_escape_hashes, admissible_count = select_beam_with_escape(
                exact_pool,
                beam_width=args.beam_width,
                escape_lanes=args.escape_lanes,
                escape_height=args.escape_height,
                global_best_total=best.score.total,
            )

            elapsed = time.perf_counter() - step_started
            best_delta = delta_total(best_child)
            barrier = best.score.total + args.escape_height

            escape_in_beam = sum(
                state.score.graph.graph_hash in reserved_escape_hashes
                for state in beam
            )
            above_best_in_beam = sum(
                state.score.total is not None
                and state.score.total > best.score.total
                for state in beam
            )

            console.print(
                f"DEPTH {depth:>2} "
                f"scored={len(pool)}/{proposal_space} "
                f"exact={len(exact_pool)} "
                f"improving={improving} neutral={neutral} uphill={uphill} "
                f"admissible={admissible_count} "
                f"best_child_total={best_child.score.total} "
                f"Δ={best_delta:+d} "
                f"global_best={best.score.total} "
                f"barrier<={barrier} "
                f"time={elapsed:.2f}s"
            )
            console.print(
                f"     next beam={len(beam)} "
                f"reserved_escape={escape_in_beam}/{args.escape_lanes} "
                f"states_above_best={above_best_in_beam}; "
                f"best child {components_text(best_child.score)} "
                f"{best_child.mutation.label() if best_child.mutation else '-'} "
                f"hash={best_child.score.graph.graph_hash[:8]}"
            )

            if reserved_escape_hashes:
                escape_states = [
                    state
                    for state in beam
                    if state.score.graph.graph_hash in reserved_escape_hashes
                ]
                escape_best = min(
                    escape_states,
                    key=lambda state: (
                        rank_key(state),
                        state.score.graph.graph_hash,
                    ),
                )
                console.print(
                    f"     best escape total={escape_best.score.total} "
                    f"{components_text(escape_best.score)} "
                    f"hash={escape_best.score.graph.graph_hash[:8]}"
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
                    f"[yellow]Depth {depth}: no exact state remains inside the "
                    f"non-worsening/escape policy (barrier <= {barrier}).[/yellow]"
                )
                break

    if args.save_best is not None:
        save_graph(args.save_best, best)
        console.print(f"Best graph saved: {args.save_best}")

    if args.save_lineage is not None:
        save_lineage(args.save_lineage, best)
        console.print(f"Best lineage saved: {args.save_lineage}")

    print_final_lineage(best)

    console.print(
        f"[bold]Done[/bold] order={best.score.graph.order} "
        f"best_total={best.score.total} "
        f"{components_text(best.score)} "
        f"depth={best.depth} elapsed={time.perf_counter()-started:.2f}s "
        f"hash={best.score.graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
