#!/usr/bin/env python3
"""
Paired plateau-only benchmark for HEG compactness elite ranking.

This benchmark conditions each repeat on one common low-F graph before comparing
selection metrics.  The warm-up phase is not measured.  It searches for a graph
with exactly the configured plateau F using the baseline cycle-blind walk.

Every measured arm then starts from that exact same graph and uses ELITE-only
parent selection:

    static selection, elite_parent_prob=1, root_parent_prob=0

Thus compactness can affect parent choice after the first candidate batch instead
of being bypassed by a direct jump from a high-F random phase to the target.

Defaults:
    n=10: start at F=6, target F=4
    n=11: start at F=4, target F=2

Example:
    uv run python scripts/heg_compactness_plateau_sweep.py \
      --repeats 5 --seconds-per-run 10 --workers 16
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
DEFAULT_CASES = ((10, 6, 4), (11, 4, 2))
MAX_ORDER = 128


@dataclass(frozen=True, slots=True)
class ArmResult:
    order: int
    repeat: int
    plateau_total: int
    target: int
    metric: str
    plateau_graph: str
    search_seed: int
    reached_target: bool
    time_to_target_seconds: float | None
    evaluated_to_target: int | None
    best_total: int | None
    evaluated: int | None
    elapsed_seconds: float
    child_exit_code: int
    run_log: str
    compactness_stats: str


def parse_case(raw: str) -> tuple[int, int, int]:
    try:
        order_raw, plateau_raw, target_raw = raw.split(":", 2)
        order, plateau, target = int(order_raw), int(plateau_raw), int(target_raw)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("case must be ORDER:PLATEAU:TARGET") from exc
    if not 4 <= order <= MAX_ORDER:
        raise argparse.ArgumentTypeError(f"order must be in [4,{MAX_ORDER}]")
    if target < 0 or plateau <= target:
        raise argparse.ArgumentTypeError("require 0 <= TARGET < PLATEAU")
    return order, plateau, target


def parse_metrics(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = sorted(set(values) - set(METRICS))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown metrics: {unknown}")
    if not values:
        raise argparse.ArgumentTypeError("at least one metric is required")
    return values


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--case",
        action="append",
        type=parse_case,
        default=[],
        metavar="ORDER:PLATEAU:TARGET",
        help="Repeatable; defaults to 10:6:4 and 11:4:2.",
    )
    p.add_argument("--metrics", type=parse_metrics, default=METRICS)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--seconds-per-run", type=float, default=10.0)
    p.add_argument("--warmup-seconds", type=float, default=10.0)
    p.add_argument("--warmup-attempts", type=int, default=12)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--candidates-per-worker", type=int, default=8)
    p.add_argument("--walk-min", type=int, default=4)
    p.add_argument("--walk-max", type=int, default=48)
    p.add_argument("--walk-retries", type=int, default=8)
    p.add_argument("--remove-trials", type=int, default=64)
    p.add_argument("--reservoir-size", type=int, default=2048)
    p.add_argument("--elite-size", type=int, default=64)
    p.add_argument(
        "--compactness-placement",
        choices=("before-weighted", "after-weighted"),
        default="before-weighted",
    )
    p.add_argument("--geometry-node-budget", type=int, default=5_000_000)
    p.add_argument("--node-budget", type=int, default=10_000_000)
    p.add_argument("--witness-cap", type=int, default=1_000_000)
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
        default=Path("results/sweeps/compactness_plateau_only"),
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.repeats < 1 or args.warmup_attempts < 1:
        p.error("repeat/attempt counts must be >= 1")
    if args.seconds_per_run <= 0 or args.warmup_seconds <= 0:
        p.error("time limits must be > 0")
    if args.workers < 1 or args.candidates_per_worker < 1:
        p.error("worker counts must be >= 1")
    if args.walk_min < 1 or args.walk_max < args.walk_min:
        p.error("require 1 <= --walk-min <= --walk-max")
    if args.geometry_node_budget < 1 or args.node_budget < 1 or args.witness_cap < 2:
        p.error("invalid scorer/geometry limits")
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


def write_graph(path: Path, order: int, edges: Iterable[Sequence[int]], metadata: dict[str, Any]) -> None:
    payload = {
        "schema_version": "graphoratory.heg_compactness_plateau_seed.v1",
        "order": order,
        "edges": [[int(edge[0]), int(edge[1])] for edge in edges],
        "plateau_seed": metadata,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def first_exact_total(path: Path, wanted: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("total", -1)) == wanted and isinstance(row.get("edges"), list):
                return row
    return None


def read_best(path: Path) -> tuple[int | None, int | None, float | None]:
    if not path.exists():
        return None, None, None
    payload = json.loads(path.read_text(encoding="utf-8"))
    score = payload.get("score") if isinstance(payload.get("score"), dict) else {}
    exp = payload.get("experiment") if isinstance(payload.get("experiment"), dict) else {}
    total = score.get("total") if isinstance(score.get("total"), int) else None
    evaluated = exp.get("evaluated") if isinstance(exp.get("evaluated"), int) else None
    elapsed = (
        float(exp["elapsed_seconds"])
        if isinstance(exp.get("elapsed_seconds"), (int, float))
        else None
    )
    return total, evaluated, elapsed


def first_target_hit(path: Path, target: int) -> tuple[float, int] | None:
    if not path.exists():
        return None
    best: tuple[float, int] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
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


def run_child(cmd: list[str], log: Path) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.perf_counter()
    proc = subprocess.run(cmd, text=True, capture_output=True)
    wall = time.perf_counter() - started
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        proc.stdout + ("\n--- STDERR ---\n" + proc.stderr if proc.stderr else ""),
        encoding="utf-8",
    )
    return proc, wall


def base_search_args(args: argparse.Namespace, order: int) -> list[str]:
    return [
        "--expected-order", str(order),
        "--workers", str(args.workers),
        "--candidates-per-worker", str(args.candidates_per_worker),
        "--walk-min", str(args.walk_min),
        "--walk-max", str(args.walk_max),
        "--walk-retries", str(args.walk_retries),
        "--remove-trials", str(args.remove_trials),
        "--reservoir-size", str(args.reservoir_size),
        "--elite-size", str(args.elite_size),
        "--node-budget", str(args.node_budget),
        "--witness-cap", str(args.witness_cap),
        "--report-seconds", "60",
    ]


def prepare_plateau(
    *,
    args: argparse.Namespace,
    helper: Any,
    child: Path,
    order: int,
    plateau: int,
    target: int,
    repeat: int,
    repeat_dir: Path,
) -> Path:
    plateau_path = repeat_dir / "plateau.json"
    for attempt in range(1, args.warmup_attempts + 1):
        warm_seed = args.seed + order * 1_000_003 + repeat * 10_007 + attempt * 1_009
        seed_edges = helper.random_minimal_legal_seed(order, warm_seed)
        attempt_dir = repeat_dir / "warmup" / f"attempt-{attempt:02d}"
        start_path = attempt_dir / "start.json"
        hits = attempt_dir / "hits.jsonl"
        best = attempt_dir / "best.json"
        pool = attempt_dir / "pool.json"
        log = attempt_dir / "run.log"
        write_graph(
            start_path,
            order,
            seed_edges,
            {"kind": "warmup_root", "seed": warm_seed, "attempt": attempt},
        )

        cmd = [
            sys.executable,
            str(child),
            "--compactness-metric", "baseline",
            "--start-graph", str(start_path),
            *base_search_args(args, order),
            "--selection-mode", "static",
            "--elite-parent-prob", "0",
            "--root-parent-prob", "0.05",
            "--success-total", str(plateau),
            "--log-total", str(plateau),
            "--total-seconds", str(args.warmup_seconds),
            "--seed", str(warm_seed ^ 0x6A09E667),
            "--save-best", str(best),
            "--save-hits", str(hits),
            "--save-pool", str(pool),
        ]
        if args.dry_run:
            print("WARMUP DRY", " ".join(cmd), flush=True)
            write_graph(
                plateau_path,
                order,
                seed_edges,
                {"kind": "dry_run_placeholder", "plateau_total": plateau, "target": target},
            )
            return plateau_path

        proc, _wall = run_child(cmd, log)
        row = first_exact_total(hits, plateau)
        if proc.returncode == 0 and row is not None:
            write_graph(
                plateau_path,
                order,
                row["edges"],
                {
                    "kind": "conditioned_exact_plateau",
                    "plateau_total": plateau,
                    "target": target,
                    "warmup_seed": warm_seed,
                    "warmup_attempt": attempt,
                    "source_elapsed_seconds": row.get("elapsed_seconds"),
                    "source_evaluated": row.get("evaluated"),
                    "source_graph_hash": row.get("graph_hash"),
                },
            )
            print(
                f"  plateau n={order} repeat={repeat} F={plateau} "
                f"attempt={attempt} warmup_eval={row.get('evaluated')} "
                f"warmup_t={row.get('elapsed_seconds')}",
                flush=True,
            )
            return plateau_path

    raise RuntimeError(
        f"could not condition n={order} repeat={repeat} on exact F={plateau} "
        f"after {args.warmup_attempts} attempts"
    )


def aggregate(results: Sequence[ArmResult]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str], list[ArmResult]] = defaultdict(list)
    for row in results:
        groups[(row.order, row.metric)].append(row)
    out: list[dict[str, Any]] = []
    for (order, metric), rows in sorted(groups.items()):
        successes = [row for row in rows if row.reached_target]
        out.append(
            {
                "order": order,
                "metric": metric,
                "runs": len(rows),
                "successes": len(successes),
                "success_rate": len(successes) / len(rows),
                "plateau_total": rows[0].plateau_total,
                "target": rows[0].target,
                "median_time_to_target_seconds": median_or_none(
                    [
                        float(row.time_to_target_seconds)
                        for row in successes
                        if row.time_to_target_seconds is not None
                    ]
                ),
                "median_evaluated_to_target": median_or_none(
                    [
                        float(row.evaluated_to_target)
                        for row in successes
                        if row.evaluated_to_target is not None
                    ]
                ),
            }
        )
    return out


def main() -> int:
    args = parse_args()
    cases = tuple(args.case) if args.case else DEFAULT_CASES
    repo = Path.cwd()
    child = args.child_script if args.child_script.is_absolute() else repo / args.child_script
    helper_path = args.seed_helper if args.seed_helper.is_absolute() else repo / args.seed_helper
    if not child.is_file():
        raise SystemExit(f"missing child script: {child}")
    if not helper_path.is_file():
        raise SystemExit(f"missing seed helper: {helper_path}")

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()) and not args.force:
        raise SystemExit(f"output directory is not empty: {output}; use a fresh path or --force")
    output.mkdir(parents=True, exist_ok=True)
    helper = load_module(helper_path, "heg_compactness_plateau_seed_helper")

    results: list[ArmResult] = []
    for order, plateau, target in cases:
        print(
            f"\n=== n={order} conditioned_plateau={plateau} target={target} "
            "selection=ELITE_ONLY ===",
            flush=True,
        )
        for repeat in range(1, args.repeats + 1):
            repeat_dir = output / f"order-{order}" / f"repeat-{repeat:02d}"
            plateau_path = prepare_plateau(
                args=args,
                helper=helper,
                child=child,
                order=order,
                plateau=plateau,
                target=target,
                repeat=repeat,
                repeat_dir=repeat_dir,
            )
            search_seed = args.seed ^ (order * 0x9E3779B1) ^ (repeat * 0x85EBCA6B)
            metric_order = list(args.metrics)
            random.Random(search_seed ^ 0xC2B2AE35).shuffle(metric_order)

            for metric in metric_order:
                arm = repeat_dir / metric
                best = arm / "best.json"
                hits = arm / "hits.jsonl"
                pool = arm / "pool.json"
                stats = arm / "compactness.json"
                log = arm / "run.log"
                cmd = [
                    sys.executable,
                    str(child),
                    "--compactness-metric", metric,
                    "--compactness-threshold", str(plateau),
                    "--compactness-placement", args.compactness_placement,
                    "--geometry-node-budget", str(args.geometry_node_budget),
                    "--compactness-stats", str(stats),
                    "--start-graph", str(plateau_path),
                    *base_search_args(args, order),
                    "--selection-mode", "static",
                    "--elite-parent-prob", "1",
                    "--root-parent-prob", "0",
                    "--success-total", str(target),
                    "--log-total", str(plateau),
                    "--total-seconds", str(args.seconds_per_run),
                    "--seed", str(search_seed),
                    "--save-best", str(best),
                    "--save-hits", str(hits),
                    "--save-pool", str(pool),
                ]
                if args.dry_run:
                    print("ARM DRY", " ".join(cmd), flush=True)
                    continue

                proc, wall = run_child(cmd, log)
                best_total, evaluated, child_elapsed = read_best(best)
                hit = first_target_hit(hits, target)
                reached = bool(best_total is not None and best_total <= target)
                if reached and hit is None:
                    hit = (0.0, 0)
                result = ArmResult(
                    order=order,
                    repeat=repeat,
                    plateau_total=plateau,
                    target=target,
                    metric=metric,
                    plateau_graph=str(plateau_path),
                    search_seed=search_seed,
                    reached_target=reached,
                    time_to_target_seconds=None if hit is None else hit[0],
                    evaluated_to_target=None if hit is None else hit[1],
                    best_total=best_total,
                    evaluated=evaluated,
                    elapsed_seconds=child_elapsed if child_elapsed is not None else wall,
                    child_exit_code=proc.returncode,
                    run_log=str(log),
                    compactness_stats=str(stats),
                )
                results.append(result)
                print(
                    f"  {metric:<14} target={'YES' if reached else 'NO ':<3} "
                    f"t={result.time_to_target_seconds if result.time_to_target_seconds is not None else '-'} "
                    f"eval={result.evaluated_to_target if result.evaluated_to_target is not None else '-'} "
                    f"best={best_total} rc={proc.returncode}",
                    flush=True,
                )

    if args.dry_run:
        return 0

    aggregate_rows = aggregate(results)
    summary = {
        "schema_version": "graphoratory.heg_compactness_plateau_sweep.v1",
        "cases": [
            {"order": order, "plateau_total": plateau, "target": target}
            for order, plateau, target in cases
        ],
        "metrics": list(args.metrics),
        "repeats": args.repeats,
        "seconds_per_run": args.seconds_per_run,
        "warmup_seconds": args.warmup_seconds,
        "warmup_attempts": args.warmup_attempts,
        "workers": args.workers,
        "selection_mode": "elite_only",
        "compactness_placement": args.compactness_placement,
        "geometry_node_budget": args.geometry_node_budget,
        "runs": [asdict(row) for row in results],
        "aggregate": aggregate_rows,
    }
    summary_path = output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    csv_path = output / "runs.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for row in results:
            writer.writerow(asdict(row))

    print("\n=== AGGREGATE ===")
    for row in aggregate_rows:
        print(
            f"n={row['order']} F={row['plateau_total']}->{row['target']} "
            f"{row['metric']:<14} success={row['successes']}/{row['runs']} "
            f"median_t={row['median_time_to_target_seconds']} "
            f"median_eval={row['median_evaluated_to_target']}"
        )
    print(f"summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
