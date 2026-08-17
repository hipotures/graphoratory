#!/usr/bin/env python3
"""
HEG fixed-order blind legal random walk with cascade scoring and evaluation budgets.

Purpose
-------
This is the optimized follow-up to heg_random_alternating_mutator.py for
orders >= 32, where exact C32 enumeration makes full scoring of every candidate
unnecessarily expensive.

The mutation kernel remains cycle-blind:

    parent -> legal random ADD/REMOVE walk

Scoring is cascade/branch-and-bound:

    C4 -> C8 -> C16 -> C32 -> ...

After each exact component, if the partial TOTAL is already strictly greater
than the incumbent TOTAL, the candidate is CERTIFIED_PRUNED and longer cycle
lengths are not scored.  A dynamic per-length witness cap is also chosen so the
scorer may stop as soon as enough witnesses have been found to prove that the
candidate cannot beat the incumbent.

Important properties
--------------------
* pruning is conservative: only non-negative already-proved cycle counts are used;
* candidates tied with the incumbent are NOT pruned merely for equality, so
  equal-TOTAL exact graphs can still populate the elite pool;
* the RANDOM reservoir may contain legal candidates even if their score was
  pruned/nonexact; mutation never consumes score information;
* only fully exact candidates can enter ELITE or update BEST;
* phase changes and total budget are based on evaluated candidates, not wall time;
* an optional emergency wall-clock cap remains available;
* .json and .json.gz start graphs are both supported;
* global all-history hash deduplication is intentionally NOT used: only exact
  graphs and the bounded reservoir are deduplicated, so RAM/CPU do not grow
  with tens of millions of pruned candidates.

"evaluated" means a non-duplicate legal candidate reached the cascade scorer.
It includes EXACT, CERTIFIED_PRUNED, and scorer-budget/cap outcomes.

Recommended use for the current HEG runs:
    n=32: --evaluation-budget 300000 --phase-evaluations 15000
    n=33: --evaluation-budget 360000 --phase-evaluations 16500
"""

from __future__ import annotations

import argparse
import atexit
import gzip
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
STATUS_PRUNED = "CERTIFIED_PRUNED"
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
    witness_cap: int


@dataclass(frozen=True, slots=True)
class ParentPayload:
    order: int
    edges: tuple[Edge, ...]
    graph_hash: str

    @property
    def edge_count(self) -> int:
        return len(self.edges)


@dataclass(frozen=True, slots=True)
class ExactEntry:
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
        return ParentPayload(self.order, self.edges, self.graph_hash)


@dataclass(frozen=True, slots=True)
class ReservoirEntry:
    order: int
    edges: tuple[Edge, ...]
    graph_hash: str

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def parent_payload(self) -> ParentPayload:
        return ParentPayload(self.order, self.edges, self.graph_hash)


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    request_id: int
    parent: ParentPayload
    seed: int
    incumbent_total: int


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
    scored_lengths: tuple[int, ...]
    prune_lower_bound: int | None
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
        return (
            self.score_status == STATUS_EXACT
            and self.total is not None
            and self.weighted is not None
        )

    @property
    def reached_scorer(self) -> bool:
        return self.score_status not in {
            "MUTATION_FAILED",
            "LOCAL_DUPLICATE",
            "ERROR",
        }


def norm_edge(u: int, v: int) -> Edge:
    if u == v:
        raise ValueError("self-loop")
    return (u, v) if u < v else (v, u)


