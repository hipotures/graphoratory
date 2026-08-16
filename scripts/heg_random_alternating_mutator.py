#!/usr/bin/env python3
"""
HEG fixed-order blind random legal walk with alternating exploration/exploitation.

Mutation remains completely cycle-blind:

    parent -> legal random ADD/REMOVE walk -> exact HEG evaluation

The score is never used to construct a mutation. It is used only to maintain
an elite pool and, in alternating mode, to choose which pool supplies parents.

Alternating schedule
--------------------
With:

    --selection-mode alternating --phase-seconds 30

the parent process alternates:

    RANDOM -> ELITE -> RANDOM -> ELITE -> ...

RANDOM phase:
    parent is chosen uniformly from the score-blind reservoir, with optional
    --root-parent-prob restart probability.

ELITE phase:
    parent is chosen uniformly from the current top --elite-size exact graphs.
    The mutation kernel is unchanged and still does not inspect forbidden-cycle
    witnesses, cycle locations, hitting sets, or structural score features.

Every score worker both mutates and evaluates locally. The main process only
selects parents, dispatches batches, updates pools, deduplicates, and logs.

All intermediate graphs remain simple, connected, fixed-order, and delta>=3.
The number of edges may change unless --max-edges is set.

The authoritative external HEG scorer determines forbidden lengths dynamically
from the order. Only fully exact candidates can update the best result.
"""


from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import random
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Iterable

from rich.console import Console
from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import forbidden_lengths  # type: ignore[import-untyped]

from graphoratory.graphs import Graph
from graphoratory.science.worker import ScoreWorker


Edge = tuple[int, int]
MAX_ORDER = 128
DEFAULT_WITNESS_CAP = 1_000_000
DEFAULT_NODE_BUDGET = 10_000_000

STATUS_EXACT = "EXACT"
STATUS_SATURATED = "SATURATED_AT_CAP"
STATUS_BUDGET = "SEARCH_BUDGET_EXHAUSTED"

console = Console()
_WORKER_SCORER: ScoreWorker | None = None
_WORKER_WITNESS_CAP = DEFAULT_WITNESS_CAP
_WORKER_NODE_BUDGET = DEFAULT_NODE_BUDGET


@dataclass(frozen=True, slots=True)
class ComponentResult:
    length: int
    observed: int
    status: str
    nodes: int
    elapsed_ns: int


@dataclass(frozen=True, slots=True)
class ParentPayload:
    order: int
    edges: tuple[Edge, ...]
    graph_hash: str
    total: int
    weighted: int
    components: tuple[tuple[int, int], ...]

    @property
    def edge_count(self) -> int:
        return len(self.edges)


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    request_id: int
    parent: ParentPayload
    seed: int


@dataclass(frozen=True, slots=True)
class WorkerBatchTask:
    task_id: int
    requests: tuple[CandidateRequest, ...]
    walk_min: int
    walk_max: int
    walk_retries: int
    remove_trials: int
    max_edges: int


@dataclass(frozen=True, slots=True)
class CandidateResult:
    task_id: int
    request_id: int
    seed: int
    parent_hash: str
    order: int
    edges: tuple[Edge, ...] | None
    graph_hash: str | None
    components: tuple[ComponentResult, ...]
    total: int | None
    weighted: int | None
    score_status: str
    score_seconds: float
    walk_steps: int
    add_steps: int
    remove_steps: int
    net_added_edges: int
    net_removed_edges: int
    max_edges_seen: int
    mutation_seconds: float
    failure: str | None = None

    @property
    def fully_exact(self) -> bool:
        return self.total is not None and self.weighted is not None


@dataclass(slots=True)
class PopulationEntry:
    order: int
    edges: tuple[Edge, ...]
    graph_hash: str
    total: int
    weighted: int
    components: tuple[tuple[int, int], ...]
    parent_hash: str | None
    seed: int | None
    walk_steps: int
    add_steps: int
    remove_steps: int

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def parent_payload(self) -> ParentPayload:
        return ParentPayload(
            order=self.order,
            edges=self.edges,
            graph_hash=self.graph_hash,
            total=self.total,
            weighted=self.weighted,
            components=self.components,
        )


def norm_edge(u: int, v: int) -> Edge:
    if u == v:
        raise ValueError("self-loop")
    return (u, v) if u < v else (v, u)


