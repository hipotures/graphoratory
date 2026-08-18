#!/usr/bin/env python3
"""
Paired benchmark for HEG compactness-guided elite ranking.

For each (order, repeat), one deterministic fresh minimum-edge legal start graph
is generated and reused by every metric arm.  Arms differ only in elite
ranking; mutation, scoring, random/elite phase schedule, time budget, target,
and RNG seed are matched.

Recommended first experiment uses the internally exact targets:

  F(10)=4, F(11)=2

Example:

  uv run python scripts/heg_compactness_benchmark.py \
    --orders 10,11 \
    --metrics baseline,cycle-min,vertex-mean,edge-potential \
    --target 10=4 --target 11=2 \
    --repeats 5 --seconds-per-run 20 \
    --workers 16 --phase-seconds 5 \
    --compactness-threshold 4 \
    --output-dir results/sweeps/compactness_10_11

The main comparison is paired time/evaluations to the target.  Because every
arm receives the same seed graph and search RNG seed within a repeat, variance
from initialization is reduced.  Metric arm execution order is shuffled per
repeat to reduce systematic thermal/order bias.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import random
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


METRICS = ("baseline", "cycle-min", "vertex-mean", "edge-potential")
MAX_ORDER = 128


@dataclass(frozen=True, slots=True)
class ArmResult:
    order: int
    repeat: int
    metric: str
    target: int
    seed_graph: str
    search_seed: int
    status: str
    reached_target: bool
    time_to_target_seconds: float | None
    evaluated_to_target: int | None
    best_total: int | None
    best_weighted: int | None
    evaluated: int | None
    exact: int | None
    elapsed_seconds: float
    child_exit_code: int
    run_log: str
    best_graph: str
    hits_log: str
    compactness_stats: str


def parse_orders(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if hi < lo:
                raise argparse.ArgumentTypeError(f"descending range: {part}")
            values.extend(range(lo, hi + 1))
        else:
            values.append(int(part))
    out = tuple(sorted(set(values)))
    if not out or out[0] < 4 or out[-1] > MAX_ORDER:
        raise argparse.ArgumentTypeError(f"orders must be in [4,{MAX_ORDER}]")
    return out


def parse_metrics(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(x.strip() for x in raw.split(",") if x.strip()))
    unknown = sorted(set(values) - set(METRICS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown metrics: {unknown}")
    if not values:
        raise argparse.ArgumentTypeError("at least one metric is required")
    return values


def parse_target(raw: str) -> tuple[int, int]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("target must be ORDER=F")
    a, b = raw.split("=", 1)
    order, target = int(a), int(b)
    if order < 4 or order > MAX_ORDER or target < 0:
        raise argparse.ArgumentTypeError("invalid ORDER=F target")
    return order, target


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--orders", type=parse_orders, default=(10, 11))
    p.add_argument(
        "--metrics",
        type=parse_metrics,
        default=METRICS,
    )
    p.add_argument(
        "--target",
        type=parse_target,
        action="append",
        default=[],
        metavar="ORDER=F",
        help="Known/desired target for an order; unspecified orders default to F=0.",
    )
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--seconds-per-run", type=float, default=20.0)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--candidates-per-worker", type=int, default=8)
    p.add_argument("--walk-min", type=int, default=4)
    p.add_argument("--walk-max", type=int, default=48)
    p.add_argument("--walk-retries", type=int, default=8)
    p.add_argument("--remove-trials", type=int, default=64)
    p.add_argument("--max-edges", type=int, default=0)
    p.add_argument("--phase-seconds", type=float, default=5.0)
    p.add_argument("--root-parent-prob", type=float, default=0.05)
    p.add_argument("--reservoir-size", type=int, default=2048)
    p.add_argument("--elite-size", type=int, default=64)
    p.add_argument("--compactness-threshold", type=int, default=4)
    p.add_argument(
        "--compactness-placement",
        choices=("before-weighted", "after-weighted"),
        default="before-weighted",
    )
    p.add_argument("--geometry-node-budget", type=int, default=5_000_000)
    p.add_argument("--node-budget", type=int, default=10_000_000)
    p.add_argument("--witness-cap", type=int, default=1_000_000)
    p.add_argument("--report-seconds", type=float, default=10.0)
    p.add_argument("--seed", type=int, default=260818)
    p.add_argument(
        "--child-script",
        type=Path,
        default=Path("scripts/heg_compactness_plateau_mutator.py"),
    )
    p.add_argument(
        "--seed-helper",
        type=Path,
        default=Path("scripts/heg_random_alternating_sweep.py"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/sweeps/compactness_10_11"),
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.repeats < 1:
        p.error("--repeats must be >= 1")
    if args.seconds_per_run <= 0 or args.phase_seconds <= 0:
        p.error("time limits must be > 0")
    if args.workers < 1 or args.candidates_per_worker < 1:
        p.error("worker counts must be >= 1")
    if args.walk_min < 1 or args.walk_max < args.walk_min:
        p.error("require 1 <= --walk-min <= --walk-max")
    if args.compactness_threshold < 0 or args.geometry_node_budget < 1:
        p.error("invalid compactness limits")
    if not 0 <= args.root_parent_prob <= 1:
        p.error("--root-parent-prob must be in [0,1]")
    targets = [order for order, _ in args.target]
    if len(targets) != len(set(targets)):
        p.error("duplicate --target for an order")
    return args


def load_module(path: Path, name: str) -> Any:
    path = path.resolve()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_seed(path: Path, order: int, edges: Iterable[tuple[int, int]], seed: int) -> None:
    payload = {
        "schema_version": "graphoratory.heg_compactness_benchmark_seed.v1",
        "order": order,
        "edges": [list(e) for e in edges],
        "seed": seed,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_best(path: Path) -> tuple[int | None, int | None, int | None, int | None, float | None]:
    if not path.exists():
        return None, None, None, None, None
    p = json.loads(path.read_text(encoding="utf-8"))
    score = p.get("score") if isinstance(p.get("score"), dict) else {}
    exp = p.get("experiment") if isinstance(p.get("experiment"), dict) else {}
    return (
        score.get("total") if isinstance(score.get("total"), int) else None,
        score.get("weighted") if isinstance(score.get("weighted"), int) else None,
        exp.get("evaluated") if isinstance(exp.get("evaluated"), int) else None,
        exp.get("exact") if isinstance(exp.get("exact"), int) else None,
        float(exp["elapsed_seconds"]) if isinstance(exp.get("elapsed_seconds"), (int, float)) else None,
    )


def first_target_hit(path: Path, target: int) -> tuple[float, int] | None:
    if not path.exists():
        return None
    best: tuple[float, int] | None = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("total", 1 << 60)) <= target:
                candidate = (float(row["elapsed_seconds"]), int(row["evaluated"]))
                if best is None or candidate < best:
                    best = candidate
    return best


def median_or_none(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def aggregate(results: Sequence[ArmResult]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str], list[ArmResult]] = defaultdict(list)
    for r in results:
        groups[(r.order, r.metric)].append(r)

    out = []
    for (order, metric), rows in sorted(groups.items()):
        successes = [r for r in rows if r.reached_target]
        times = [r.time_to_target_seconds for r in successes if r.time_to_target_seconds is not None]
        evals_to = [r.evaluated_to_target for r in successes if r.evaluated_to_target is not None]
        bests = [r.best_total for r in rows if r.best_total is not None]
        evaluated = [r.evaluated for r in rows if r.evaluated is not None]
        out.append(
            {
                "order": order,
                "metric": metric,
                "runs": len(rows),
                "target": rows[0].target,
                "successes": len(successes),
                "success_rate": len(successes) / len(rows),
                "median_time_to_target_seconds": median_or_none([float(x) for x in times]),
                "median_evaluated_to_target": median_or_none([float(x) for x in evals_to]),
                "median_best_total": median_or_none([float(x) for x in bests]),
                "median_total_evaluated": median_or_none([float(x) for x in evaluated]),
            }
        )
    return out


def main() -> int:
    args = parse_args()
    repo = Path.cwd()
    child = args.child_script if args.child_script.is_absolute() else repo / args.child_script
    helper_path = args.seed_helper if args.seed_helper.is_absolute() else repo / args.seed_helper
    if not child.is_file():
        raise SystemExit(f"missing child script: {child}")
    if not helper_path.is_file():
        raise SystemExit(f"missing seed helper: {helper_path}")

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()) and not args.force:
        raise SystemExit(f"output directory is not empty: {output}; use --force")
    output.mkdir(parents=True, exist_ok=True)

    helper = load_module(helper_path, "heg_random_alternating_sweep_seed_helper")
    targets = {order: target for order, target in args.target}
    results: list[ArmResult] = []

    for order in args.orders:
        target = targets.get(order, 0)
        for repeat in range(1, args.repeats + 1):
            pair_seed = args.seed + order * 1_000_003 + repeat * 10_007
            seed_edges = helper.random_minimal_legal_seed(order, pair_seed)
            repeat_dir = output / f"order-{order}" / f"repeat-{repeat:02d}"
            seed_path = repeat_dir / "seed.json"
            write_seed(seed_path, order, seed_edges, pair_seed)

            metric_order = list(args.metrics)
            random.Random(pair_seed ^ 0x5A17C0DE).shuffle(metric_order)
            print(
                f"ORDER {order} repeat={repeat}/{args.repeats} target={target} "
                f"metric_order={','.join(metric_order)}",
                flush=True,
            )

            for metric in metric_order:
                arm = repeat_dir / metric
                arm.mkdir(parents=True, exist_ok=True)
                best = arm / "best.json"
                hits = arm / "hits.jsonl"
                pool = arm / "pool.json"
                cstats = arm / "compactness.json"
                log = arm / "run.log"
                search_seed = pair_seed ^ 0x13579BDF

                cmd = [
                    sys.executable,
                    str(child),
                    "--compactness-metric", metric,
                    "--compactness-threshold", str(args.compactness_threshold),
                    "--compactness-placement", args.compactness_placement,
                    "--geometry-node-budget", str(args.geometry_node_budget),
                    "--compactness-stats", str(cstats),
                    "--start-graph", str(seed_path),
                    "--expected-order", str(order),
                    "--workers", str(args.workers),
                    "--candidates-per-worker", str(args.candidates_per_worker),
                    "--walk-min", str(args.walk_min),
                    "--walk-max", str(args.walk_max),
                    "--walk-retries", str(args.walk_retries),
                    "--remove-trials", str(args.remove_trials),
                    "--max-edges", str(args.max_edges),
                    "--selection-mode", "alternating",
                    "--phase-seconds", str(args.phase_seconds),
                    "--root-parent-prob", str(args.root_parent_prob),
                    "--reservoir-size", str(args.reservoir_size),
                    "--elite-size", str(args.elite_size),
                    "--success-total", str(target),
                    "--log-total", str(max(target, args.compactness_threshold)),
                    "--total-seconds", str(args.seconds_per_run),
                    "--report-seconds", str(args.report_seconds),
                    "--seed", str(search_seed),
                    "--node-budget", str(args.node_budget),
                    "--witness-cap", str(args.witness_cap),
                    "--save-best", str(best),
                    "--save-hits", str(hits),
                    "--save-pool", str(pool),
                ]

                if args.dry_run:
                    print("DRY", " ".join(cmd), flush=True)
                    continue

                started = time.perf_counter()
                proc = subprocess.run(cmd, text=True, capture_output=True)
                wall = time.perf_counter() - started
                log.write_text(proc.stdout + ("\n--- STDERR ---\n" + proc.stderr if proc.stderr else ""), encoding="utf-8")

                best_total, best_weighted, evaluated, exact, child_elapsed = read_best(best)
                hit = first_target_hit(hits, target)
                reached = bool(best_total is not None and best_total <= target)
                if reached and hit is None:
                    # This can happen only if the initial seed already meets the
                    # target; treat target time/evaluations as zero.
                    hit = (0.0, 0)

                row = ArmResult(
                    order=order,
                    repeat=repeat,
                    metric=metric,
                    target=target,
                    seed_graph=str(seed_path),
                    search_seed=search_seed,
                    status="OK" if proc.returncode == 0 else "ERROR",
                    reached_target=reached,
                    time_to_target_seconds=None if hit is None else hit[0],
                    evaluated_to_target=None if hit is None else hit[1],
                    best_total=best_total,
                    best_weighted=best_weighted,
                    evaluated=evaluated,
                    exact=exact,
                    elapsed_seconds=child_elapsed if child_elapsed is not None else wall,
                    child_exit_code=proc.returncode,
                    run_log=str(log),
                    best_graph=str(best),
                    hits_log=str(hits),
                    compactness_stats=str(cstats),
                )
                results.append(row)
                print(
                    f"  {metric:<14} target={'YES' if reached else 'NO ':<3} "
                    f"t={row.time_to_target_seconds if row.time_to_target_seconds is not None else '-'} "
                    f"eval={row.evaluated_to_target if row.evaluated_to_target is not None else '-'} "
                    f"best={best_total} total_eval={evaluated} rc={proc.returncode}",
                    flush=True,
                )
                if proc.returncode != 0:
                    print(f"    ERROR log={log}", flush=True)

    if args.dry_run:
        return 0

    aggregate_rows = aggregate(results)
    summary = {
        "schema_version": "graphoratory.heg_compactness_benchmark.v1",
        "orders": list(args.orders),
        "metrics": list(args.metrics),
        "targets": {str(k): v for k, v in sorted(targets.items())},
        "repeats": args.repeats,
        "seconds_per_run": args.seconds_per_run,
        "workers": args.workers,
        "phase_seconds": args.phase_seconds,
        "compactness_threshold": args.compactness_threshold,
        "compactness_placement": args.compactness_placement,
        "geometry_node_budget": args.geometry_node_budget,
        "runs": [asdict(r) for r in results],
        "aggregate": aggregate_rows,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with (output / "runs.csv").open("w", newline="", encoding="utf-8") as f:
        if results:
            writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
            writer.writeheader()
            writer.writerows(asdict(r) for r in results)

    print("\n=== AGGREGATE ===")
    for row in aggregate_rows:
        print(
            f"n={row['order']} {row['metric']:<14} "
            f"success={row['successes']}/{row['runs']} "
            f"median_t={row['median_time_to_target_seconds']} "
            f"median_eval={row['median_evaluated_to_target']} "
            f"median_best={row['median_best_total']}"
        )
    print(f"summary: {output / 'summary.json'}")
    return 0 if all(r.child_exit_code == 0 for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