def weight(length: int) -> int:
    return max(1, 64 // length)


def component_text(components: Iterable[tuple[int, int]]) -> str:
    return " ".join(f"C{length}={count}" for length, count in sorted(components))


def rank_key(entry: ExactEntry) -> tuple[int, int, int, str]:
    return (entry.total, entry.weighted, entry.edge_count, entry.graph_hash)


def read_json(path: Path) -> dict[str, object]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cycle-blind HEG random walk with cascade scoring and evaluation budgets."
    )
    p.add_argument("--start-graph", type=Path, required=True)
    p.add_argument("--expected-order", type=int, required=True)

    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--candidates-per-worker", type=int, default=8)

    p.add_argument("--walk-min", type=int, default=4)
    p.add_argument("--walk-max", type=int, default=48)
    p.add_argument("--walk-retries", type=int, default=8)
    p.add_argument("--remove-trials", type=int, default=64)
    p.add_argument("--max-edges", type=int, default=0)

    p.add_argument(
        "--evaluation-budget",
        type=int,
        default=300_000,
        help="Total scored candidate budget (EXACT + certified-pruned + nonexact).",
    )
    p.add_argument(
        "--phase-evaluations",
        type=int,
        default=15_000,
        help="Switch RANDOM/ELITE after this many evaluated candidates.",
    )
    p.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help="Emergency wall-clock cap; 0 disables it.",
    )
    p.add_argument("--report-evaluations", type=int, default=10_000)
    p.add_argument("--report-seconds", type=float, default=30.0)

    p.add_argument("--root-parent-prob", type=float, default=0.05)
    p.add_argument("--reservoir-size", type=int, default=4096)
    p.add_argument("--elite-size", type=int, default=128)

    p.add_argument(
        "--success-total",
        type=int,
        default=0,
        help="Stop immediately when an exact candidate reaches TOTAL <= this value.",
    )
    p.add_argument("--log-total", type=int, default=32)
    p.add_argument("--seed", type=int, default=8172026)

    p.add_argument("--node-budget", type=int, default=DEFAULT_NODE_BUDGET)
    p.add_argument(
        "--root-node-budget",
        type=int,
        default=250_000_000,
        help=(
            "Maximum node budget used only to obtain an exact bootstrap/root score. "
            "Candidate scoring continues to use --node-budget."
        ),
    )
    p.add_argument("--witness-cap", type=int, default=DEFAULT_WITNESS_CAP)

    p.add_argument("--save-best", type=Path, default=Path("cascade_best.json"))
    p.add_argument("--save-hits", type=Path, default=Path("cascade_hits.jsonl"))
    p.add_argument("--save-pool", type=Path, default=Path("cascade_pool.json"))
    p.add_argument("--save-summary", type=Path, default=Path("cascade_summary.json"))

    args = p.parse_args()

    if not (4 <= args.expected_order <= MAX_ORDER):
        p.error(f"--expected-order must be in [4,{MAX_ORDER}]")
    if args.workers < 1 or args.candidates_per_worker < 1:
        p.error("worker counts must be >= 1")
    if args.walk_min < 1 or args.walk_max < args.walk_min:
        p.error("require 1 <= --walk-min <= --walk-max")
    if args.walk_retries < 1 or args.remove_trials < 1:
        p.error("retry/trial counts must be >= 1")
    if args.max_edges < 0:
        p.error("--max-edges must be >= 0")
    min_edges = math.ceil(3 * args.expected_order / 2)
    complete_edges = args.expected_order * (args.expected_order - 1) // 2
    if args.max_edges and not (min_edges <= args.max_edges <= complete_edges):
        p.error(f"--max-edges must be 0 or in [{min_edges},{complete_edges}]")
    if args.evaluation_budget < 1:
        p.error("--evaluation-budget must be >= 1")
    if args.phase_evaluations < 1:
        p.error("--phase-evaluations must be >= 1")
    if args.max_seconds < 0:
        p.error("--max-seconds must be >= 0")
    if args.report_evaluations < 1 or args.report_seconds <= 0:
        p.error("report intervals must be positive")
    if not 0 <= args.root_parent_prob <= 1:
        p.error("--root-parent-prob must be in [0,1]")
    if args.reservoir_size < 1 or args.elite_size < 1:
        p.error("pool sizes must be >= 1")
    if args.success_total < 0 or args.log_total < 0:
        p.error("TOTAL thresholds must be >= 0")
    if args.node_budget < 1:
        p.error("--node-budget must be >= 1")
    if args.root_node_budget < args.node_budget:
        p.error("--root-node-budget must be >= --node-budget")
    if args.witness_cap < 2:
        p.error("--witness-cap must be >= 2")
    return args


