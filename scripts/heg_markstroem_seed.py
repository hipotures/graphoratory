#!/usr/bin/env python3
"""
Build and exactly score the canonical 24-vertex Markström graph.

The construction below is transcribed from the current SageMath implementation
of graphs.MarkstroemGraph(). It deliberately does NOT depend on SageMath.

Sage construction:
    - cycle 0..8
    - paths:
        0-9-10-11-2-1-11
        3-12-13-14-5-4-14
        6-15-16-17-8-7-17
    - triangles:
        10-9-18-10
        12-13-19-12
        15-16-20-15
        21-22-23-21
    - extra edges:
        19-22, 18-21, 20-23

Known external properties:
    order = 24
    edges = 36
    cubic
    planar
    no C4
    no C8
    at least one C16

This script:
    1. reconstructs the graph;
    2. checks basic structural invariants locally;
    3. validates Graphoratory's scientific invariants;
    4. runs the same authoritative ScoreWorker used by the HEG experiments;
    5. saves a JSON directly usable by:

       uv run python scripts/heg_fixed_order_coupled_switch_v2.py \
         --start-graph markstroem_n24.json ...

Run:
    uv run python scripts/heg_markstroem_seed.py

Optional:
    uv run python scripts/heg_markstroem_seed.py \
      --output markstroem_n24.json \
      --witness-cap 1000000 \
      --node-budget 10000000
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.table import Table
from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import forbidden_lengths  # type: ignore[import-untyped]

from graphoratory.graphs import Graph
from graphoratory.science.worker import ScoreWorker


Edge = tuple[int, int]
MAX_ORDER = 128

STATUS_EXACT = "EXACT"
STATUS_SATURATED = "SATURATED_AT_CAP"
STATUS_BUDGET = "SEARCH_BUDGET_EXHAUSTED"

console = Console()


def norm_edge(u: int, v: int) -> Edge:
    if u == v:
        raise ValueError(f"self-loop {u}-{v}")
    return (u, v) if u < v else (v, u)


def add_edge(edges: set[Edge], u: int, v: int) -> None:
    edge = norm_edge(u, v)
    edges.add(edge)


def add_path(edges: set[Edge], vertices: Iterable[int]) -> None:
    path = list(vertices)
    for u, v in zip(path, path[1:]):
        add_edge(edges, u, v)


def add_cycle(edges: set[Edge], vertices: Iterable[int]) -> None:
    cycle = list(vertices)
    if len(cycle) < 3:
        raise ValueError("cycle must contain at least 3 vertices")
    add_path(edges, cycle)
    add_edge(edges, cycle[-1], cycle[0])


def build_markstroem_graph() -> Graph:
    """
    Exact topology used by SageMath's MarkstroemGraph().
    """
    edges: set[Edge] = set()

    add_cycle(edges, range(9))

    add_path(edges, [0, 9, 10, 11, 2, 1, 11])
    add_path(edges, [3, 12, 13, 14, 5, 4, 14])
    add_path(edges, [6, 15, 16, 17, 8, 7, 17])

    add_cycle(edges, [10, 9, 18])
    add_cycle(edges, [12, 13, 19])
    add_cycle(edges, [15, 16, 20])
    add_cycle(edges, [21, 22, 23])

    add_edge(edges, 19, 22)
    add_edge(edges, 18, 21)
    add_edge(edges, 20, 23)

    graph = Graph.from_edges(24, sorted(edges))
    return graph


def degree_sequence(graph: Graph) -> tuple[int, ...]:
    degrees = [0] * graph.order
    for u, v in graph.edges:
        degrees[u] += 1
        degrees[v] += 1
    return tuple(degrees)


def exact_c4(graph: Graph) -> int:
    """
    Exact C4 count:
        C4 = 1/2 * sum_{u<v} binom(|N(u) ∩ N(v)|, 2)
    """
    adjacency = [0] * graph.order
    for u, v in graph.edges:
        adjacency[u] |= 1 << v
        adjacency[v] |= 1 << u

    doubled = 0
    for u in range(graph.order):
        for v in range(u + 1, graph.order):
            common = (adjacency[u] & adjacency[v]).bit_count()
            doubled += common * (common - 1) // 2

    if doubled % 2:
        raise RuntimeError(f"odd doubled C4 count: {doubled}")
    return doubled // 2


def weight(length: int) -> int:
    return max(1, 64 // length)


def score_graph(
    graph: Graph,
    *,
    witness_cap: int,
    node_budget: int,
) -> tuple[list[dict[str, object]], int | None, int | None, float]:
    lengths = tuple(int(length) for length in forbidden_lengths(graph.order))
    bit_graph = BitGraph.from_edges(graph.order, graph.edges)

    started = time.perf_counter()
    with ScoreWorker() as worker:
        response = worker.score(
            bit_graph,
            lengths=lengths,
            witness_cap=witness_cap,
            node_budget=node_budget,
        )
    elapsed = time.perf_counter() - started

    by_length = {int(result.length): result for result in response.results}
    if set(by_length) != set(lengths):
        raise RuntimeError(
            f"scorer returned {sorted(by_length)}, expected {list(lengths)}"
        )

    components: list[dict[str, object]] = []
    fully_exact = True

    for length in lengths:
        result = by_length[length]
        raw_count = int(result.count)

        if raw_count >= witness_cap:
            observed = witness_cap
            status = STATUS_SATURATED
            fully_exact = False
        elif bool(result.complete):
            observed = raw_count
            status = STATUS_EXACT
        else:
            observed = raw_count
            status = STATUS_BUDGET
            fully_exact = False

        components.append(
            {
                "length": length,
                "observed": observed,
                "status": status,
                "nodes": int(result.nodes),
                "elapsed_ns": int(result.elapsed_ns),
            }
        )

    total: int | None
    weighted: int | None
    if fully_exact:
        total = sum(int(component["observed"]) for component in components)
        weighted = sum(
            weight(int(component["length"])) * int(component["observed"])
            for component in components
        )
    else:
        total = None
        weighted = None

    return components, total, weighted, elapsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("markstroem_n24.json"),
    )
    parser.add_argument("--witness-cap", type=int, default=1_000_000)
    parser.add_argument("--node-budget", type=int, default=10_000_000)
    args = parser.parse_args()

    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")

    return args


def main() -> int:
    args = parse_args()

    graph = build_markstroem_graph()
    degrees = degree_sequence(graph)

    if graph.order != 24:
        raise RuntimeError(f"unexpected order {graph.order}")
    if len(graph.edges) != 36:
        raise RuntimeError(f"unexpected edge count {len(graph.edges)}")
    if set(degrees) != {3}:
        raise RuntimeError(f"graph is not cubic: degrees={degrees}")

    graph.validate_scientific_invariants(max_order=MAX_ORDER)

    c4_formula = exact_c4(graph)
    if c4_formula != 0:
        raise RuntimeError(
            f"Sage-transcribed Markström graph unexpectedly has C4={c4_formula}"
        )

    components, total, weighted, elapsed = score_graph(
        graph,
        witness_cap=args.witness_cap,
        node_budget=args.node_budget,
    )

    component_map = {
        int(component["length"]): component for component in components
    }

    c4_worker = component_map.get(4)
    if (
        c4_worker is not None
        and c4_worker["status"] == STATUS_EXACT
        and int(c4_worker["observed"]) != c4_formula
    ):
        raise RuntimeError(
            "C4 formula / ScoreWorker disagreement: "
            f"{c4_formula} != {c4_worker['observed']}"
        )

    # Sage's documented defining property. Fail loudly if our local scorer
    # disagrees with the externally documented zero-C4/zero-C8 claims.
    for length in (4, 8):
        component = component_map.get(length)
        if component is None:
            raise RuntimeError(f"scorer omitted C{length}")
        if component["status"] != STATUS_EXACT:
            raise RuntimeError(
                f"C{length} is not exact: {component['status']}"
            )
        if int(component["observed"]) != 0:
            raise RuntimeError(
                f"transcribed Markström graph unexpectedly has "
                f"C{length}={component['observed']}"
            )

    payload = {
        **graph.record(),
        "source": {
            "name": "Markstroem Graph",
            "construction": "SageMath graphs.MarkstroemGraph() topology",
            "sage_source_path": (
                "src/sage/graphs/generators/smallgraphs.py:"
                "MarkstroemGraph"
            ),
            "external_known_properties": {
                "order": 24,
                "edge_count": 36,
                "regular_degree": 3,
                "planar": True,
                "C4": 0,
                "C8": 0,
                "contains_C16": True,
            },
        },
        "score": {
            "total": total,
            "weighted": weighted,
            "fully_exact": total is not None,
            "components": {
                str(component["length"]): {
                    "observed": component["observed"],
                    "status": component["status"],
                    "nodes": component["nodes"],
                    "elapsed_ns": component["elapsed_ns"],
                }
                for component in components
            },
            "wall_elapsed_seconds": elapsed,
            "witness_cap": args.witness_cap,
            "node_budget": args.node_budget,
        },
        "cross_checks": {
            "edge_count": len(graph.edges),
            "degree_sequence": list(degrees),
            "exact_c4_formula": c4_formula,
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    table = Table(title="Markström n=24 exact HEG score")
    table.add_column("length", justify="right")
    table.add_column("count", justify="right")
    table.add_column("status")
    table.add_column("nodes", justify="right")

    for component in components:
        table.add_row(
            str(component["length"]),
            str(component["observed"]),
            str(component["status"]),
            str(component["nodes"]),
        )

    console.print(table)
    console.print(
        f"order={graph.order} edges={len(graph.edges)} cubic=yes "
        f"hash={graph.graph_hash[:8]}"
    )
    console.print(
        f"TOTAL={total if total is not None else 'inexact'} "
        f"weighted={weighted if weighted is not None else 'inexact'} "
        f"elapsed={elapsed:.3f}s"
    )
    console.print(f"saved: {args.output}")

    if total is not None:
        console.print()
        console.print("[bold]Polish command:[/bold]")
        console.print(
            "uv run python scripts/heg_fixed_order_coupled_switch_v2.py "
            f"--start-graph {args.output} "
            "--beam-width 32 --main-lanes 16 --structural-lanes 8 "
            "--escape-lanes 8 --escape-height 1 "
            "--structural-pool 128 --escape-structural-pool 48 "
            "--structural-workers 4 --max-structural-cycle-length 8 "
            "--max-depth 1000 --step-seconds 120 --total-seconds 900 "
            "--workers 16 --score-batch-size 64 --inflight-per-worker 4 "
            "--families "
            "two_switch,endpoint_relocate,vertex_rewire2,coupled_switch2 "
            "--guided-limit-per-parent 512 "
            "--coupled-scan-per-parent 8192 "
            "--hot-vertices-per-parent 6 "
            "--node-budget 10000000 --witness-cap 1000000 "
            "--save-best markstroem_polish_best_n24.json "
            "--save-lineage markstroem_polish_lineage_n24.json"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
