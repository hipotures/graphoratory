#!/usr/bin/env python3
"""
Blind HEG random-walk sweep for several graph orders.

Runs the existing cycle-blind worker-local mutator independently for each order.
Default target set is exactly the requested laboratory sweep:

    n = 23, 25, 26, 27, 28, 29, 30, 31
    300 seconds per order

The child mutator remains blind to C4/C8/C16 when constructing mutations.
Every child worker mutates and evaluates locally. This driver only schedules
orders, chooses a start graph, captures full child output to logs, and emits a
minimal stdout stream containing only improvements of

    T(n) = min exact (C4 + C8 + C16).

Example stdout:

    T(23)=12
    T(23)=8
    T(23)=5
    T(25)=17
    T(25)=9
    ...

Everything else is written under --output-dir.

Start graph policy
------------------
To maximize the chance of finding something new, the default is a warm start:
for each order, the driver looks for the best already-known exact graph in a
small set of likely project locations and any --seed-dir supplied by the user.
The mutation/search itself is still completely cycle-blind and parent selection
remains score-blind by default.

If no prior exact graph is found, the driver creates a deterministic legal seed
with |V|=n, simple/connected, and delta>=3. For even n this seed is cubic; for
odd n it has the minimum possible number ceil(3n/2) of edges.

Use --fresh-seeds to disable warm starts entirely and use only deterministic
legal seeds. Explicit --start ORDER=PATH overrides both policies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_ORDERS = (23, 25, 26, 27, 28, 29, 30, 31)
MAX_ORDER = 128

START_RE = re.compile(r"\bSTART total=(\d+)\b")
NEW_BEST_RE = re.compile(r"\bNEW BEST total=(\d+)\b")
DONE_RE = re.compile(r"\bDONE best_total=(\d+)\b")


@dataclass(frozen=True, slots=True)
class SeedChoice:
    order: int
    path: str
    source: str
    reported_total: int | None
    reported_weighted: int | None
    edge_count: int


@dataclass(frozen=True, slots=True)
class OrderSummary:
    order: int
    status: str
    seed: SeedChoice
    initial_total: int | None
    best_total: int | None
    best_weighted: int | None
    best_components: dict[str, int] | None
    best_edge_count: int | None
    best_hash: str | None
    evaluated: int | None
    exact: int | None
    elapsed_seconds: float
    child_exit_code: int
    run_log: str
    best_graph: str
    hits_log: str
    pool_file: str


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
                raise argparse.ArgumentTypeError(f"descending range: {part}")
            values.extend(range(start, end + 1))
        else:
            values.append(int(part))
    values = sorted(set(values))
    if not values:
        raise argparse.ArgumentTypeError("at least one order is required")
    if values[0] < 4 or values[-1] > MAX_ORDER:
        raise argparse.ArgumentTypeError(f"orders must be in [4,{MAX_ORDER}]")
    return tuple(values)


def parse_explicit_start(raw: str) -> tuple[int, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected ORDER=PATH")
    left, right = raw.split("=", 1)
    order = int(left)
    path = Path(right)
    if order < 4 or order > MAX_ORDER:
        raise argparse.ArgumentTypeError(f"order must be in [4,{MAX_ORDER}]")
    return order, path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run independent cycle-blind random legal-walk HEG searches over "
            "several graph orders while printing only running T(n) minima."
        )
    )
    parser.add_argument(
        "--orders",
        type=parse_orders,
        default=DEFAULT_ORDERS,
        help="comma/range syntax; default: 23,25-31",
    )
    parser.add_argument("--seconds-per-order", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--candidates-per-worker", type=int, default=8)
    parser.add_argument("--walk-min", type=int, default=4)
    parser.add_argument("--walk-max", type=int, default=48)
    parser.add_argument("--walk-retries", type=int, default=8)
    parser.add_argument("--remove-trials", type=int, default=64)
    parser.add_argument("--max-edges", type=int, default=0)
    parser.add_argument("--elite-parent-prob", type=float, default=0.0)
    parser.add_argument("--root-parent-prob", type=float, default=0.05)
    parser.add_argument("--reservoir-size", type=int, default=2048)
    parser.add_argument("--elite-size", type=int, default=64)
    parser.add_argument(
        "--log-total",
        type=int,
        default=16,
        help="child saves every new unique exact graph with TOTAL <= threshold",
    )
    parser.add_argument(
        "--stop-total",
        type=int,
        default=0,
        help="stop an order if exact TOTAL <= this; default 0 = counterexample only",
    )
    parser.add_argument(
        "--stop-all-on-counterexample",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="stop the whole sweep if any order reaches exact TOTAL=0",
    )
    parser.add_argument("--node-budget", type=int, default=10_000_000)
    parser.add_argument("--witness-cap", type=int, default=1_000_000)
    parser.add_argument("--report-seconds", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=240816)
    parser.add_argument(
        "--child-script",
        type=Path,
        default=Path("scripts/heg_random_legal_mutator.py"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("random_order_sweep_23_31"),
    )
    parser.add_argument(
        "--seed-dir",
        action="append",
        type=Path,
        default=[],
        help=(
            "additional directory recursively searched for exact graph JSONs; "
            "may be repeated"
        ),
    )
    parser.add_argument(
        "--fresh-seeds",
        action="store_true",
        help="ignore prior result files and use deterministic legal seeds",
    )
    parser.add_argument(
        "--start",
        action="append",
        type=parse_explicit_start,
        default=[],
        metavar="ORDER=PATH",
        help="explicit start graph override for one order; may be repeated",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.seconds_per_order <= 0:
        parser.error("--seconds-per-order must be > 0")
    if args.workers < 1 or args.candidates_per_worker < 1:
        parser.error("worker counts must be >= 1")
    if args.walk_min < 1 or args.walk_max < args.walk_min:
        parser.error("require 1 <= --walk-min <= --walk-max")
    if args.walk_retries < 1 or args.remove_trials < 1:
        parser.error("retry/trial counts must be >= 1")
    if args.max_edges < 0:
        parser.error("--max-edges must be >= 0")
    if not 0.0 <= args.elite_parent_prob <= 1.0:
        parser.error("--elite-parent-prob must be in [0,1]")
    if not 0.0 <= args.root_parent_prob <= 1.0:
        parser.error("--root-parent-prob must be in [0,1]")
    if args.elite_parent_prob + args.root_parent_prob > 1.0:
        parser.error("elite + root parent probabilities must sum to <= 1")
    if args.reservoir_size < 1 or args.elite_size < 1:
        parser.error("pool sizes must be >= 1")
    if args.log_total < 0 or args.stop_total < 0:
        parser.error("TOTAL thresholds must be >= 0")
    if args.node_budget < 1 or args.witness_cap < 2:
        parser.error("invalid scorer limits")
    if args.report_seconds <= 0:
        parser.error("--report-seconds must be > 0")

    explicit_orders = [order for order, _ in args.start]
    if len(explicit_orders) != len(set(explicit_orders)):
        parser.error("duplicate --start override for the same order")
    return args


def norm_edge(u: int, v: int) -> tuple[int, int]:
    if u == v:
        raise ValueError("self-loop")
    return (u, v) if u < v else (v, u)


def graph_is_legal(order: int, raw_edges: Iterable[Iterable[int]]) -> tuple[bool, tuple[tuple[int, int], ...]]:
    try:
        edges = tuple(sorted({norm_edge(int(e[0]), int(e[1])) for e in raw_edges}))
    except (TypeError, ValueError, IndexError):
        return False, ()

    # Reject duplicates rather than silently normalizing them away.
    raw_list = list(raw_edges) if not isinstance(raw_edges, list) else raw_edges
    if len(edges) != len(raw_list):
        return False, ()
    if any(u < 0 or v >= order for u, v in edges):
        return False, ()

    degree = [0] * order
    adjacency: list[list[int]] = [[] for _ in range(order)]
    for u, v in edges:
        degree[u] += 1
        degree[v] += 1
        adjacency[u].append(v)
        adjacency[v].append(u)
    if min(degree, default=0) < 3:
        return False, ()

    seen = {0}
    stack = [0]
    while stack:
        u = stack.pop()
        for v in adjacency[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return len(seen) == order, edges


def deterministic_legal_seed(order: int) -> tuple[tuple[int, int], ...]:
    """Create a simple connected delta>=3 graph at minimum edge count."""
    edges: set[tuple[int, int]] = {
        norm_edge(i, (i + 1) % order) for i in range(order)
    }

    if order % 2 == 0:
        half = order // 2
        for i in range(half):
            edges.add(norm_edge(i, i + half))
    else:
        # Perfect matching on vertices 0..order-2 plus one extra chord from the
        # unmatched last vertex. This gives degree sequence 4,3,3,... and
        # exactly ceil(3n/2) edges.
        even_part = order - 1
        half = even_part // 2
        for i in range(half):
            edges.add(norm_edge(i, i + half))
        unmatched = order - 1
        target = half
        edges.add(norm_edge(unmatched, target))

    legal, normalized = graph_is_legal(order, [list(edge) for edge in edges])
    if not legal:
        raise RuntimeError(f"internal seed construction failed for order {order}")
    expected_min_edges = math.ceil(3 * order / 2)
    if len(normalized) != expected_min_edges:
        raise RuntimeError(
            f"seed order {order}: expected {expected_min_edges} edges, got {len(normalized)}"
        )
    return normalized


def exact_total_from_payload(payload: dict[str, Any]) -> int | None:
    candidates: list[Any] = []
    if "total" in payload:
        candidates.append(payload.get("total"))
    score = payload.get("score")
    if isinstance(score, dict):
        candidates.append(score.get("total"))
    bootstrap = payload.get("bootstrap_score")
    if isinstance(bootstrap, dict):
        candidates.append(bootstrap.get("total"))

    for value in candidates:
        if isinstance(value, int):
            return int(value)
        if isinstance(value, dict):
            lower = value.get("lower")
            upper = value.get("upper")
            if isinstance(lower, int) and isinstance(upper, int) and lower == upper:
                return int(lower)
    return None


def weighted_from_payload(payload: dict[str, Any]) -> int | None:
    candidates: list[Any] = []
    if "weighted" in payload:
        candidates.append(payload.get("weighted"))
    score = payload.get("score")
    if isinstance(score, dict):
        candidates.append(score.get("weighted"))
    bootstrap = payload.get("bootstrap_score")
    if isinstance(bootstrap, dict):
        candidates.append(bootstrap.get("weighted"))
    for value in candidates:
        if isinstance(value, int):
            return int(value)
    return None


def inspect_seed(path: Path, order: int) -> SeedChoice | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("order") != order or not isinstance(payload.get("edges"), list):
        return None
    legal, edges = graph_is_legal(order, payload["edges"])
    if not legal:
        return None
    total = exact_total_from_payload(payload)
    if total is None:
        return None
    return SeedChoice(
        order=order,
        path=str(path),
        source="discovered",
        reported_total=total,
        reported_weighted=weighted_from_payload(payload),
        edge_count=len(edges),
    )


def likely_seed_paths(repo_root: Path, order: int) -> list[Path]:
    paths: list[Path] = []
    known = [
        repo_root / "order_sweep_23_30" / f"order_{order}" / "best.json",
        repo_root / "order_sweep_23_30" / "bootstrap" / f"order_{order}.json",
        repo_root / f"random_walk_best_n{order}.json",
        repo_root / f"markstroem_slack_best_n{order}.json",
        repo_root / f"markstroem_c8_best_n{order}.json",
    ]
    paths.extend(path for path in known if path.is_file())

    # Shallow root scan catches user-renamed best graphs without traversing a
    # potentially huge workspace tree.
    patterns = (f"*n{order}*.json", f"*_{order}.json", f"*order*{order}*.json")
    for pattern in patterns:
        paths.extend(repo_root.glob(pattern))
    return paths


def discover_best_seed(
    repo_root: Path,
    order: int,
    extra_dirs: Iterable[Path],
) -> SeedChoice | None:
    seen_paths: set[Path] = set()
    candidates: list[SeedChoice] = []

    for path in likely_seed_paths(repo_root, order):
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        item = inspect_seed(path, order)
        if item is not None:
            candidates.append(item)

    for directory in extra_dirs:
        base = directory if directory.is_absolute() else repo_root / directory
        if not base.exists():
            continue
        for path in base.rglob("*.json"):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            item = inspect_seed(path, order)
            if item is not None:
                candidates.append(item)

    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            item.reported_total if item.reported_total is not None else math.inf,
            item.reported_weighted if item.reported_weighted is not None else math.inf,
            item.edge_count,
            item.path,
        ),
    )


def write_generated_seed(path: Path, order: int) -> SeedChoice:
    edges = deterministic_legal_seed(order)
    payload = {
        "schema_version": "heg.random_order_sweep.seed.v1",
        "order": order,
        "edges": [list(edge) for edge in edges],
        "seed_kind": "deterministic_cycle_plus_matching",
        "constraints": {
            "simple": True,
            "connected": True,
            "minimum_degree": 3,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SeedChoice(
        order=order,
        path=str(path),
        source="generated",
        reported_total=None,
        reported_weighted=None,
        edge_count=len(edges),
    )


def resolve_seed(
    *,
    repo_root: Path,
    output_dir: Path,
    order: int,
    explicit: dict[int, Path],
    fresh: bool,
    extra_dirs: Iterable[Path],
) -> SeedChoice:
    explicit_path = explicit.get(order)
    if explicit_path is not None:
        path = explicit_path if explicit_path.is_absolute() else repo_root / explicit_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read explicit seed for n={order}: {path}: {exc}") from exc
        if payload.get("order") != order or not isinstance(payload.get("edges"), list):
            raise RuntimeError(f"explicit seed has wrong/missing graph data: {path}")
        legal, edges = graph_is_legal(order, payload["edges"])
        if not legal:
            raise RuntimeError(f"explicit seed is not simple/connected/delta>=3: {path}")
        return SeedChoice(
            order=order,
            path=str(path),
            source="explicit",
            reported_total=exact_total_from_payload(payload),
            reported_weighted=weighted_from_payload(payload),
            edge_count=len(edges),
        )

    if not fresh:
        discovered = discover_best_seed(repo_root, order, extra_dirs)
        if discovered is not None:
            return discovered

    seed_path = output_dir / f"order_{order}" / "generated_seed.json"
    return write_generated_seed(seed_path, order)


def read_best_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def best_fields(payload: dict[str, Any] | None) -> tuple[
    int | None,
    int | None,
    dict[str, int] | None,
    int | None,
    str | None,
    int | None,
    int | None,
]:
    if payload is None:
        return None, None, None, None, None, None, None
    score = payload.get("score")
    if not isinstance(score, dict):
        score = {}
    total = score.get("total") if isinstance(score.get("total"), int) else None
    weighted = score.get("weighted") if isinstance(score.get("weighted"), int) else None
    components_raw = score.get("components")
    components: dict[str, int] | None = None
    if isinstance(components_raw, dict):
        components = {}
        for key, value in components_raw.items():
            if isinstance(value, dict) and isinstance(value.get("observed"), int):
                components[str(key)] = int(value["observed"])
    edges = payload.get("edges")
    edge_count = len(edges) if isinstance(edges, list) else None
    graph_hash = payload.get("graph_hash") if isinstance(payload.get("graph_hash"), str) else None
    experiment = payload.get("experiment")
    evaluated = exact = None
    if isinstance(experiment, dict):
        if isinstance(experiment.get("evaluated"), int):
            evaluated = int(experiment["evaluated"])
        if isinstance(experiment.get("exact"), int):
            exact = int(experiment["exact"])
    return total, weighted, components, edge_count, graph_hash, evaluated, exact


def child_command(
    *,
    python: str,
    child_script: Path,
    seed: SeedChoice,
    order: int,
    order_dir: Path,
    args: argparse.Namespace,
) -> list[str]:
    return [
        python,
        str(child_script),
        "--start-graph", str(seed.path),
        "--expected-order", str(order),
        "--workers", str(args.workers),
        "--candidates-per-worker", str(args.candidates_per_worker),
        "--walk-min", str(args.walk_min),
        "--walk-max", str(args.walk_max),
        "--walk-retries", str(args.walk_retries),
        "--remove-trials", str(args.remove_trials),
        "--max-edges", str(args.max_edges),
        "--elite-parent-prob", str(args.elite_parent_prob),
        "--root-parent-prob", str(args.root_parent_prob),
        "--reservoir-size", str(args.reservoir_size),
        "--elite-size", str(args.elite_size),
        "--success-total", str(args.stop_total),
        "--log-total", str(args.log_total),
        "--total-seconds", str(args.seconds_per_order),
        "--report-seconds", str(args.report_seconds),
        "--seed", str(args.seed + order * 1_000_003),
        "--node-budget", str(args.node_budget),
        "--witness-cap", str(args.witness_cap),
        "--save-best", str(order_dir / "best.json"),
        "--save-hits", str(order_dir / "hits.jsonl"),
        "--save-pool", str(order_dir / "pool.json"),
    ]


def run_order(
    *,
    repo_root: Path,
    child_script: Path,
    order: int,
    seed: SeedChoice,
    output_dir: Path,
    args: argparse.Namespace,
) -> OrderSummary:
    order_dir = output_dir / f"order_{order}"
    order_dir.mkdir(parents=True, exist_ok=True)
    log_path = order_dir / "run.log"
    best_path = order_dir / "best.json"
    hits_path = order_dir / "hits.jsonl"
    pool_path = order_dir / "pool.json"

    command = child_command(
        python=sys.executable,
        child_script=child_script,
        seed=seed,
        order=order,
        order_dir=order_dir,
        args=args,
    )

    invocation = {
        "order": order,
        "seed": asdict(seed),
        "command": command,
        "cwd": str(repo_root),
        "started_unix": time.time(),
    }
    (order_dir / "invocation.json").write_text(
        json.dumps(invocation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if args.dry_run:
        return OrderSummary(
            order=order,
            status="DRY_RUN",
            seed=seed,
            initial_total=seed.reported_total,
            best_total=seed.reported_total,
            best_weighted=seed.reported_weighted,
            best_components=None,
            best_edge_count=seed.edge_count,
            best_hash=None,
            evaluated=None,
            exact=None,
            elapsed_seconds=0.0,
            child_exit_code=0,
            run_log=str(log_path),
            best_graph=str(best_path),
            hits_log=str(hits_path),
            pool_file=str(pool_path),
        )

    started = time.perf_counter()
    running_t: int | None = None
    initial_total: int | None = None

    env = os.environ.copy()
    # Make Rich child output plain/stable in the captured log.
    env.setdefault("NO_COLOR", "1")

    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()

            match = START_RE.search(line)
            if match:
                value = int(match.group(1))
                initial_total = value
                if running_t is None or value < running_t:
                    running_t = value
                    print(f"T({order})={value}", flush=True)
                continue

            match = NEW_BEST_RE.search(line)
            if match:
                value = int(match.group(1))
                # Child may emit tie-break improvements at equal TOTAL. stdout
                # intentionally reports T(n) only, so suppress equal values.
                if running_t is None or value < running_t:
                    running_t = value
                    print(f"T({order})={value}", flush=True)
                continue

            match = DONE_RE.search(line)
            if match:
                value = int(match.group(1))
                if running_t is None or value < running_t:
                    running_t = value
                    print(f"T({order})={value}", flush=True)

        exit_code = process.wait()

    elapsed = time.perf_counter() - started
    payload = read_best_payload(best_path)
    (
        best_total,
        best_weighted,
        best_components,
        best_edge_count,
        best_hash,
        evaluated,
        exact,
    ) = best_fields(payload)

    status = "OK" if exit_code == 0 else "FAILED"
    return OrderSummary(
        order=order,
        status=status,
        seed=seed,
        initial_total=initial_total,
        best_total=best_total if best_total is not None else running_t,
        best_weighted=best_weighted,
        best_components=best_components,
        best_edge_count=best_edge_count,
        best_hash=best_hash,
        evaluated=evaluated,
        exact=exact,
        elapsed_seconds=elapsed,
        child_exit_code=exit_code,
        run_log=str(log_path),
        best_graph=str(best_path),
        hits_log=str(hits_path),
        pool_file=str(pool_path),
    )


def write_summary(path: Path, summaries: list[OrderSummary], args: argparse.Namespace) -> None:
    payload = {
        "schema_version": "heg.random_order_sweep.v1",
        "orders": [summary.order for summary in summaries],
        "seconds_per_order": args.seconds_per_order,
        "mutation": "cycle_blind_ADD_REMOVE_legal_walk",
        "parent_selection": {
            "elite_parent_probability": args.elite_parent_prob,
            "root_parent_probability": args.root_parent_prob,
            "reservoir_size": args.reservoir_size,
        },
        "scoring": {
            "node_budget": args.node_budget,
            "witness_cap": args.witness_cap,
            "exact_only_for_T": True,
        },
        "results": [asdict(summary) for summary in summaries],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    repo_root = Path.cwd().resolve()
    output_dir = args.output_dir if args.output_dir.is_absolute() else repo_root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    child_script = args.child_script if args.child_script.is_absolute() else repo_root / args.child_script
    if not child_script.is_file() and not args.dry_run:
        print(f"missing child script: {child_script}", file=sys.stderr)
        return 2

    explicit = {order: path for order, path in args.start}
    summaries: list[OrderSummary] = []

    for order in args.orders:
        try:
            seed = resolve_seed(
                repo_root=repo_root,
                output_dir=output_dir,
                order=order,
                explicit=explicit,
                fresh=args.fresh_seeds,
                extra_dirs=args.seed_dir,
            )
            summary = run_order(
                repo_root=repo_root,
                child_script=child_script,
                order=order,
                seed=seed,
                output_dir=output_dir,
                args=args,
            )
        except Exception as exc:
            print(f"n={order} failed: {exc}", file=sys.stderr, flush=True)
            return 2

        summaries.append(summary)
        write_summary(output_dir / "summary.json", summaries, args)

        if summary.child_exit_code != 0:
            print(
                f"n={order} child failed; see {summary.run_log}",
                file=sys.stderr,
                flush=True,
            )
            return summary.child_exit_code or 2

        if (
            args.stop_all_on_counterexample
            and summary.best_total is not None
            and summary.best_total == 0
        ):
            break

    write_summary(output_dir / "summary.json", summaries, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