def load_graph(path: Path, expected_order: int) -> Graph:
    payload = read_json(path)
    order = int(payload["order"])
    raw_edges = payload["edges"]
    if not isinstance(raw_edges, list):
        raise ValueError("start graph missing edges list")
    graph = Graph.from_edges(
        order,
        (norm_edge(int(edge[0]), int(edge[1])) for edge in raw_edges),
    )
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    if graph.order != expected_order:
        raise ValueError(f"start graph order={graph.order}, expected {expected_order}")
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
    return min(_degrees(order, edges), default=0) >= 3 and _connected(order, edges)


def _random_nonedge(order: int, edges: set[Edge], rng: random.Random) -> Edge | None:
    total_pairs = order * (order - 1) // 2
    if len(edges) >= total_pairs:
        return None
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
        edge for edge in edges
        if degree[edge[0]] > 3 and degree[edge[1]] > 3
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
                removed = _try_remove(parent.order, edges, rng, remove_trials)
                if removed is not None:
                    remove_steps += 1
                    continue
            else:
                removed = _try_remove(parent.order, edges, rng, remove_trials)
                if removed is not None:
                    remove_steps += 1
                    continue
                added = _try_add(parent.order, edges, rng, max_edges)
                if added is not None:
                    add_steps += 1
                    max_edges_seen = max(max_edges_seen, len(edges))
                    continue
            break

        if edges == start:
            continue
        if not _legal(parent.order, edges):
            raise RuntimeError("legal random walk produced invalid graph")

        return (
            tuple(sorted(edges)),
            steps,
            add_steps,
            remove_steps,
            len(edges - start),
            len(start - edges),
            max_edges_seen,
        )
    return None


def _single_length_score(
    bit_graph: BitGraph,
    *,
    length: int,
    witness_cap: int,
) -> ComponentResult:
    scorer = _WORKER_SCORER
    if scorer is None:
        raise RuntimeError("worker scorer is not initialized")

    response = scorer.score(
        bit_graph,
        lengths=(length,),
        witness_cap=witness_cap,
        node_budget=_WORKER_NODE_BUDGET,
    )
    if len(response.results) != 1 or int(response.results[0].length) != length:
        got = [int(item.length) for item in response.results]
        raise RuntimeError(f"scorer returned {got}, expected [{length}]")

    item = response.results[0]
    raw_count = int(item.count)
    if raw_count >= witness_cap:
        status = STATUS_SATURATED
    elif bool(item.complete):
        status = STATUS_EXACT
    else:
        status = STATUS_BUDGET

    return ComponentResult(
        length=length,
        observed=raw_count,
        status=status,
        nodes=int(item.nodes),
        elapsed_ns=int(item.elapsed_ns),
        witness_cap=witness_cap,
    )


def _score_edges_cascade(
    order: int,
    edges: tuple[Edge, ...],
    incumbent_total: int,
) -> tuple[
    tuple[ComponentResult, ...],
    int | None,
    int | None,
    str,
    float,
    int | None,
]:
    """
    Conservative cascade score.

    Prune only after proving partial TOTAL > incumbent_total.
    Equality is allowed to continue so equal-TOTAL graphs can be exact and
    diversify the elite pool.
    """
    graph = Graph.from_edges(order, edges)
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    bit_graph = BitGraph.from_edges(order, edges)
    lengths = tuple(int(length) for length in forbidden_lengths(order))

    started = time.perf_counter()
    components: list[ComponentResult] = []
    partial_total = 0
    partial_weighted = 0

    for length in lengths:
        # We need enough witnesses at this component to prove:
        # partial_total + C_length > incumbent_total.
        need_to_prune = incumbent_total - partial_total + 1
        if need_to_prune <= 0:
            return (
                tuple(components),
                None,
                None,
                STATUS_PRUNED,
                time.perf_counter() - started,
                partial_total,
            )

        # ScoreWorker callers in this project use witness_cap >= 2.
        # If just one witness would suffice, cap=2 may do one extra witness of
        # work but remains conservative.
        local_cap = min(
            _WORKER_WITNESS_CAP,
            max(2, need_to_prune),
        )

        component = _single_length_score(
            bit_graph,
            length=length,
            witness_cap=local_cap,
        )
        components.append(component)

        if component.status == STATUS_EXACT:
            partial_total += component.observed
            partial_weighted += weight(length) * component.observed
            if partial_total > incumbent_total:
                return (
                    tuple(components),
                    None,
                    None,
                    STATUS_PRUNED,
                    time.perf_counter() - started,
                    partial_total,
                )
            continue

        if component.status == STATUS_SATURATED:
            lower_bound = partial_total + component.observed
            if lower_bound > incumbent_total:
                return (
                    tuple(components),
                    None,
                    None,
                    STATUS_PRUNED,
                    time.perf_counter() - started,
                    lower_bound,
                )
            # This can only happen if the global cap was too small to establish
            # the incumbent comparison.
            return (
                tuple(components),
                None,
                None,
                STATUS_SATURATED,
                time.perf_counter() - started,
                lower_bound,
            )

        # Node budget exhausted before an exact answer or a pruning certificate.
        return (
            tuple(components),
            None,
            None,
            STATUS_BUDGET,
            time.perf_counter() - started,
            partial_total + component.observed,
        )

    return (
        tuple(components),
        partial_total,
        partial_weighted,
        STATUS_EXACT,
        time.perf_counter() - started,
        None,
    )


