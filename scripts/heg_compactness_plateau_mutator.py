#!/usr/bin/env python3
"""
Experimental HEG plateau search with forbidden-cycle compactness elite ranking.

This is deliberately a thin adapter around heg_random_alternating_mutator.py.
The mutation kernel, exact HEG scorer, reservoir, success criterion, and global
best semantics are unchanged.  Only ELITE maintenance is replaced when a
compactness metric is enabled.

Primary objective is always exact

    F(G) = sum C_{2^k}(G).

For candidates with F <= --compactness-threshold, ELITE ties at the same F can
be ranked by one of three geometry metrics:

  cycle-min
      Minimize the mean, over unordered forbidden-cycle pairs, of the minimum
      graph distance between the two vertex sets.

  vertex-mean
      Minimize the mean, over cycle pairs, of the mean of all vertex-to-vertex
      graph distances between the two cycles.

  edge-potential
      Maximize a normalized attraction potential over all pairs of cycle
      edges. Edge distance is line-graph-like: the same edge has distance 0;
      distinct edges have 1 + the minimum endpoint-to-endpoint graph distance.
      Attraction is 1/(1+d_edge).  The implementation stores -potential as the
      minimization energy.

Each cycle pair is normalized before the graph-level mean, so a C8 does not
receive four times the geometry weight of a C4 merely because it contains more
vertex/edge pairs.

The generic exact cycle enumerator is intentionally Python and intended for
hypothesis testing on small/medium orders first.  It verifies its witness count
against the authoritative exact score already attached to the candidate.  A
node budget prevents geometry tie-breaking from stalling the underlying search;
if exhausted, that candidate falls back behind geometry-scored candidates at
that same F.  If the hypothesis works, witness extraction should later move
into the C++ scorer rather than keeping this Python enumerator in production.

Example:

  uv run python scripts/heg_compactness_plateau_mutator.py \
    --compactness-metric vertex-mean --compactness-threshold 4 \
    --start-graph seed.json --expected-order 10 \
    --selection-mode alternating --phase-seconds 5 \
    --total-seconds 30 --success-total 4
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import math
import statistics
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


METRICS = ("baseline", "cycle-min", "vertex-mean", "edge-potential")
PLACEMENTS = ("before-weighted", "after-weighted")


class GeometryBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GeometryScore:
    metric: str
    energy: float
    cycle_count: int
    pair_count: int
    enumeration_nodes: int


@dataclass(slots=True)
class GeometryStats:
    requested: int = 0
    cache_hits: int = 0
    computed: int = 0
    budget_exhausted: int = 0
    mismatches: int = 0
    total_seconds: float = 0.0
    enumeration_nodes: int = 0


def _load_base_module() -> Any:
    path = Path(__file__).with_name("heg_random_alternating_mutator.py")
    if not path.is_file():
        raise RuntimeError(f"required sibling script is missing: {path}")
    spec = importlib.util.spec_from_file_location("heg_random_alternating_mutator_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses and a few introspection paths expect the module to be present.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse_adapter_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--compactness-self-test", action="store_true")
    parser.add_argument("--compactness-metric", choices=METRICS, default="baseline")
    parser.add_argument(
        "--compactness-threshold",
        type=int,
        default=4,
        help="Apply geometry elite ranking only when exact TOTAL/F <= this value.",
    )
    parser.add_argument(
        "--compactness-placement",
        choices=PLACEMENTS,
        default="before-weighted",
        help=(
            "before-weighted: at equal F, geometry outranks the legacy weighted "
            "tie-break; after-weighted: geometry only resolves equal F+weighted."
        ),
    )
    parser.add_argument(
        "--geometry-node-budget",
        type=int,
        default=5_000_000,
        help="Maximum DFS expansion nodes for exact forbidden-cycle enumeration per graph.",
    )
    parser.add_argument(
        "--compactness-stats",
        type=Path,
        default=None,
        help="Optional JSON diagnostics path for geometry ranking overhead.",
    )
    args, remaining = parser.parse_known_args(list(argv))
    if args.compactness_threshold < 0:
        parser.error("--compactness-threshold must be >= 0")
    if args.geometry_node_budget < 1:
        parser.error("--geometry-node-budget must be >= 1")
    return args, remaining


def _adjacency(order: int, edges: Iterable[tuple[int, int]]) -> list[tuple[int, ...]]:
    raw: list[list[int]] = [[] for _ in range(order)]
    for u, v in edges:
        raw[u].append(v)
        raw[v].append(u)
    return [tuple(sorted(row)) for row in raw]


def _enumerate_cycles_exact(
    adj: Sequence[Sequence[int]],
    length: int,
    *,
    expected_count: int,
    remaining_budget: list[int],
) -> tuple[list[tuple[int, ...]], int]:
    """Enumerate simple undirected cycles exactly once.

    Canonicalization mirrors the scorer's logic: the start vertex is the
    minimum vertex on the cycle, and one of the two orientations is retained
    by requiring first_neighbor < last_neighbor.
    """
    n = len(adj)
    if length < 3 or length > n:
        return [], 0

    found: list[tuple[int, ...]] = []
    nodes = 0
    path: list[int] = []
    seen: set[int] = set()

    def consume_node() -> None:
        nonlocal nodes
        nodes += 1
        remaining_budget[0] -= 1
        if remaining_budget[0] < 0:
            raise GeometryBudgetExceeded

    def dfs(start: int, current: int) -> None:
        consume_node()
        if len(path) == length:
            if start in adj[current] and path[1] < current:
                found.append(tuple(path))
                if len(found) > expected_count:
                    raise RuntimeError(
                        f"cycle witness count exceeded authoritative score for C{length}: "
                        f"expected {expected_count}, found >{expected_count}"
                    )
            return

        for nxt in adj[current]:
            # start must be the minimum vertex in this undirected cycle.
            if nxt <= start or nxt in seen:
                continue
            seen.add(nxt)
            path.append(nxt)
            dfs(start, nxt)
            path.pop()
            seen.remove(nxt)

    for start in range(n):
        path[:] = [start]
        seen.clear()
        seen.add(start)
        for first in adj[start]:
            if first <= start:
                continue
            seen.add(first)
            path.append(first)
            dfs(start, first)
            path.pop()
            seen.remove(first)

    if len(found) != expected_count:
        raise RuntimeError(
            f"cycle witness mismatch at C{length}: authoritative={expected_count}, "
            f"enumerated={len(found)}"
        )
    return found, nodes


def _all_pairs_distances(adj: Sequence[Sequence[int]]) -> list[list[int]]:
    n = len(adj)
    result: list[list[int]] = []
    for source in range(n):
        distance = [-1] * n
        distance[source] = 0
        q: deque[int] = deque([source])
        while q:
            u = q.popleft()
            nd = distance[u] + 1
            for v in adj[u]:
                if distance[v] != -1:
                    continue
                distance[v] = nd
                q.append(v)
        if any(d < 0 for d in distance):
            raise RuntimeError("compactness metric received a disconnected graph")
        result.append(distance)
    return result


def _cycle_edges(cycle: Sequence[int]) -> tuple[tuple[int, int], ...]:
    out = []
    for i, u in enumerate(cycle):
        v = cycle[(i + 1) % len(cycle)]
        out.append((u, v) if u < v else (v, u))
    return tuple(out)


def _edge_distance(
    left: tuple[int, int],
    right: tuple[int, int],
    dist: Sequence[Sequence[int]],
) -> int:
    if left == right:
        return 0
    a, b = left
    x, y = right
    return 1 + min(dist[a][x], dist[a][y], dist[b][x], dist[b][y])


def _compute_geometry(
    entry: Any,
    *,
    metric: str,
    node_budget: int,
) -> GeometryScore:
    adj = _adjacency(entry.order, entry.edges)
    budget = [node_budget]
    cycles: list[tuple[int, ...]] = []
    nodes = 0

    for length, count in entry.components:
        count = int(count)
        if count <= 0:
            continue
        witnesses, used = _enumerate_cycles_exact(
            adj,
            int(length),
            expected_count=count,
            remaining_budget=budget,
        )
        nodes += used
        cycles.extend(witnesses)

    if len(cycles) != int(entry.total):
        raise RuntimeError(
            f"forbidden-cycle witness total mismatch: score={entry.total}, enumerated={len(cycles)}"
        )

    pair_count = len(cycles) * (len(cycles) - 1) // 2
    if pair_count == 0:
        return GeometryScore(metric, 0.0, len(cycles), pair_count, nodes)

    dist = _all_pairs_distances(adj)
    pair_values: list[float] = []

    if metric == "cycle-min":
        for i in range(len(cycles)):
            for j in range(i + 1, len(cycles)):
                pair_values.append(
                    float(min(dist[u][v] for u in cycles[i] for v in cycles[j]))
                )
        energy = statistics.fmean(pair_values)

    elif metric == "vertex-mean":
        for i in range(len(cycles)):
            for j in range(i + 1, len(cycles)):
                pair_values.append(
                    statistics.fmean(dist[u][v] for u in cycles[i] for v in cycles[j])
                )
        energy = statistics.fmean(pair_values)

    elif metric == "edge-potential":
        edge_sets = [_cycle_edges(cycle) for cycle in cycles]
        for i in range(len(edge_sets)):
            for j in range(i + 1, len(edge_sets)):
                attractions = [
                    1.0 / (1.0 + _edge_distance(e, f, dist))
                    for e in edge_sets[i]
                    for f in edge_sets[j]
                ]
                pair_values.append(statistics.fmean(attractions))
        # Higher attraction = more compact; elite sorter minimizes energy.
        energy = -statistics.fmean(pair_values)

    else:
        raise ValueError(f"unsupported compactness metric: {metric}")

    if not math.isfinite(energy):
        raise RuntimeError("non-finite compactness energy")
    return GeometryScore(metric, float(energy), len(cycles), pair_count, nodes)


def _self_test() -> None:
    class Entry:
        order = 4
        edges = tuple((u, v) for u in range(4) for v in range(u + 1, 4))
        components = ((4, 3),)
        total = 3
        weighted = 48
        graph_hash = "self-test-k4"

        @property
        def edge_count(self) -> int:
            return len(self.edges)

    for metric in ("cycle-min", "vertex-mean", "edge-potential"):
        score = _compute_geometry(Entry(), metric=metric, node_budget=100_000)
        if score.cycle_count != 3 or score.pair_count != 3 or not math.isfinite(score.energy):
            raise RuntimeError(f"self-test failed for {metric}: {score}")
    print("COMPACTNESS_SELF_TEST: OK")


def main() -> int:
    adapter, remaining = _parse_adapter_args(sys.argv[1:])
    if adapter.compactness_self_test:
        _self_test()
        return 0
    sys.argv = [sys.argv[0], *remaining]
    base = _load_base_module()

    if adapter.compactness_metric == "baseline":
        base.console.print("COMPACTNESS metric=baseline; legacy elite ranking unchanged")
        return int(base.main())

    cache: dict[str, GeometryScore | None] = {}
    stats = GeometryStats()
    failures: Counter[str] = Counter()
    best_energy_by_total: dict[int, float] = {}

    def geometry_for(entry: Any) -> GeometryScore | None:
        stats.requested += 1
        if entry.graph_hash in cache:
            stats.cache_hits += 1
            return cache[entry.graph_hash]

        started = time.perf_counter()
        try:
            score = _compute_geometry(
                entry,
                metric=adapter.compactness_metric,
                node_budget=adapter.geometry_node_budget,
            )
        except GeometryBudgetExceeded:
            stats.budget_exhausted += 1
            failures["budget"] += 1
            score = None
        except RuntimeError as exc:
            stats.mismatches += 1
            failures[str(exc)] += 1
            # A witness/count mismatch is a correctness problem, not a harmless
            # missing tie-break. Fail fast so the experiment cannot silently
            # rank with inconsistent geometry.
            raise
        finally:
            stats.total_seconds += time.perf_counter() - started

        if score is not None:
            stats.computed += 1
            stats.enumeration_nodes += score.enumeration_nodes
            old = best_energy_by_total.get(int(entry.total))
            if old is None or score.energy < old:
                best_energy_by_total[int(entry.total)] = score.energy
        cache[entry.graph_hash] = score
        return score

    def compact_rank(entry: Any) -> tuple[Any, ...]:
        # F is ALWAYS the first key. No geometry can make F=4 outrank F=3.
        total = int(entry.total)
        if total > adapter.compactness_threshold:
            return (total, 1, 0.0, int(entry.weighted), entry.edge_count, entry.graph_hash)

        score = geometry_for(entry)
        if score is None:
            available = 1
            energy = 0.0
        else:
            available = 0
            energy = score.energy

        if adapter.compactness_placement == "before-weighted":
            return (
                total,
                available,
                energy,
                int(entry.weighted),
                entry.edge_count,
                entry.graph_hash,
            )
        return (
            total,
            int(entry.weighted),
            available,
            energy,
            entry.edge_count,
            entry.graph_hash,
        )

    def update_elite(elite: list[Any], entry: Any, capacity: int) -> None:
        by_hash = {item.graph_hash: item for item in elite}
        previous = by_hash.get(entry.graph_hash)
        if previous is None or base.rank_key(entry) < base.rank_key(previous):
            by_hash[entry.graph_hash] = entry
        elite[:] = sorted(by_hash.values(), key=compact_rank)[:capacity]

    base.update_elite = update_elite

    def write_stats() -> None:
        payload = {
            "schema_version": "graphoratory.heg_compactness_elite.v1",
            "metric": adapter.compactness_metric,
            "threshold": adapter.compactness_threshold,
            "placement": adapter.compactness_placement,
            "geometry_node_budget": adapter.geometry_node_budget,
            "requested": stats.requested,
            "cache_hits": stats.cache_hits,
            "computed": stats.computed,
            "budget_exhausted": stats.budget_exhausted,
            "mismatches": stats.mismatches,
            "geometry_seconds": stats.total_seconds,
            "enumeration_nodes": stats.enumeration_nodes,
            "best_energy_by_total": {
                str(k): v for k, v in sorted(best_energy_by_total.items())
            },
            "failures": dict(failures),
        }
        base.console.print(
            "COMPACTNESS_STATS "
            f"metric={adapter.compactness_metric} computed={stats.computed:,} "
            f"cache_hits={stats.cache_hits:,} budget={stats.budget_exhausted:,} "
            f"geometry_s={stats.total_seconds:.3f} mismatches={stats.mismatches}"
        )
        if adapter.compactness_stats is not None:
            adapter.compactness_stats.parent.mkdir(parents=True, exist_ok=True)
            adapter.compactness_stats.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    atexit.register(write_stats)
    base.console.print(
        "COMPACTNESS "
        f"metric={adapter.compactness_metric} threshold={adapter.compactness_threshold} "
        f"placement={adapter.compactness_placement} "
        f"geometry_node_budget={adapter.geometry_node_budget:,}; "
        "mutation/scoring/global-best remain unchanged"
    )
    return int(base.main())


if __name__ == "__main__":
    raise SystemExit(main())