def weight(length: int) -> int:
    return max(1, 64 // length)


def component_text(components: Iterable[tuple[int, int]]) -> str:
    return " ".join(f"C{length}={count}" for length, count in sorted(components))


def rank_key(entry: PopulationEntry) -> tuple[int, int, int, str]:
    return (entry.total, entry.weighted, entry.edge_count, entry.graph_hash)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cycle-blind random legal graph walk at fixed order; each score "
            "process mutates and evaluates its own candidates."
        )
    )
    parser.add_argument("--start-graph", type=Path, required=True)
    parser.add_argument("--expected-order", type=int, default=24)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--candidates-per-worker", type=int, default=8)
    parser.add_argument("--walk-min", type=int, default=4)
    parser.add_argument("--walk-max", type=int, default=48)
    parser.add_argument("--walk-retries", type=int, default=8)
    parser.add_argument("--remove-trials", type=int, default=64)
    parser.add_argument(
        "--max-edges",
        type=int,
        default=0,
        help="0 = no artificial upper edge-count cap",
    )
    parser.add_argument(
        "--elite-parent-prob",
        type=float,
        default=0.0,
        help=(
            "Probability of choosing a parent from the score-ranked elite. "
            "Default 0 keeps parent selection score-blind."
        ),
    )
    parser.add_argument(
        "--selection-mode",
        choices=("static", "alternating"),
        default="static",
        help=(
            "static uses --elite-parent-prob continuously; alternating switches "
            "between reservoir-only RANDOM and elite-only ELITE phases."
        ),
    )
    parser.add_argument(
        "--phase-seconds",
        type=float,
        default=30.0,
        help="Wall-clock duration of each RANDOM/ELITE phase in alternating mode.",
    )
    parser.add_argument(
        "--root-parent-prob",
        type=float,
        default=0.05,
        help=(
            "Static mode: root restart probability. Alternating mode: root "
            "restart probability in RANDOM phases only; ELITE phases use 0."
        ),
    )
    parser.add_argument("--reservoir-size", type=int, default=2048)
    parser.add_argument("--elite-size", type=int, default=64)
    parser.add_argument("--success-total", type=int, default=2)
    parser.add_argument(
        "--log-total",
        type=int,
        default=8,
        help="Append every new unique exact candidate with TOTAL <= this value.",
    )
    parser.add_argument("--total-seconds", type=float, default=900.0)
    parser.add_argument("--report-seconds", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=240816)
    parser.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET)
    parser.add_argument("--witness-cap", type=int, default=DEFAULT_WITNESS_CAP)
    parser.add_argument("--save-best", type=Path, default=Path("random_walk_best_n24.json"))
    parser.add_argument("--save-hits", type=Path, default=Path("random_walk_hits_n24.jsonl"))
    parser.add_argument("--save-pool", type=Path, default=Path("random_walk_pool_n24.json"))
    args = parser.parse_args()

    if args.expected_order < 4 or args.expected_order > MAX_ORDER:
        parser.error(f"--expected-order must be in [4,{MAX_ORDER}]")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.candidates_per_worker < 1:
        parser.error("--candidates-per-worker must be >= 1")
    if args.walk_min < 1 or args.walk_max < args.walk_min:
        parser.error("require 1 <= --walk-min <= --walk-max")
    if args.walk_retries < 1:
        parser.error("--walk-retries must be >= 1")
    if args.remove_trials < 1:
        parser.error("--remove-trials must be >= 1")
    if args.max_edges < 0:
        parser.error("--max-edges must be >= 0")
    complete_edges = args.expected_order * (args.expected_order - 1) // 2
    min_edges = math.ceil(3 * args.expected_order / 2)
    if args.max_edges and not (min_edges <= args.max_edges <= complete_edges):
        parser.error(
            f"--max-edges must be 0 or in [{min_edges},{complete_edges}] "
            f"for order {args.expected_order}"
        )
    if not 0.0 <= args.elite_parent_prob <= 1.0:
        parser.error("--elite-parent-prob must be in [0,1]")
    if not 0.0 <= args.root_parent_prob <= 1.0:
        parser.error("--root-parent-prob must be in [0,1]")
    if args.elite_parent_prob + args.root_parent_prob > 1.0:
        parser.error("elite-parent-prob + root-parent-prob must be <= 1")
    if args.reservoir_size < 1 or args.elite_size < 1:
        parser.error("pool sizes must be >= 1")
    if args.success_total < 0 or args.log_total < 0:
        parser.error("TOTAL thresholds must be >= 0")
    if args.total_seconds <= 0 or args.report_seconds <= 0:
        parser.error("time limits must be > 0")
    if args.phase_seconds <= 0:
        parser.error("--phase-seconds must be > 0")
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    return args


def load_graph(path: Path, expected_order: int) -> Graph:
    payload = json.loads(path.read_text(encoding="utf-8"))
    graph = Graph.from_edges(
        int(payload["order"]),
        (norm_edge(int(edge[0]), int(edge[1])) for edge in payload["edges"]),
    )
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    if graph.order != expected_order:
        raise ValueError(
            f"start graph order={graph.order}, expected {expected_order}"
        )
    return graph