def _failure_result(
    *,
    task: WorkerBatchTask,
    request: CandidateRequest,
    status: str,
    mutation_seconds: float,
    failure: str,
    graph_hash: str | None = None,
    walk_steps: int = 0,
    add_steps: int = 0,
    remove_steps: int = 0,
    net_added_edges: int = 0,
    net_removed_edges: int = 0,
    max_edges_seen: int | None = None,
) -> CandidateResult:
    return CandidateResult(
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
        score_status=status,
        score_seconds=0.0,
        scored_lengths=(),
        prune_lower_bound=None,
        walk_steps=walk_steps,
        add_steps=add_steps,
        remove_steps=remove_steps,
        net_added_edges=net_added_edges,
        net_removed_edges=net_removed_edges,
        max_edges_seen=(
            request.parent.edge_count
            if max_edges_seen is None
            else max_edges_seen
        ),
        mutation_seconds=mutation_seconds,
        failure=failure,
    )


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
                    _failure_result(
                        task=task,
                        request=request,
                        status="MUTATION_FAILED",
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
                    _failure_result(
                        task=task,
                        request=request,
                        status="LOCAL_DUPLICATE",
                        mutation_seconds=mutation_seconds,
                        failure="local duplicate",
                        graph_hash=graph_hash,
                        walk_steps=walk_steps,
                        add_steps=add_steps,
                        remove_steps=remove_steps,
                        net_added_edges=net_added_edges,
                        net_removed_edges=net_removed_edges,
                        max_edges_seen=max_edges_seen,
                    )
                )
                continue
            local_hashes.add(graph_hash)

            (
                components,
                total,
                weighted,
                score_status,
                score_seconds,
                prune_lower_bound,
            ) = _score_edges_cascade(
                request.parent.order,
                edges,
                request.incumbent_total,
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
                    scored_lengths=tuple(c.length for c in components),
                    prune_lower_bound=prune_lower_bound,
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
                _failure_result(
                    task=task,
                    request=request,
                    status="ERROR",
                    mutation_seconds=time.perf_counter() - mutation_started,
                    failure=f"{type(exc).__name__}: {exc}",
                )
            )

    return tuple(results)


