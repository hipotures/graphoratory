#!/usr/bin/env python3
"""
HEG order sweep: bootstrap n=23..30, then run independent fixed-order searches.

Purpose
-------
Search several nearby graph orders instead of overfitting mutation development
to the current n=26 laboratory instance.

Default experiment:

    orders:             23..30
    whole wall budget:  3600 s
    bootstrap budget:    600 s maximum
    fixed-order budget: remaining time split equally among unfinished orders

The bootstrap is used ONLY to obtain one coherent starting graph for every
requested order. Each order is then searched independently by the current
fixed-order engine.

The driver deliberately runs orders sequentially. A fixed-order child already
uses many score processes, so running multiple orders concurrently would mostly
oversubscribe the machine and make results harder to compare.

Expected files
--------------
This script expects the existing experiment scripts:

    scripts/heg_growth_rewire2_probe.py
    scripts/heg_fixed_order_coupled_switch_v2.py

Run from the repository root:

    uv run python scripts/heg_order_sweep.py

Output:

    order_sweep_23_30/
        bootstrap/
            rewire2_lineage.json
            rewire2_final.json
            order_23.json
            ...
            order_30.json
        order_23/
            best.json
            lineage.json
            run.log
            result.json
        ...
        summary.json
        summary.csv

Seed discovery
--------------
Before bootstrapping, the driver shallow-scans --seed-dir (default ".") for JSON
graphs with an exact reported score. If an existing graph for an order is better
than the bootstrap graph, it is used as that order's fixed-order root.

This is particularly useful for n=26, where an already-polished TOTAL=7 graph
may exist locally.

Scientific semantics
--------------------
The sweep never compares a heuristic score to declare mathematical success.
A counterexample is only reported when the authoritative fixed-order child saves
an exact TOTAL=0 graph.

For orders 23..30 the active forbidden lengths are the same: 4, 8, and 16.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table

from graphoratory.config import (
    DegreeRangeConfig,
    ErdosRenyiConfig,
    GraphConfig,
    MixedGeneratorConfig,
)
from graphoratory.graphs import Graph, generate_graphs


console = Console()

MAX_ORDER = 128
DEFAULT_SEED = 4001
DEFAULT_NODE_BUDGET = 10_000_000
DEFAULT_WITNESS_CAP = 1_000_000


@dataclass(frozen=True, slots=True)
class SeedGraph:
    order: int
    path: Path
    total: int | None
    weighted: int | None
    graph_hash: str | None
    source: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    order: int
    status: str
    start_path: str | None
    start_total: int | None
    best_path: str | None
    best_total: int | None
    weighted: int | None
    c4: int | None
    c8: int | None
    c16: int | None
    tau_edge: int | None
    tau_vertex: int | None
    edge_union: int | None
    vertex_union: int | None
    max_edge_load: int | None
    max_vertex_load: int | None
    best_hash: str | None
    elapsed_seconds: float
    budget_seconds: float
    pass_index: int = 1


def norm_edge(u: int, v: int) -> tuple[int, int]:
    if u == v:
        raise ValueError("self-loop")
    return (u, v) if u < v else (v, u)


def parse_orders(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start = int(left)
            end = int(right)
            if end < start:
                raise argparse.ArgumentTypeError(
                    f"invalid descending order range: {part}"
                )
            values.extend(range(start, end + 1))
        else:
            values.append(int(part))

    values = sorted(set(values))
    if not values:
        raise argparse.ArgumentTypeError("at least one order is required")
    if values[0] < 4 or values[-1] > MAX_ORDER:
        raise argparse.ArgumentTypeError(
            f"orders must be in [4, {MAX_ORDER}]"
        )
    return tuple(values)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap nearby HEG orders and run independent fixed-order "
            "mutation searches under one wall-clock budget."
        )
    )
    parser.add_argument(
        "--orders",
        type=parse_orders,
        default=parse_orders("23-30"),
        help='Orders/ranges, e.g. "23-30" or "23,24,26-30".',
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=3600.0,
        help="Whole sweep wall-clock budget, including bootstrap.",
    )
    parser.add_argument(
        "--bootstrap-seconds",
        type=float,
        default=600.0,
        help=(
            "Maximum bootstrap wall-clock budget. Set 0 to require existing "
            "seed graphs for all requested orders."
        ),
    )
    parser.add_argument(
        "--bootstrap-step-seconds",
        type=float,
        default=40.0,
        help="Per-order-increment budget for the growth bootstrap.",
    )
    parser.add_argument(
        "--bootstrap-start-order",
        type=int,
        default=16,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("order_sweep_23_30"),
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=Path("."),
        help=(
            "Shallow directory scanned for already-known exact graph JSONs. "
            "No recursive scan is performed."
        ),
    )
    parser.add_argument(
        "--no-discover-seeds",
        action="store_true",
        help="Do not inspect --seed-dir for existing better starting graphs.",
    )

    parser.add_argument(
        "--growth-script",
        type=Path,
        default=Path("scripts/heg_growth_rewire2_probe.py"),
    )
    parser.add_argument(
        "--polish-script",
        type=Path,
        default=Path("scripts/heg_fixed_order_coupled_switch_v2.py"),
    )

    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--structural-workers", type=int, default=4)
    parser.add_argument("--beam-width", type=int, default=32)
    parser.add_argument("--main-lanes", type=int, default=16)
    parser.add_argument("--structural-lanes", type=int, default=8)
    parser.add_argument("--escape-lanes", type=int, default=8)
    parser.add_argument("--escape-height", type=int, default=1)
    parser.add_argument("--structural-pool", type=int, default=128)
    parser.add_argument("--escape-structural-pool", type=int, default=48)
    parser.add_argument("--max-structural-cycle-length", type=int, default=8)
    parser.add_argument("--max-depth", type=int, default=1000)
    parser.add_argument("--step-seconds", type=float, default=120.0)
    parser.add_argument("--score-batch-size", type=int, default=64)
    parser.add_argument("--inflight-per-worker", type=int, default=4)
    parser.add_argument(
        "--families",
        default=(
            "two_switch,endpoint_relocate,"
            "vertex_rewire2,coupled_switch2"
        ),
    )
    parser.add_argument("--guided-limit-per-parent", type=int, default=512)
    parser.add_argument("--coupled-scan-per-parent", type=int, default=8192)
    parser.add_argument("--hot-vertices-per-parent", type=int, default=6)

    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--node-budget",
        type=int,
        default=DEFAULT_NODE_BUDGET,
    )
    parser.add_argument(
        "--witness-cap",
        type=int,
        default=DEFAULT_WITNESS_CAP,
    )

    parser.add_argument(
        "--min-order-seconds",
        type=float,
        default=60.0,
        help=(
            "If less than this much per remaining order is available, stop "
            "instead of launching scientifically tiny runs."
        ),
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Reuse completed results and use a partial order's best.json as "
            "its next start graph."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned child commands without running them.",
    )

    args = parser.parse_args()

    if args.total_seconds <= 0:
        parser.error("--total-seconds must be > 0")
    if args.bootstrap_seconds < 0:
        parser.error("--bootstrap-seconds must be >= 0")
    if args.bootstrap_step_seconds <= 0:
        parser.error("--bootstrap-step-seconds must be > 0")
    if args.bootstrap_start_order < 4:
        parser.error("--bootstrap-start-order must be >= 4")
    if args.bootstrap_start_order > min(args.orders):
        parser.error(
            "--bootstrap-start-order must not exceed the smallest requested order"
        )
    if args.workers < 1 or args.structural_workers < 1:
        parser.error("worker counts must be >= 1")
    if args.beam_width < 1:
        parser.error("--beam-width must be >= 1")
    if (
        args.main_lanes
        + args.structural_lanes
        + args.escape_lanes
        != args.beam_width
    ):
        parser.error(
            "--main-lanes + --structural-lanes + --escape-lanes "
            "must equal --beam-width"
        )
    if args.escape_height < 0:
        parser.error("--escape-height must be >= 0")
    if args.escape_lanes == 0 and args.escape_structural_pool != 0:
        parser.error(
            "--escape-structural-pool must be 0 when --escape-lanes is 0"
        )
    if args.escape_structural_pool >= args.structural_pool:
        parser.error(
            "--escape-structural-pool must be < --structural-pool"
        )
    if args.min_order_seconds < 1:
        parser.error("--min-order-seconds must be >= 1")

    return args


def generate_bootstrap_root(order: int, seed: int) -> Graph:
    """
    Reproduce the root generation used by heg_growth_rewire2_probe.py.
    """
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
        degree_sequence_rejection=DegreeRangeConfig(
            degree_min=3,
            degree_max=4,
        ),
        mixed=MixedGeneratorConfig(
            generators=("cycle_matching_stub_pairing",),
            weights=(1.0,),
        ),
    )
    graph = generate_graphs(config).graphs[0]
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    return graph


def replay_growth_mutation(
    graph: Graph,
    mutation_payload: dict[str, Any],
) -> Graph:
    edge_set = set(graph.edges)

    removed_edges = tuple(
        norm_edge(int(edge[0]), int(edge[1]))
        for edge in mutation_payload.get("removed_edges", [])
    )
    added_old_edges = tuple(
        norm_edge(int(edge[0]), int(edge[1]))
        for edge in mutation_payload.get("added_old_edges", [])
    )
    new_neighbors = tuple(
        int(vertex)
        for vertex in mutation_payload.get("new_neighbors", [])
    )

    for edge in removed_edges:
        if edge not in edge_set:
            raise RuntimeError(
                f"growth replay: removed edge {edge} does not exist"
            )
        edge_set.remove(edge)

    for edge in added_old_edges:
        if edge in edge_set:
            raise RuntimeError(
                f"growth replay: added old edge {edge} already exists"
            )
        edge_set.add(edge)

    x = graph.order
    if len(set(new_neighbors)) != len(new_neighbors):
        raise RuntimeError("growth replay: duplicate new-vertex neighbor")
    if len(new_neighbors) < 3:
        raise RuntimeError("growth replay: new vertex would have degree < 3")

    for vertex in new_neighbors:
        if not (0 <= vertex < graph.order):
            raise RuntimeError(
                f"growth replay: invalid new neighbor {vertex}"
            )
        edge_set.add(norm_edge(vertex, x))

    candidate = Graph.from_edges(graph.order + 1, edge_set)
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)

    if len(candidate.edges) != len(graph.edges) + 2:
        raise RuntimeError(
            "growth replay: expected every lineage step to have Δedges=+2"
        )
    return candidate


def state_total(state: dict[str, Any]) -> int | None:
    total = state.get("total")
    if not isinstance(total, dict):
        return None
    upper = total.get("upper")
    lower = total.get("lower")
    if upper is None or lower is None or upper != lower:
        return None
    return int(upper)


def state_weighted_from_components(
    state: dict[str, Any],
) -> int | None:
    components = state.get("components")
    if not isinstance(components, dict):
        return None

    value = 0
    for length_text, component in components.items():
        if not isinstance(component, dict):
            return None
        if component.get("status") != "EXACT":
            return None
        observed = component.get("observed")
        if observed is None:
            return None
        length = int(length_text)
        value += max(1, 64 // length) * int(observed)
    return value


def save_bootstrap_graph(
    path: Path,
    graph: Graph,
    state: dict[str, Any],
) -> None:
    payload = {
        **graph.record(),
        "bootstrap_source": "heg_growth_rewire2_probe.py",
        "bootstrap_score": {
            "total": state_total(state),
            "weighted": state_weighted_from_components(state),
            "components": {
                str(length): {
                    "observed": int(component["observed"]),
                    "status": component["status"],
                }
                for length, component in state.get("components", {}).items()
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def reconstruct_bootstrap_orders(
    *,
    lineage_path: Path,
    output_dir: Path,
    requested_orders: Iterable[int],
    start_order: int,
    seed: int,
) -> dict[int, SeedGraph]:
    payload = json.loads(lineage_path.read_text(encoding="utf-8"))
    states = payload.get("states")
    if not isinstance(states, list) or not states:
        raise RuntimeError("bootstrap lineage has no states")

    root_state = states[0]
    if int(root_state["order"]) != start_order:
        raise RuntimeError(
            "bootstrap lineage root order mismatch: "
            f"{root_state['order']} != {start_order}"
        )

    graph = generate_bootstrap_root(start_order, seed)
    expected_root_hash = str(root_state["graph_hash"])
    if graph.graph_hash != expected_root_hash:
        raise RuntimeError(
            "bootstrap root hash mismatch. "
            f"generated={graph.graph_hash}, lineage={expected_root_hash}. "
            "The graph generator/config no longer reproduces the lineage."
        )

    requested = set(int(order) for order in requested_orders)
    found: dict[int, SeedGraph] = {}

    def maybe_save(
        current_graph: Graph,
        current_state: dict[str, Any],
    ) -> None:
        if current_graph.order not in requested:
            return
        path = output_dir / f"order_{current_graph.order:02d}.json"
        save_bootstrap_graph(path, current_graph, current_state)
        found[current_graph.order] = SeedGraph(
            order=current_graph.order,
            path=path,
            total=state_total(current_state),
            weighted=state_weighted_from_components(current_state),
            graph_hash=current_graph.graph_hash,
            source="bootstrap",
        )

    maybe_save(graph, root_state)

    for state in states[1:]:
        mutation = state.get("mutation")
        if not isinstance(mutation, dict):
            raise RuntimeError(
                f"bootstrap lineage order={state.get('order')} has no mutation"
            )
        graph = replay_growth_mutation(graph, mutation)

        expected_order = int(state["order"])
        expected_hash = str(state["graph_hash"])
        if graph.order != expected_order:
            raise RuntimeError(
                f"growth replay order mismatch: {graph.order} != {expected_order}"
            )
        if graph.graph_hash != expected_hash:
            raise RuntimeError(
                "growth replay hash mismatch at "
                f"order={expected_order}: {graph.graph_hash} != {expected_hash}"
            )

        maybe_save(graph, state)

    return found


def exact_total_from_payload(payload: dict[str, Any]) -> int | None:
    score = payload.get("score")
    if isinstance(score, dict):
        total = score.get("total")
        if isinstance(total, int):
            components = score.get("components")
            if isinstance(components, dict):
                statuses = [
                    component.get("status")
                    for component in components.values()
                    if isinstance(component, dict)
                ]
                if statuses and all(status == "EXACT" for status in statuses):
                    return int(total)
            # Fixed-order saved best is only written from a fully exact state.
            if "polish_depth" in payload:
                return int(total)

    bootstrap = payload.get("bootstrap_score")
    if isinstance(bootstrap, dict):
        total = bootstrap.get("total")
        if isinstance(total, int):
            return int(total)

    growth_score = payload.get("forbidden_cycle_score")
    if isinstance(growth_score, dict) and growth_score:
        total = 0
        for component in growth_score.values():
            if not isinstance(component, dict):
                return None
            if component.get("status") != "EXACT":
                return None
            observed = component.get("observed")
            if not isinstance(observed, int):
                return None
            total += observed
        return total

    return None


def weighted_from_payload(payload: dict[str, Any]) -> int | None:
    score = payload.get("score")
    if isinstance(score, dict) and isinstance(score.get("weighted"), int):
        return int(score["weighted"])

    bootstrap = payload.get("bootstrap_score")
    if isinstance(bootstrap, dict) and isinstance(
        bootstrap.get("weighted"), int
    ):
        return int(bootstrap["weighted"])

    return None


def graph_hash_from_payload(payload: dict[str, Any]) -> str | None:
    value = payload.get("graph_hash")
    if isinstance(value, str):
        return value
    return None


def inspect_seed_json(path: Path) -> SeedGraph | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    order = payload.get("order")
    edges = payload.get("edges")
    if not isinstance(order, int) or not isinstance(edges, list):
        return None

    total = exact_total_from_payload(payload)
    if total is None:
        return None

    return SeedGraph(
        order=int(order),
        path=path,
        total=total,
        weighted=weighted_from_payload(payload),
        graph_hash=graph_hash_from_payload(payload),
        source="discovered",
    )


def discover_seed_graphs(
    directory: Path,
    orders: Iterable[int],
) -> dict[int, SeedGraph]:
    wanted = set(int(order) for order in orders)
    result: dict[int, SeedGraph] = {}

    if not directory.exists():
        return result

    for path in sorted(directory.glob("*.json")):
        seed = inspect_seed_json(path)
        if seed is None or seed.order not in wanted:
            continue

        incumbent = result.get(seed.order)
        if incumbent is None:
            result[seed.order] = seed
            continue

        incumbent_key = (
            math.inf if incumbent.total is None else incumbent.total,
            math.inf if incumbent.weighted is None else incumbent.weighted,
            str(incumbent.path),
        )
        seed_key = (
            math.inf if seed.total is None else seed.total,
            math.inf if seed.weighted is None else seed.weighted,
            str(seed.path),
        )
        if seed_key < incumbent_key:
            result[seed.order] = seed

    return result


def choose_better_seed(
    first: SeedGraph | None,
    second: SeedGraph | None,
) -> SeedGraph | None:
    candidates = [seed for seed in (first, second) if seed is not None]
    if not candidates:
        return None

    return min(
        candidates,
        key=lambda seed: (
            math.inf if seed.total is None else seed.total,
            math.inf if seed.weighted is None else seed.weighted,
            0 if seed.source == "discovered" else 1,
            str(seed.path),
        ),
    )


def stream_command(
    command: list[str],
    *,
    log_path: Path,
    cwd: Path,
    dry_run: bool,
) -> int:
    console.print("[dim]$ " + " ".join(command) + "[/dim]")

    if dry_run:
        return 0

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return process.wait()


def bootstrap_command(
    args: argparse.Namespace,
    *,
    bootstrap_dir: Path,
    budget: float,
) -> list[str]:
    return [
        sys.executable,
        str(args.growth_script),
        "--start-order",
        str(args.bootstrap_start_order),
        "--target-order",
        str(max(args.orders)),
        "--beam-width",
        "16",
        "--step-seconds",
        f"{args.bootstrap_step_seconds:.6f}",
        "--total-seconds",
        f"{budget:.6f}",
        "--workers",
        str(args.workers),
        "--families",
        "split_spoke,double_hub,switch_spoke",
        "--objective",
        "total",
        "--node-budget",
        str(args.node_budget),
        "--witness-cap",
        str(args.witness_cap),
        "--seed",
        str(args.seed),
        "--save-final",
        str(bootstrap_dir / "rewire2_final.json"),
        "--save-lineage",
        str(bootstrap_dir / "rewire2_lineage.json"),
        "--hit-dir",
        str(bootstrap_dir / "hits"),
    ]


def polish_command(
    args: argparse.Namespace,
    *,
    order: int,
    start_graph: Path,
    order_dir: Path,
    budget: float,
    pass_index: int,
) -> list[str]:
    # Change the random stream between resumed/top-up runs, while keeping every
    # individual run exactly reproducible.
    run_seed = args.seed + 1_000_003 * (pass_index - 1) + order

    return [
        sys.executable,
        str(args.polish_script),
        "--start-graph",
        str(start_graph),
        "--beam-width",
        str(args.beam_width),
        "--main-lanes",
        str(args.main_lanes),
        "--structural-lanes",
        str(args.structural_lanes),
        "--escape-lanes",
        str(args.escape_lanes),
        "--escape-height",
        str(args.escape_height),
        "--structural-pool",
        str(args.structural_pool),
        "--escape-structural-pool",
        str(args.escape_structural_pool),
        "--structural-workers",
        str(args.structural_workers),
        "--max-structural-cycle-length",
        str(args.max_structural_cycle_length),
        "--max-depth",
        str(args.max_depth),
        "--step-seconds",
        f"{args.step_seconds:.6f}",
        "--total-seconds",
        f"{budget:.6f}",
        "--workers",
        str(args.workers),
        "--score-batch-size",
        str(args.score_batch_size),
        "--inflight-per-worker",
        str(args.inflight_per_worker),
        "--families",
        args.families,
        "--guided-limit-per-parent",
        str(args.guided_limit_per_parent),
        "--coupled-scan-per-parent",
        str(args.coupled_scan_per_parent),
        "--hot-vertices-per-parent",
        str(args.hot_vertices_per_parent),
        "--seed",
        str(run_seed),
        "--node-budget",
        str(args.node_budget),
        "--witness-cap",
        str(args.witness_cap),
        "--save-best",
        str(order_dir / "best.json"),
        "--save-lineage",
        str(order_dir / "lineage.json"),
        "--hit-dir",
        str(order_dir / "hits"),
    ]


def payload_component(
    payload: dict[str, Any],
    length: int,
) -> int | None:
    score = payload.get("score")
    if not isinstance(score, dict):
        return None
    components = score.get("components")
    if not isinstance(components, dict):
        return None
    component = components.get(str(length))
    if not isinstance(component, dict):
        return None
    observed = component.get("observed")
    return int(observed) if isinstance(observed, int) else None


def load_order_result(
    *,
    order: int,
    start_seed: SeedGraph,
    best_path: Path,
    elapsed: float,
    budget: float,
    status: str,
    pass_index: int,
) -> OrderResult:
    if not best_path.exists():
        return OrderResult(
            order=order,
            status=status,
            start_path=str(start_seed.path),
            start_total=start_seed.total,
            best_path=None,
            best_total=None,
            weighted=None,
            c4=None,
            c8=None,
            c16=None,
            tau_edge=None,
            tau_vertex=None,
            edge_union=None,
            vertex_union=None,
            max_edge_load=None,
            max_vertex_load=None,
            best_hash=None,
            elapsed_seconds=elapsed,
            budget_seconds=budget,
            pass_index=pass_index,
        )

    payload = json.loads(best_path.read_text(encoding="utf-8"))
    structural = payload.get("structural")
    if not isinstance(structural, dict):
        structural = {}

    return OrderResult(
        order=order,
        status=status,
        start_path=str(start_seed.path),
        start_total=start_seed.total,
        best_path=str(best_path),
        best_total=exact_total_from_payload(payload),
        weighted=weighted_from_payload(payload),
        c4=payload_component(payload, 4),
        c8=payload_component(payload, 8),
        c16=payload_component(payload, 16),
        tau_edge=(
            int(structural["tau_edge"])
            if isinstance(structural.get("tau_edge"), int)
            else None
        ),
        tau_vertex=(
            int(structural["tau_vertex"])
            if isinstance(structural.get("tau_vertex"), int)
            else None
        ),
        edge_union=(
            int(structural["forbidden_edge_union"])
            if isinstance(structural.get("forbidden_edge_union"), int)
            else None
        ),
        vertex_union=(
            int(structural["forbidden_vertex_union"])
            if isinstance(structural.get("forbidden_vertex_union"), int)
            else None
        ),
        max_edge_load=(
            int(structural["max_cycles_per_edge"])
            if isinstance(structural.get("max_cycles_per_edge"), int)
            else None
        ),
        max_vertex_load=(
            int(structural["max_cycles_per_vertex"])
            if isinstance(structural.get("max_cycles_per_vertex"), int)
            else None
        ),
        best_hash=graph_hash_from_payload(payload),
        elapsed_seconds=elapsed,
        budget_seconds=budget,
        pass_index=pass_index,
    )


def result_to_dict(result: OrderResult) -> dict[str, Any]:
    return {
        field: getattr(result, field)
        for field in result.__dataclass_fields__
    }


def save_result(path: Path, result: OrderResult) -> None:
    path.write_text(
        json.dumps(result_to_dict(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_saved_result(path: Path) -> OrderResult | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return OrderResult(**payload)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def print_summary(results: list[OrderResult]) -> None:
    table = Table(title="HEG order sweep")
    table.add_column("n", justify="right")
    table.add_column("status")
    table.add_column("start", justify="right")
    table.add_column("best", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("C4", justify="right")
    table.add_column("C8", justify="right")
    table.add_column("C16", justify="right")
    table.add_column("τE", justify="right")
    table.add_column("τV", justify="right")
    table.add_column("maxE", justify="right")
    table.add_column("maxV", justify="right")
    table.add_column("sec", justify="right")
    table.add_column("hash")

    for result in sorted(results, key=lambda item: item.order):
        delta = (
            None
            if result.start_total is None or result.best_total is None
            else result.best_total - result.start_total
        )
        table.add_row(
            str(result.order),
            result.status,
            "-" if result.start_total is None else str(result.start_total),
            "-" if result.best_total is None else str(result.best_total),
            "-" if delta is None else f"{delta:+d}",
            "-" if result.c4 is None else str(result.c4),
            "-" if result.c8 is None else str(result.c8),
            "-" if result.c16 is None else str(result.c16),
            "-" if result.tau_edge is None else str(result.tau_edge),
            "-" if result.tau_vertex is None else str(result.tau_vertex),
            "-" if result.max_edge_load is None else str(result.max_edge_load),
            "-" if result.max_vertex_load is None else str(result.max_vertex_load),
            f"{result.elapsed_seconds:.1f}",
            "-" if result.best_hash is None else result.best_hash[:8],
        )

    console.print()
    console.print(table)


def write_summary_files(
    output_dir: Path,
    results: list[OrderResult],
    *,
    started_at_unix: float,
    wall_elapsed: float,
    args: argparse.Namespace,
) -> None:
    ordered = sorted(results, key=lambda item: item.order)
    payload = {
        "schema_version": "graphoratory.heg_order_sweep.v1",
        "orders": list(args.orders),
        "requested_total_seconds": args.total_seconds,
        "wall_elapsed_seconds": wall_elapsed,
        "started_at_unix": started_at_unix,
        "results": [result_to_dict(result) for result in ordered],
    }

    (output_dir / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    fieldnames = list(OrderResult.__dataclass_fields__)
    with (output_dir / "summary.csv").open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in ordered:
            writer.writerow(result_to_dict(result))


def hit_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def main() -> int:
    args = parse_args()

    repo_root = Path.cwd()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_dir = args.output_dir / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)

    if not args.growth_script.exists():
        raise FileNotFoundError(args.growth_script)
    if not args.polish_script.exists():
        raise FileNotFoundError(args.polish_script)

    started_perf = time.perf_counter()
    started_unix = time.time()
    deadline = started_perf + args.total_seconds

    console.print(
        f"[bold]HEG order sweep[/bold] "
        f"orders={min(args.orders)}..{max(args.orders)} "
        f"count={len(args.orders)} total_budget={args.total_seconds:.0f}s"
    )
    console.print(
        f"workers={args.workers} structural_workers={args.structural_workers} "
        f"beam={args.beam_width} escape_height={args.escape_height}"
    )
    console.print(
        f"families={args.families} "
        f"coupled_scan={args.coupled_scan_per_parent}"
    )

    discovered: dict[int, SeedGraph] = {}
    if not args.no_discover_seeds:
        discovered = discover_seed_graphs(args.seed_dir, args.orders)
        if discovered:
            console.print("[bold]Existing exact seeds discovered:[/bold]")
            for order in sorted(discovered):
                seed = discovered[order]
                console.print(
                    f"  n={order} total={seed.total} "
                    f"path={seed.path}"
                )

    bootstrap_seeds: dict[int, SeedGraph] = {}
    lineage_path = bootstrap_dir / "rewire2_lineage.json"

    if args.resume and lineage_path.exists():
        console.print(
            f"[green]Reusing bootstrap lineage:[/green] {lineage_path}"
        )
        bootstrap_seeds = reconstruct_bootstrap_orders(
            lineage_path=lineage_path,
            output_dir=bootstrap_dir,
            requested_orders=args.orders,
            start_order=args.bootstrap_start_order,
            seed=args.seed,
        )

    missing_after_discovery = [
        order
        for order in args.orders
        if order not in discovered and order not in bootstrap_seeds
    ]

    if missing_after_discovery and not bootstrap_seeds:
        if args.bootstrap_seconds <= 0:
            raise RuntimeError(
                "Missing seed graphs for orders "
                + ",".join(map(str, missing_after_discovery))
                + " and --bootstrap-seconds=0."
            )

        remaining = max(0.0, deadline - time.perf_counter())
        # Do not let bootstrap consume the entire sweep. Preserve at least one
        # minimum fixed-order run per requested order.
        fixed_reserve = args.min_order_seconds * len(args.orders)
        bootstrap_budget = min(
            args.bootstrap_seconds,
            max(1.0, remaining - fixed_reserve),
        )

        console.print(
            f"[bold]Bootstrap growth[/bold] "
            f"budget={bootstrap_budget:.1f}s target={max(args.orders)}"
        )

        command = bootstrap_command(
            args,
            bootstrap_dir=bootstrap_dir,
            budget=bootstrap_budget,
        )
        rc = stream_command(
            command,
            log_path=bootstrap_dir / "run.log",
            cwd=repo_root,
            dry_run=args.dry_run,
        )
        if rc != 0:
            raise RuntimeError(f"bootstrap child exited with code {rc}")

        bootstrap_hits = hit_files(bootstrap_dir / "hits")
        if bootstrap_hits:
            console.print(
                "[bold red]BOOTSTRAP FOUND TOTAL=0[/bold red] "
                f"{bootstrap_hits[0]}"
            )
            return 0

        if args.dry_run:
            console.print(
                "[yellow]Dry-run stops before lineage reconstruction.[/yellow]"
            )
            return 0

        if not lineage_path.exists():
            raise RuntimeError(
                "bootstrap completed without writing rewire2_lineage.json"
            )

        bootstrap_seeds = reconstruct_bootstrap_orders(
            lineage_path=lineage_path,
            output_dir=bootstrap_dir,
            requested_orders=args.orders,
            start_order=args.bootstrap_start_order,
            seed=args.seed,
        )

    seeds: dict[int, SeedGraph] = {}
    for order in args.orders:
        seed = choose_better_seed(
            discovered.get(order),
            bootstrap_seeds.get(order),
        )
        if seed is None:
            raise RuntimeError(
                f"No starting graph available for requested order {order}. "
                "Increase --bootstrap-seconds or provide an exact seed JSON."
            )
        seeds[order] = seed

    console.print()
    console.print("[bold]Selected fixed-order roots:[/bold]")
    for order in args.orders:
        seed = seeds[order]
        console.print(
            f"  n={order} total={seed.total} source={seed.source} "
            f"path={seed.path}"
        )

    results_by_order: dict[int, OrderResult] = {}

    # Resume completed order results first.
    if args.resume:
        for order in args.orders:
            result_path = args.output_dir / f"order_{order:02d}" / "result.json"
            saved = load_saved_result(result_path)
            if saved is not None and saved.status in {"complete", "zero"}:
                results_by_order[order] = saved

    pending_orders = [
        order for order in args.orders if order not in results_by_order
    ]

    for position, order in enumerate(pending_orders):
        remaining_orders = len(pending_orders) - position
        remaining_time = max(0.0, deadline - time.perf_counter())
        fair_budget = remaining_time / remaining_orders

        if fair_budget < args.min_order_seconds:
            console.print(
                f"[yellow]Stopping before n={order}: only "
                f"{fair_budget:.1f}s/order remains.[/yellow]"
            )
            break

        # Small guard for process startup / result serialization.
        order_budget = max(
            args.min_order_seconds,
            fair_budget - 2.0,
        )

        order_dir = args.output_dir / f"order_{order:02d}"
        order_dir.mkdir(parents=True, exist_ok=True)
        best_path = order_dir / "best.json"
        result_path = order_dir / "result.json"

        start_seed = seeds[order]
        pass_index = 1

        # If a previous interrupted attempt produced a best graph, continue
        # from it rather than throwing away scientific progress.
        if args.resume and best_path.exists():
            partial = inspect_seed_json(best_path)
            if partial is not None:
                start_seed = choose_better_seed(start_seed, partial) or start_seed
                pass_index = 2

        console.rule(
            f"n={order}  start_TOTAL={start_seed.total}  "
            f"budget={order_budget:.1f}s"
        )

        command = polish_command(
            args,
            order=order,
            start_graph=start_seed.path,
            order_dir=order_dir,
            budget=order_budget,
            pass_index=pass_index,
        )

        run_started = time.perf_counter()
        rc = stream_command(
            command,
            log_path=order_dir / "run.log",
            cwd=repo_root,
            dry_run=args.dry_run,
        )
        elapsed = time.perf_counter() - run_started

        if args.dry_run:
            continue

        status = "complete" if rc == 0 else f"exit_{rc}"
        order_hits = hit_files(order_dir / "hits")
        if order_hits:
            status = "zero"

        result = load_order_result(
            order=order,
            start_seed=start_seed,
            best_path=best_path,
            elapsed=elapsed,
            budget=order_budget,
            status=status,
            pass_index=pass_index,
        )
        results_by_order[order] = result
        save_result(result_path, result)

        print_summary(list(results_by_order.values()))
        write_summary_files(
            args.output_dir,
            list(results_by_order.values()),
            started_at_unix=started_unix,
            wall_elapsed=time.perf_counter() - started_perf,
            args=args,
        )

        if result.best_total == 0 or order_hits:
            console.print(
                f"[bold red]COUNTEREXAMPLE HIT AT ORDER {order}[/bold red]"
            )
            if order_hits:
                console.print(str(order_hits[0]))
            break

        if rc != 0:
            console.print(
                f"[yellow]n={order} child exited {rc}; continuing sweep.[/yellow]"
            )

    results = list(results_by_order.values())
    print_summary(results)

    wall_elapsed = time.perf_counter() - started_perf
    write_summary_files(
        args.output_dir,
        results,
        started_at_unix=started_unix,
        wall_elapsed=wall_elapsed,
        args=args,
    )

    console.print(
        f"[bold]Sweep done[/bold] elapsed={wall_elapsed:.1f}s "
        f"results={len(results)}/{len(args.orders)}"
    )
    console.print(f"summary: {args.output_dir / 'summary.json'}")
    console.print(f"csv:     {args.output_dir / 'summary.csv'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