def _close_worker_scorer() -> None:
    global _WORKER_SCORER
    scorer = _WORKER_SCORER
    _WORKER_SCORER = None
    if scorer is not None:
        try:
            scorer.close()
        except Exception:
            pass


def _worker_init(witness_cap: int, node_budget: int) -> None:
    global _WORKER_SCORER, _WORKER_WITNESS_CAP, _WORKER_NODE_BUDGET
    _WORKER_WITNESS_CAP = witness_cap
    _WORKER_NODE_BUDGET = node_budget
    scorer = ScoreWorker()
    scorer.__enter__()
    _WORKER_SCORER = scorer
    atexit.register(_close_worker_scorer)


def _degrees(order: int, edges: set[Edge]) -> list[int]:
    degree = [0] * order
    for u, v in edges:
        degree[u] += 1
        degree[v] += 1
    return degree


def _connected(order: int, edges: set[Edge]) -> bool:
    if order == 0:
        return True
    adjacency: list[list[int]] = [[] for _ in range(order)]
    for u, v in edges:
        adjacency[u].append(v)
        adjacency[v].append(u)
    seen = {0}
    stack = [0]
    while stack:
        u = stack.pop()
        for v in adjacency[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return len(seen) == order


def _legal(order: int, edges: set[Edge]) -> bool:
    if any(u == v or not (0 <= u < order and 0 <= v < order) for u, v in edges):
        return False
    degree = _degrees(order, edges)
    return min(degree, default=0) >= 3 and _connected(order, edges)


def _random_nonedge(order: int, edges: set[Edge], rng: random.Random) -> Edge | None:
    total_pairs = order * (order - 1) // 2
    if len(edges) >= total_pairs:
        return None

    # Rejection is very cheap in the sparse regime. Fall back to exact
    # enumeration so the method remains correct even if a walk gets dense.
    for _ in range(64):
        u = rng.randrange(order)
        v = rng.randrange(order - 1)
        if v >= u:
            v += 1
        edge = norm_edge(u, v)
        if edge not in edges:
            return edge

    nonedges = [
        (u, v)
        for u, v in combinations(range(order), 2)
        if (u, v) not in edges
    ]
    return rng.choice(nonedges) if nonedges else None


def _try_add(
    order: int,
    edges: set[Edge],
    rng: random.Random,
    max_edges: int,
) -> Edge | None:
    if max_edges and len(edges) >= max_edges:
        return None
    edge = _random_nonedge(order, edges, rng)
    if edge is None:
        return None
    edges.add(edge)
    return edge


def _try_remove(
    order: int,
    edges: set[Edge],
    rng: random.Random,
    remove_trials: int,
) -> Edge | None:
    degree = _degrees(order, edges)
    candidates = [
        edge for edge in edges if degree[edge[0]] > 3 and degree[edge[1]] > 3
    ]
    if not candidates:
        return None

    rng.shuffle(candidates)
    for edge in candidates[:remove_trials]:
        edges.remove(edge)
        if _connected(order, edges):
            return edge
        edges.add(edge)
    return None


def _mutate_legal_random_walk(
    parent: ParentPayload,
    *,
    seed: int,
    walk_min: int,
    walk_max: int,
    walk_retries: int,
    remove_trials: int,
    max_edges: int,
) -> tuple[tuple[Edge, ...], int, int, int, int, int, int] | None:
    """
    Return:
      edges, walk_steps, add_steps, remove_steps,
      net_added_edges, net_removed_edges, max_edges_seen

    The kernel knows only graph legality. It never sees a cycle score or witness.
    """
    start = set(parent.edges)
    rng = random.Random(seed)

    for _attempt in range(walk_retries):
        edges = set(start)
        steps = rng.randint(walk_min, walk_max)
        add_steps = 0
        remove_steps = 0
        max_edges_seen = len(edges)

        for _ in range(steps):
            choose_add = bool(rng.getrandbits(1))

            if choose_add:
                added = _try_add(parent.order, edges, rng, max_edges)
                if added is not None:
                    add_steps += 1
                    max_edges_seen = max(max_edges_seen, len(edges))
                    continue
                removed = _try_remove(
                    parent.order, edges, rng, remove_trials
                )
                if removed is not None:
                    remove_steps += 1
                    continue
            else:
                removed = _try_remove(
                    parent.order, edges, rng, remove_trials
                )
                if removed is not None:
                    remove_steps += 1
                    continue
                added = _try_add(parent.order, edges, rng, max_edges)
                if added is not None:
                    add_steps += 1
                    max_edges_seen = max(max_edges_seen, len(edges))
                    continue

            # Complete graph with an impossible removal under an edge cap, or
            # another degenerate boundary. End this attempt and retry.
            break

        if edges == start:
            continue
        if not _legal(parent.order, edges):
            raise RuntimeError("internal error: legal random walk produced invalid graph")

        net_added = len(edges - start)
        net_removed = len(start - edges)
        return (
            tuple(sorted(edges)),
            steps,
            add_steps,
            remove_steps,
            net_added,
            net_removed,
            max_edges_seen,
        )

    return None


def _score_edges(order: int, edges: tuple[Edge, ...]) -> tuple[
    tuple[ComponentResult, ...], int | None, int | None, str, float
]:
    scorer = _WORKER_SCORER
    if scorer is None:
        raise RuntimeError("worker scorer is not initialized")

    graph = Graph.from_edges(order, edges)
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    lengths = tuple(int(length) for length in forbidden_lengths(order))
    bit_graph = BitGraph.from_edges(order, edges)

    started = time.perf_counter()
    response = scorer.score(
        bit_graph,
        lengths=lengths,
        witness_cap=_WORKER_WITNESS_CAP,
        node_budget=_WORKER_NODE_BUDGET,
    )
    elapsed = time.perf_counter() - started

    by_length = {int(result.length): result for result in response.results}
    if set(by_length) != set(lengths):
        raise RuntimeError(
            f"scorer returned {sorted(by_length)}, expected {list(lengths)}"
        )

    components: list[ComponentResult] = []
    exact = True
    for length in lengths:
        result = by_length[length]
        raw_count = int(result.count)
        if raw_count >= _WORKER_WITNESS_CAP:
            observed = _WORKER_WITNESS_CAP
            status = STATUS_SATURATED
            exact = False
        elif bool(result.complete):
            observed = raw_count
            status = STATUS_EXACT
        else:
            observed = raw_count
            status = STATUS_BUDGET
            exact = False
        components.append(
            ComponentResult(
                length=length,
                observed=observed,
                status=status,
                nodes=int(result.nodes),
                elapsed_ns=int(result.elapsed_ns),
            )
        )

    if exact:
        total = sum(component.observed for component in components)
        weighted = sum(
            weight(component.length) * component.observed
            for component in components
        )
        score_status = STATUS_EXACT
    else:
        total = None
        weighted = None
        statuses = {component.status for component in components}
        score_status = (
            STATUS_SATURATED if STATUS_SATURATED in statuses else STATUS_BUDGET
        )

    return tuple(components), total, weighted, score_status, elapsed


def _run_worker_batch(task: WorkerBatchTask) -> tuple[CandidateResult, ...]:
    results: list[CandidateResult] = []
    local_hashes: set[str] = set()

    for request in task.requests:
        mutation_started = time.perf_counter()
        try:
            mutated = _mutate_legal_random_walk(
                request.parent,
                seed=request.seed,
                walk_min=task.walk_min,
                walk_max=task.walk_max,
                walk_retries=task.walk_retries,
                remove_trials=task.remove_trials,
                max_edges=task.max_edges,
            )
            mutation_seconds = time.perf_counter() - mutation_started
            if mutated is None:
                results.append(
                    CandidateResult(
                        task_id=task.task_id,
                        request_id=request.request_id,
                        seed=request.seed,
                        parent_hash=request.parent.graph_hash,
                        order=request.parent.order,
                        edges=None,
                        graph_hash=None,
                        components=(),
                        total=None,
                        weighted=None,
                        score_status="MUTATION_FAILED",
                        score_seconds=0.0,
                        walk_steps=0,
                        add_steps=0,
                        remove_steps=0,
                        net_added_edges=0,
                        net_removed_edges=0,
                        max_edges_seen=request.parent.edge_count,
                        mutation_seconds=mutation_seconds,
                        failure="walk produced no distinct legal graph",
                    )
                )
                continue

            (
                edges,
                walk_steps,
                add_steps,
                remove_steps,
                net_added_edges,
                net_removed_edges,
                max_edges_seen,
            ) = mutated
            graph = Graph.from_edges(request.parent.order, edges)
            graph_hash = graph.graph_hash
            if graph_hash == request.parent.graph_hash or graph_hash in local_hashes:
                results.append(
                    CandidateResult(
                        task_id=task.task_id,
                        request_id=request.request_id,
                        seed=request.seed,
                        parent_hash=request.parent.graph_hash,
                        order=request.parent.order,
                        edges=None,
                        graph_hash=graph_hash,
                        components=(),
                        total=None,
                        weighted=None,
                        score_status="LOCAL_DUPLICATE",
                        score_seconds=0.0,
                        walk_steps=walk_steps,
                        add_steps=add_steps,
                        remove_steps=remove_steps,
                        net_added_edges=net_added_edges,
                        net_removed_edges=net_removed_edges,
                        max_edges_seen=max_edges_seen,
                        mutation_seconds=mutation_seconds,
                        failure="local duplicate",
                    )
                )
                continue
            local_hashes.add(graph_hash)

            components, total, weighted, score_status, score_seconds = _score_edges(
                request.parent.order, edges
            )
            results.append(
                CandidateResult(
                    task_id=task.task_id,
                    request_id=request.request_id,
                    seed=request.seed,
                    parent_hash=request.parent.graph_hash,
                    order=request.parent.order,
                    edges=edges,
                    graph_hash=graph_hash,
                    components=components,
                    total=total,
                    weighted=weighted,
                    score_status=score_status,
                    score_seconds=score_seconds,
                    walk_steps=walk_steps,
                    add_steps=add_steps,
                    remove_steps=remove_steps,
                    net_added_edges=net_added_edges,
                    net_removed_edges=net_removed_edges,
                    max_edges_seen=max_edges_seen,
                    mutation_seconds=mutation_seconds,
                )
            )
        except Exception as exc:
            results.append(
                CandidateResult(
                    task_id=task.task_id,
                    request_id=request.request_id,
                    seed=request.seed,
                    parent_hash=request.parent.graph_hash,
                    order=request.parent.order,
                    edges=None,
                    graph_hash=None,
                    components=(),
                    total=None,
                    weighted=None,
                    score_status="ERROR",
                    score_seconds=0.0,
                    walk_steps=0,
                    add_steps=0,
                    remove_steps=0,
                    net_added_edges=0,
                    net_removed_edges=0,
                    max_edges_seen=request.parent.edge_count,
                    mutation_seconds=time.perf_counter() - mutation_started,
                    failure=f"{type(exc).__name__}: {exc}",
                )
            )

    return tuple(results)


def score_root(graph: Graph, witness_cap: int, node_budget: int) -> PopulationEntry:
    # One initialization score in the parent process is not part of the search
    # loop. All candidate mutation + evaluation work happens in score processes.
    with ScoreWorker() as scorer:
        lengths = tuple(int(length) for length in forbidden_lengths(graph.order))
        response = scorer.score(
            BitGraph.from_edges(graph.order, graph.edges),
            lengths=lengths,
            witness_cap=witness_cap,
            node_budget=node_budget,
        )
    by_length = {int(result.length): result for result in response.results}
    components: list[tuple[int, int]] = []
    for length in lengths:
        result = by_length[length]
        if int(result.count) >= witness_cap or not bool(result.complete):
            raise RuntimeError(
                f"start graph score is not exact at C{length}: "
                f"count={int(result.count)} complete={bool(result.complete)}"
            )
        components.append((length, int(result.count)))
    total = sum(count for _, count in components)
    weighted = sum(weight(length) * count for length, count in components)
    return PopulationEntry(
        order=graph.order,
        edges=tuple(graph.edges),
        graph_hash=graph.graph_hash,
        total=total,
        weighted=weighted,
        components=tuple(components),
        parent_hash=None,
        seed=None,
        walk_steps=0,
        add_steps=0,
        remove_steps=0,
    )


def choose_parent(
    *,
    rng: random.Random,
    root: PopulationEntry,
    reservoir: list[PopulationEntry],
    elite: list[PopulationEntry],
    root_probability: float,
    elite_probability: float,
) -> PopulationEntry:
    draw = rng.random()
    if draw < root_probability:
        return root
    if draw < root_probability + elite_probability and elite:
        return rng.choice(elite)
    if reservoir:
        return rng.choice(reservoir)
    return root


def update_elite(
    elite: list[PopulationEntry],
    entry: PopulationEntry,
    capacity: int,
) -> None:
    by_hash = {item.graph_hash: item for item in elite}
    previous = by_hash.get(entry.graph_hash)
    if previous is None or rank_key(entry) < rank_key(previous):
        by_hash[entry.graph_hash] = entry
    elite[:] = sorted(by_hash.values(), key=rank_key)[:capacity]


def update_reservoir(
    reservoir: list[PopulationEntry],
    entry: PopulationEntry,
    *,
    capacity: int,
    seen_exact_unique: int,
    rng: random.Random,
) -> None:
    if len(reservoir) < capacity:
        reservoir.append(entry)
        return
    slot = rng.randrange(seen_exact_unique)
    if slot < capacity:
        reservoir[slot] = entry


def result_to_entry(result: CandidateResult) -> PopulationEntry:
    if (
        result.edges is None
        or result.graph_hash is None
        or result.total is None
        or result.weighted is None
    ):
        raise ValueError("result is not an exact graph candidate")
    return PopulationEntry(
        order=result.order,
        edges=result.edges,
        graph_hash=result.graph_hash,
        total=result.total,
        weighted=result.weighted,
        components=tuple(
            (component.length, component.observed)
            for component in result.components
        ),
        parent_hash=result.parent_hash,
        seed=result.seed,
        walk_steps=result.walk_steps,
        add_steps=result.add_steps,
        remove_steps=result.remove_steps,
    )


def save_entry(path: Path, entry: PopulationEntry, *, metadata: dict[str, object]) -> None:
    graph = Graph.from_edges(entry.order, entry.edges)
    payload = {
        **graph.record(),
        "score": {
            "total": entry.total,
            "weighted": entry.weighted,
            "components": {
                str(length): {"observed": count, "status": STATUS_EXACT}
                for length, count in entry.components
            },
        },
        "random_walk": {
            "parent_hash": entry.parent_hash,
            "seed": entry.seed,
            "walk_steps": entry.walk_steps,
            "add_steps": entry.add_steps,
            "remove_steps": entry.remove_steps,
        },
        "experiment": metadata,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_hit(path: Path, entry: PopulationEntry, *, elapsed: float, evaluated: int) -> None:
    payload = {
        "elapsed_seconds": elapsed,
        "evaluated": evaluated,
        "order": entry.order,
        "edge_count": entry.edge_count,
        "graph_hash": entry.graph_hash,
        "parent_hash": entry.parent_hash,
        "seed": entry.seed,
        "walk_steps": entry.walk_steps,
        "add_steps": entry.add_steps,
        "remove_steps": entry.remove_steps,
        "total": entry.total,
        "weighted": entry.weighted,
        "components": {str(length): count for length, count in entry.components},
        "edges": [list(edge) for edge in entry.edges],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def save_pool(
    path: Path,
    *,
    root: PopulationEntry,
    best: PopulationEntry,
    elite: list[PopulationEntry],
    reservoir: list[PopulationEntry],
    metadata: dict[str, object],
) -> None:
    def compact(entry: PopulationEntry) -> dict[str, object]:
        return {
            "order": entry.order,
            "edge_count": entry.edge_count,
            "graph_hash": entry.graph_hash,
            "parent_hash": entry.parent_hash,
            "seed": entry.seed,
            "walk_steps": entry.walk_steps,
            "add_steps": entry.add_steps,
            "remove_steps": entry.remove_steps,
            "total": entry.total,
            "weighted": entry.weighted,
            "components": {str(length): count for length, count in entry.components},
            "edges": [list(edge) for edge in entry.edges],
        }

    payload = {
        "schema_version": "heg.random_legal_walk.pool.v1",
        "metadata": metadata,
        "root": compact(root),
        "best": compact(best),
        "elite": [compact(entry) for entry in elite],
        "reservoir": [compact(entry) for entry in reservoir],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root_graph = load_graph(args.start_graph, args.expected_order)
    root = score_root(root_graph, args.witness_cap, args.node_budget)

    min_edges = math.ceil(3 * root.order / 2)
    max_edges_label = "none" if args.max_edges == 0 else str(args.max_edges)
    if args.selection_mode == "alternating":
        parent_mode = (
            f"alternating RANDOM/ELITE phase={args.phase_seconds:.1f}s "
            f"elite_size={args.elite_size}"
        )
    else:
        parent_mode = (
            "score-blind reservoir"
            if args.elite_parent_prob == 0.0
            else f"hybrid elite_prob={args.elite_parent_prob:.3f}"
        )

    console.print(
        f"HEG random legal walk order={root.order} delta>=3 workers={args.workers} "
        f"candidates/worker={args.candidates_per_worker} walk={args.walk_min}..{args.walk_max}"
    )
    console.print(
        f"mutation=cycle-blind ADD/REMOVE legal walk parent_mode={parent_mode} "
        f"root_restart={args.root_parent_prob:.3f} edge_range={min_edges}..{max_edges_label}"
    )
    console.print(
        f"budget={args.total_seconds:.1f}s success_TOTAL<={args.success_total} "
        f"node_budget={args.node_budget:,} cap={args.witness_cap:,}"
    )
    console.print(
        f"START total={root.total} weighted={root.weighted} "
        f"{component_text(root.components)} m={root.edge_count} hash={root.graph_hash[:8]}"
    )

    if args.save_hits.exists():
        # A fresh invocation is a fresh experiment. Avoid silently mixing runs.
        args.save_hits.unlink()

    metadata: dict[str, object] = {
        "kind": "cycle_blind_random_legal_walk",
        "expected_order": args.expected_order,
        "min_degree": 3,
        "workers": args.workers,
        "candidates_per_worker": args.candidates_per_worker,
        "walk_min": args.walk_min,
        "walk_max": args.walk_max,
        "max_edges": args.max_edges,
        "selection_mode": args.selection_mode,
        "phase_seconds": args.phase_seconds,
        "elite_parent_probability_static": args.elite_parent_prob,
        "root_parent_probability": args.root_parent_prob,
        "reservoir_size": args.reservoir_size,
        "elite_size": args.elite_size,
        "seed": args.seed,
        "node_budget": args.node_budget,
        "witness_cap": args.witness_cap,
        "pid": os.getpid(),
    }

    best = root
    elite: list[PopulationEntry] = [root]
    reservoir: list[PopulationEntry] = [root]
    seen_hashes: set[str] = {root.graph_hash}
    seen_exact_unique = 1
    master_rng = random.Random(args.seed)

    evaluated = 0
    exact = 0
    nonexact = 0
    duplicates = 0
    mutation_failures = 0
    errors = 0
    low_logged = 0
    rounds = 0
    task_counter = 0
    request_counter = 0
    family_counts: Counter[str] = Counter()
    edge_count_hist: Counter[int] = Counter({root.edge_count: 1})
    profile_hist: Counter[tuple[int, ...]] = Counter()
    profile_hist[tuple(count for _, count in root.components)] += 1
    phase_evaluated: Counter[str] = Counter()
    phase_exact: Counter[str] = Counter()
    phase_unique: Counter[str] = Counter()
    phase_best_improvements: Counter[str] = Counter()
    current_phase_name: str | None = None
    current_phase_index: int | None = None

    started = time.perf_counter()
    deadline = started + args.total_seconds
    next_report = started + args.report_seconds
    stop_reason = "time budget exhausted"

    save_entry(args.save_best, best, metadata=metadata)

    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_worker_init,
        initargs=(args.witness_cap, args.node_budget),
    ) as executor:
        while time.perf_counter() < deadline:
            dispatch_now = time.perf_counter()
            if args.selection_mode == "alternating":
                phase_index = int((dispatch_now - started) // args.phase_seconds)
                phase_name = "RANDOM" if phase_index % 2 == 0 else "ELITE"
                if phase_name == "RANDOM":
                    round_root_probability = args.root_parent_prob
                    round_elite_probability = 0.0
                else:
                    round_root_probability = 0.0
                    round_elite_probability = 1.0
            else:
                phase_index = 0
                phase_name = "STATIC"
                round_root_probability = args.root_parent_prob
                round_elite_probability = args.elite_parent_prob

            if (
                phase_name != current_phase_name
                or phase_index != current_phase_index
            ):
                console.print(
                    f"PHASE {phase_name} epoch={phase_index} "
                    f"t={dispatch_now-started:.1f}s "
                    f"elite={len(elite)} reservoir={len(reservoir)}"
                )
                current_phase_name = phase_name
                current_phase_index = phase_index

            tasks: list[WorkerBatchTask] = []
            for _worker_slot in range(args.workers):
                requests: list[CandidateRequest] = []
                for _ in range(args.candidates_per_worker):
                    parent = choose_parent(
                        rng=master_rng,
                        root=root,
                        reservoir=reservoir,
                        elite=elite,
                        root_probability=round_root_probability,
                        elite_probability=round_elite_probability,
                    )
                    seed = master_rng.getrandbits(63)
                    requests.append(
                        CandidateRequest(
                            request_id=request_counter,
                            parent=parent.parent_payload(),
                            seed=seed,
                        )
                    )
                    request_counter += 1
                tasks.append(
                    WorkerBatchTask(
                        task_id=task_counter,
                        requests=tuple(requests),
                        walk_min=args.walk_min,
                        walk_max=args.walk_max,
                        walk_retries=args.walk_retries,
                        remove_trials=args.remove_trials,
                        max_edges=args.max_edges,
                    )
                )
                task_counter += 1

            futures = [executor.submit(_run_worker_batch, task) for task in tasks]
            round_results: list[CandidateResult] = []
            for future in as_completed(futures):
                round_results.extend(future.result())

            # Deterministic state update independent of process completion order.
            round_results.sort(key=lambda item: (item.task_id, item.request_id))
            rounds += 1
            found_success = False

            for result in round_results:
                if result.score_status == "MUTATION_FAILED":
                    mutation_failures += 1
                    continue
                if result.score_status == "LOCAL_DUPLICATE":
                    duplicates += 1
                    continue
                if result.score_status == "ERROR":
                    errors += 1
                    if errors <= 5:
                        console.print(
                            f"worker error task={result.task_id} req={result.request_id}: "
                            f"{result.failure}"
                        )
                    continue

                evaluated += 1
                phase_evaluated[phase_name] += 1
                family_counts["add_steps"] += result.add_steps
                family_counts["remove_steps"] += result.remove_steps

                if not result.fully_exact:
                    nonexact += 1
                    continue
                exact += 1
                phase_exact[phase_name] += 1
                if result.graph_hash is None:
                    raise RuntimeError("exact result without graph hash")
                if result.graph_hash in seen_hashes:
                    duplicates += 1
                    continue

                seen_hashes.add(result.graph_hash)
                seen_exact_unique += 1
                phase_unique[phase_name] += 1
                entry = result_to_entry(result)
                edge_count_hist[entry.edge_count] += 1
                profile_hist[tuple(count for _, count in entry.components)] += 1

                update_reservoir(
                    reservoir,
                    entry,
                    capacity=args.reservoir_size,
                    seen_exact_unique=seen_exact_unique,
                    rng=master_rng,
                )
                update_elite(elite, entry, args.elite_size)

                if entry.total <= args.log_total:
                    append_hit(
                        args.save_hits,
                        entry,
                        elapsed=time.perf_counter() - started,
                        evaluated=evaluated,
                    )
                    low_logged += 1

                if rank_key(entry) < rank_key(best):
                    old = best
                    best = entry
                    phase_best_improvements[phase_name] += 1
                    save_entry(args.save_best, best, metadata=metadata)
                    console.print(
                        f"NEW BEST total={best.total} ({best.total-old.total:+d}) "
                        f"weighted={best.weighted} {component_text(best.components)} "
                        f"m={best.edge_count} walk={best.walk_steps} "
                        f"+{best.add_steps}/-{best.remove_steps} "
                        f"hash={best.graph_hash[:8]} parent={str(best.parent_hash)[:8]}"
                    )

                if entry.total <= args.success_total:
                    best = entry if rank_key(entry) <= rank_key(best) else best
                    save_entry(args.save_best, best, metadata=metadata)
                    stop_reason = (
                        f"success: found exact TOTAL={entry.total} <= {args.success_total}"
                    )
                    found_success = True
                    break

            now = time.perf_counter()
            if now >= next_report or found_success:
                elapsed = now - started
                rate = evaluated / elapsed if elapsed > 0 else 0.0
                unique_exact = len(seen_hashes)
                common_edges = ",".join(
                    f"m{m}:{count}"
                    for m, count in edge_count_hist.most_common(5)
                )
                common_profiles = ", ".join(
                    f"{profile}:{count}"
                    for profile, count in profile_hist.most_common(4)
                )
                console.print(
                    f"STATUS t={elapsed:.1f}s phase={phase_name} rounds={rounds} evaluated={evaluated:,} "
                    f"exact={exact:,} nonexact={nonexact:,} unique={unique_exact:,} "
                    f"dup={duplicates:,} mutfail={mutation_failures:,} errors={errors:,} "
                    f"rate={rate:.1f}/s"
                )
                console.print(
                    f"       best TOTAL={best.total} weighted={best.weighted} "
                    f"{component_text(best.components)} m={best.edge_count} "
                    f"hash={best.graph_hash[:8]} elite={len(elite)} "
                    f"reservoir={len(reservoir)} low_logged={low_logged}"
                )
                console.print(
                    f"       move_steps add={family_counts['add_steps']:,} "
                    f"remove={family_counts['remove_steps']:,}; edge_hist {common_edges}"
                )
                console.print(f"       profiles {common_profiles}")
                while next_report <= now:
                    next_report += args.report_seconds

            if found_success:
                break

    elapsed = time.perf_counter() - started
    metadata.update(
        {
            "elapsed_seconds": elapsed,
            "rounds": rounds,
            "evaluated": evaluated,
            "exact": exact,
            "nonexact": nonexact,
            "duplicates": duplicates,
            "mutation_failures": mutation_failures,
            "errors": errors,
            "stop_reason": stop_reason,
            "phase_stats": {
                name: {
                    "evaluated": int(phase_evaluated[name]),
                    "exact": int(phase_exact[name]),
                    "unique_exact": int(phase_unique[name]),
                    "best_improvements": int(phase_best_improvements[name]),
                }
                for name in sorted(
                    set(phase_evaluated)
                    | set(phase_exact)
                    | set(phase_unique)
                    | set(phase_best_improvements)
                )
            },
        }
    )
    save_entry(args.save_best, best, metadata=metadata)
    save_pool(
        args.save_pool,
        root=root,
        best=best,
        elite=elite,
        reservoir=reservoir,
        metadata=metadata,
    )

    console.print(stop_reason)
    console.print(
        f"DONE best_total={best.total} weighted={best.weighted} "
        f"{component_text(best.components)} m={best.edge_count} "
        f"elapsed={elapsed:.2f}s evaluated={evaluated:,} exact={exact:,} "
        f"hash={best.graph_hash[:8]}"
    )
    console.print(f"Best graph saved: {args.save_best}")
    console.print(f"Low-TOTAL discoveries: {args.save_hits}")
    console.print(f"Final pools saved: {args.save_pool}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