def score_root(
    graph: Graph,
    witness_cap: int,
    node_budget: int,
    root_node_budget: int,
) -> ExactEntry:
    """Obtain an exact bootstrap score without inflating candidate budgets.

    Each forbidden length is scored separately. If a component exhausts the
    normal candidate node budget, retry only that component with geometrically
    increasing budgets, up to --root-node-budget.
    """
    lengths = tuple(int(length) for length in forbidden_lengths(graph.order))
    bit_graph = BitGraph.from_edges(graph.order, graph.edges)
    components: list[tuple[int, int]] = []

    with ScoreWorker() as scorer:
        for length in lengths:
            budget = node_budget
            while True:
                response = scorer.score(
                    bit_graph,
                    lengths=(length,),
                    witness_cap=witness_cap,
                    node_budget=budget,
                )
                if len(response.results) != 1:
                    raise RuntimeError(
                        f"root scorer returned {len(response.results)} results for C{length}"
                    )
                result = response.results[0]
                count = int(result.count)
                complete = bool(result.complete)

                if count >= witness_cap:
                    raise RuntimeError(
                        f"start graph score hit witness cap at C{length}: "
                        f"count={count} cap={witness_cap}; use a larger "
                        "--witness-cap or another start graph"
                    )

                if complete:
                    components.append((length, count))
                    break

                if budget >= root_node_budget:
                    raise RuntimeError(
                        f"start graph score is not exact at C{length}: "
                        f"count={count} complete=False node_budget={budget} "
                        f"root_node_budget={root_node_budget}"
                    )

                next_budget = min(root_node_budget, max(budget + 1, budget * 4))
                console.print(
                    f"ROOT RETRY C{length} count>={count} "
                    f"budget={budget:,}->{next_budget:,}"
                )
                budget = next_budget

    total = sum(count for _, count in components)
    weighted = sum(weight(length) * count for length, count in components)

    return ExactEntry(
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
    root: ExactEntry,
    reservoir: list[ReservoirEntry],
    elite: list[ExactEntry],
    phase_name: str,
    root_probability: float,
) -> ParentPayload:
    if phase_name == "ELITE" and elite:
        return rng.choice(elite).parent_payload()

    if rng.random() < root_probability:
        return root.parent_payload()
    if reservoir:
        return rng.choice(reservoir).parent_payload()
    return root.parent_payload()


def update_elite(elite: list[ExactEntry], entry: ExactEntry, capacity: int) -> None:
    by_hash = {item.graph_hash: item for item in elite}
    by_hash[entry.graph_hash] = entry
    elite[:] = sorted(by_hash.values(), key=rank_key)[:capacity]


def update_reservoir(
    reservoir: list[ReservoirEntry],
    reservoir_hashes: set[str],
    entry: ReservoirEntry,
    *,
    capacity: int,
    stream_seen: int,
    rng: random.Random,
) -> bool:
    """Bounded reservoir sampling without unbounded all-history dedupe.

    `stream_seen` counts legal scored candidates observed by the parent process.
    The hash set mirrors ONLY the current reservoir, so memory is O(capacity).
    Returning False means the candidate was already present in the reservoir or
    was not selected by reservoir sampling.
    """
    if entry.graph_hash in reservoir_hashes:
        return False

    if len(reservoir) < capacity:
        reservoir.append(entry)
        reservoir_hashes.add(entry.graph_hash)
        return True

    slot = rng.randrange(stream_seen)
    if slot >= capacity:
        return False

    old = reservoir[slot]
    reservoir_hashes.discard(old.graph_hash)
    reservoir[slot] = entry
    reservoir_hashes.add(entry.graph_hash)
    return True


def result_to_exact_entry(result: CandidateResult) -> ExactEntry:
    if (
        not result.fully_exact
        or result.edges is None
        or result.graph_hash is None
        or result.total is None
        or result.weighted is None
    ):
        raise ValueError("candidate is not fully exact")
    return ExactEntry(
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


def save_entry(path: Path, entry: ExactEntry, *, metadata: dict[str, object]) -> None:
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


def append_hit(path: Path, entry: ExactEntry, *, elapsed: float, evaluated: int) -> None:
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
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def save_pool(
    path: Path,
    *,
    root: ExactEntry,
    best: ExactEntry,
    elite: list[ExactEntry],
    reservoir: list[ReservoirEntry],
    metadata: dict[str, object],
) -> None:
    def exact_compact(entry: ExactEntry) -> dict[str, object]:
        return {
            "order": entry.order,
            "edge_count": entry.edge_count,
            "graph_hash": entry.graph_hash,
            "total": entry.total,
            "weighted": entry.weighted,
            "components": {str(k): v for k, v in entry.components},
            "edges": [list(e) for e in entry.edges],
        }

    def reservoir_compact(entry: ReservoirEntry) -> dict[str, object]:
        return {
            "order": entry.order,
            "edge_count": entry.edge_count,
            "graph_hash": entry.graph_hash,
            "edges": [list(e) for e in entry.edges],
        }

    payload = {
        "schema_version": "heg.random_cascade_budget.pool.v1",
        "metadata": metadata,
        "root": exact_compact(root),
        "best": exact_compact(best),
        "elite": [exact_compact(x) for x in elite],
        "reservoir": [reservoir_compact(x) for x in reservoir],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root_graph = load_graph(args.start_graph, args.expected_order)
    root = score_root(
        root_graph,
        args.witness_cap,
        args.node_budget,
        args.root_node_budget,
    )

    lengths = tuple(int(x) for x in forbidden_lengths(root.order))
    min_edges = math.ceil(3 * root.order / 2)
    max_edges_label = "none" if args.max_edges == 0 else str(args.max_edges)

    console.print(
        f"HEG CASCADE order={root.order} lengths={lengths} workers={args.workers} "
        f"candidates/worker={args.candidates_per_worker}"
    )
    console.print(
        f"mutation=cycle-blind ADD/REMOVE walk={args.walk_min}..{args.walk_max} "
        f"edge_range={min_edges}..{max_edges_label}"
    )
    console.print(
        f"budget={args.evaluation_budget:,} evaluated "
        f"phase={args.phase_evaluations:,} evaluated "
        f"emergency_time={'off' if args.max_seconds == 0 else f'{args.max_seconds:.0f}s'}"
    )
    console.print(
        f"START total={root.total} weighted={root.weighted} "
        f"{component_text(root.components)} m={root.edge_count} hash={root.graph_hash[:8]}"
    )

    if args.save_hits.exists():
        args.save_hits.unlink()

    metadata: dict[str, object] = {
        "kind": "cycle_blind_random_legal_walk_cascade_budget_v2",
        "implementation_version": 2,
        "order": root.order,
        "forbidden_lengths": list(lengths),
        "workers": args.workers,
        "candidates_per_worker": args.candidates_per_worker,
        "walk_min": args.walk_min,
        "walk_max": args.walk_max,
        "max_edges": args.max_edges,
        "evaluation_budget": args.evaluation_budget,
        "phase_evaluations": args.phase_evaluations,
        "root_parent_probability_random": args.root_parent_prob,
        "reservoir_size": args.reservoir_size,
        "elite_size": args.elite_size,
        "seed": args.seed,
        "node_budget": args.node_budget,
        "root_node_budget": args.root_node_budget,
        "witness_cap": args.witness_cap,
        "cascade_rule": "score ascending forbidden lengths; prune only if proven partial TOTAL > dispatch incumbent",
        "pid": os.getpid(),
    }

    best = root
    elite: list[ExactEntry] = [root]
    reservoir: list[ReservoirEntry] = [
        ReservoirEntry(root.order, root.edges, root.graph_hash)
    ]
    # Critical long-run rule: do not retain every pruned candidate hash.
    # exact_hashes stays small (only fully exact candidates), while
    # reservoir_hashes is bounded by --reservoir-size.
    exact_hashes: set[str] = {root.graph_hash}
    reservoir_hashes: set[str] = {root.graph_hash}
    reservoir_stream_seen = 1
    reservoir_insertions = 1
    rng = random.Random(args.seed)

    evaluated = 0
    exact = 0
    pruned = 0
    nonexact_budget = 0
    nonexact_cap = 0
    duplicates = 0
    mutation_failures = 0
    errors = 0
    low_logged = 0
    rounds = 0
    request_counter = 0
    task_counter = 0

    phase_counts: Counter[str] = Counter()
    phase_exact: Counter[str] = Counter()
    phase_pruned: Counter[str] = Counter()
    length_calls: Counter[int] = Counter()
    length_nodes: Counter[int] = Counter()
    length_ns: Counter[int] = Counter()
    prune_stage: Counter[int] = Counter()
    edge_hist: Counter[int] = Counter({root.edge_count: 1})

    started = time.perf_counter()
    deadline = None if args.max_seconds == 0 else started + args.max_seconds
    next_report_eval = args.report_evaluations
    next_report_time = started + args.report_seconds
    current_phase_index: int | None = None
    stop_reason = "evaluation budget exhausted"

    save_entry(args.save_best, best, metadata=metadata)

    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_worker_init,
        initargs=(args.witness_cap, args.node_budget),
    ) as executor:
        while evaluated < args.evaluation_budget:
            now = time.perf_counter()
            if deadline is not None and now >= deadline:
                stop_reason = "emergency wall-clock cap exhausted"
                break

            phase_index = evaluated // args.phase_evaluations
            phase_name = "RANDOM" if phase_index % 2 == 0 else "ELITE"
            if phase_index != current_phase_index:
                console.print(
                    f"PHASE {phase_name} epoch={phase_index} evaluated={evaluated:,} "
                    f"best={best.total} elite={len(elite)} reservoir={len(reservoir)}"
                )
                current_phase_index = phase_index

            # Avoid gross budget overshoot.  Failed/duplicate mutations do not
            # consume evaluated budget, so more rounds may still be necessary.
            target_requests = min(
                args.workers * args.candidates_per_worker,
                args.evaluation_budget - evaluated,
            )

            requests_all: list[CandidateRequest] = []
            for _ in range(target_requests):
                parent = choose_parent(
                    rng=rng,
                    root=root,
                    reservoir=reservoir,
                    elite=elite,
                    phase_name=phase_name,
                    root_probability=args.root_parent_prob,
                )
                requests_all.append(
                    CandidateRequest(
                        request_id=request_counter,
                        parent=parent,
                        seed=rng.getrandbits(63),
                        incumbent_total=best.total,
                    )
                )
                request_counter += 1

            tasks: list[WorkerBatchTask] = []
            cursor = 0
            for _slot in range(args.workers):
                if cursor >= len(requests_all):
                    break
                chunk = tuple(
                    requests_all[cursor:cursor + args.candidates_per_worker]
                )
                cursor += len(chunk)
                tasks.append(
                    WorkerBatchTask(
                        task_id=task_counter,
                        requests=chunk,
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
            round_results.sort(key=lambda r: (r.task_id, r.request_id))
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
                    if errors <= 10:
                        console.print(
                            f"worker error task={result.task_id} req={result.request_id}: "
                            f"{result.failure}"
                        )
                    continue

                if not result.reached_scorer:
                    continue

                evaluated += 1
                phase_counts[phase_name] += 1

                for comp in result.components:
                    length_calls[comp.length] += 1
                    length_nodes[comp.length] += comp.nodes
                    length_ns[comp.length] += comp.elapsed_ns

                if result.score_status == STATUS_EXACT:
                    exact += 1
                    phase_exact[phase_name] += 1
                elif result.score_status == STATUS_PRUNED:
                    pruned += 1
                    phase_pruned[phase_name] += 1
                    if result.components:
                        prune_stage[result.components[-1].length] += 1
                elif result.score_status == STATUS_BUDGET:
                    nonexact_budget += 1
                elif result.score_status == STATUS_SATURATED:
                    nonexact_cap += 1

                if result.edges is None or result.graph_hash is None:
                    # Scored results should carry the legal graph.
                    if result.score_status not in {STATUS_BUDGET, STATUS_SATURATED}:
                        errors += 1
                    continue

                reservoir_stream_seen += 1
                edge_hist[len(result.edges)] += 1

                # RANDOM reservoir is deliberately score-blind.  We sample from
                # the whole legal scored stream, but deduplicate only against the
                # CURRENT bounded reservoir.  This removes the old O(N)
                # seen_hashes set that reached tens of millions of strings.
                inserted = update_reservoir(
                    reservoir,
                    reservoir_hashes,
                    ReservoirEntry(result.order, result.edges, result.graph_hash),
                    capacity=args.reservoir_size,
                    stream_seen=reservoir_stream_seen,
                    rng=rng,
                )
                if inserted:
                    reservoir_insertions += 1

                if result.fully_exact:
                    # Exact results are rare, so keeping their hashes is cheap and
                    # prevents duplicate elite/log bookkeeping.
                    if result.graph_hash in exact_hashes:
                        duplicates += 1
                        continue
                    exact_hashes.add(result.graph_hash)
                    entry = result_to_exact_entry(result)
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
                        save_entry(args.save_best, best, metadata=metadata)
                        console.print(
                            f"NEW BEST total={best.total} ({best.total-old.total:+d}) "
                            f"weighted={best.weighted} {component_text(best.components)} "
                            f"m={best.edge_count} eval={evaluated:,} phase={phase_name} "
                            f"hash={best.graph_hash[:8]}"
                        )

                    if entry.total <= args.success_total:
                        stop_reason = (
                            f"success: exact TOTAL={entry.total} <= {args.success_total}"
                        )
                        found_success = True

                if evaluated >= args.evaluation_budget or found_success:
                    break

            now = time.perf_counter()
            if (
                evaluated >= next_report_eval
                or now >= next_report_time
                or found_success
            ):
                elapsed = now - started
                rate = evaluated / elapsed if elapsed > 0 else 0.0
                call_text = " ".join(
                    f"C{L}:{length_calls[L]:,}"
                    for L in lengths
                )
                prune_text = " ".join(
                    f"C{L}:{prune_stage[L]:,}"
                    for L in lengths
                    if prune_stage[L]
                ) or "none"
                console.print(
                    f"STATUS eval={evaluated:,}/{args.evaluation_budget:,} "
                    f"t={elapsed:.1f}s phase={phase_name} rate={rate:.1f}/s "
                    f"exact={exact:,} pruned={pruned:,} "
                    f"budget={nonexact_budget:,} cap={nonexact_cap:,} "
                    f"exact_unique={len(exact_hashes):,} reservoir_seen={reservoir_stream_seen:,} "
                    f"reservoir_ins={reservoir_insertions:,} dup={duplicates:,} errors={errors:,}"
                )
                console.print(
                    f"       best TOTAL={best.total} weighted={best.weighted} "
                    f"{component_text(best.components)} m={best.edge_count} "
                    f"elite={len(elite)} reservoir={len(reservoir)} low_logged={low_logged}"
                )
                console.print(f"       scorer_calls {call_text}")
                console.print(f"       prune_stage {prune_text}")

                while next_report_eval <= evaluated:
                    next_report_eval += args.report_evaluations
                while next_report_time <= now:
                    next_report_time += args.report_seconds

            if found_success:
                break

    elapsed = time.perf_counter() - started
    avoided_calls = {}
    for i, length in enumerate(lengths):
        if i == 0:
            avoided_calls[str(length)] = 0
        else:
            avoided_calls[str(length)] = evaluated - int(length_calls[length])

    metadata.update(
        {
            "elapsed_seconds": elapsed,
            "rounds": rounds,
            "evaluated": evaluated,
            "exact": exact,
            "certified_pruned": pruned,
            "nonexact_budget": nonexact_budget,
            "nonexact_cap": nonexact_cap,
            "duplicates": duplicates,
            "mutation_failures": mutation_failures,
            "errors": errors,
            "exact_unique": len(exact_hashes),
            "reservoir_stream_seen": reservoir_stream_seen,
            "reservoir_insertions": reservoir_insertions,
            "dedupe_policy": "local batch + exact hashes + current reservoir only; no all-history pruned hash set",
            "stop_reason": stop_reason,
            "scorer_calls_by_length": {
                str(k): int(v) for k, v in sorted(length_calls.items())
            },
            "scorer_nodes_by_length": {
                str(k): int(v) for k, v in sorted(length_nodes.items())
            },
            "scorer_elapsed_ns_by_length": {
                str(k): int(v) for k, v in sorted(length_ns.items())
            },
            "avoided_scorer_calls_by_length": avoided_calls,
            "prune_stage": {
                str(k): int(v) for k, v in sorted(prune_stage.items())
            },
            "phase_evaluated": dict(phase_counts),
            "phase_exact": dict(phase_exact),
            "phase_pruned": dict(phase_pruned),
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
    args.save_summary.parent.mkdir(parents=True, exist_ok=True)
    args.save_summary.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    console.print(stop_reason)
    console.print(
        f"DONE best_total={best.total} weighted={best.weighted} "
        f"{component_text(best.components)} m={best.edge_count} "
        f"elapsed={elapsed:.2f}s evaluated={evaluated:,} exact={exact:,} "
        f"pruned={pruned:,} hash={best.graph_hash[:8]}"
    )
    console.print(
        "CALLS "
        + " ".join(f"C{L}={length_calls[L]:,}" for L in lengths)
    )
    console.print(f"Best graph saved: {args.save_best}")
    console.print(f"Summary saved: {args.save_summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
