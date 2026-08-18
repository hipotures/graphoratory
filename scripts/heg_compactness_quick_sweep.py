#!/usr/bin/env python3
"""
Quick paired HEG compactness sweep on certified small orders.

This driver runs scripts/heg_compactness_benchmark.py separately per order so
the compactness threshold can be tied to the target:

    compactness_threshold = target + threshold_margin

That detail matters.  If n=10 has certified F(10)=4 and the threshold were 4,
compactness would only become active at the exact moment the benchmark stops at
F=4, so it could not possibly help time-to-target.  With the default margin 2,
geometry can guide equal-F plateaus at F=6 and F=5 while F itself remains the
first ranking key.

The underlying mutator changes only ELITE ordering.  Mutation, exact HEG
scoring, reservoir handling, success criterion, and the alternating
random/elite schedule remain unchanged.

Default experiment:
    n=10 target 4, threshold 6
    n=11 target 2, threshold 4
    metrics: baseline, cycle-min, vertex-mean, edge-potential

Example:
    uv run python scripts/heg_compactness_quick_sweep.py \
      --repeats 10 --seconds-per-run 20 --workers 16 \
      --output-dir results/sweeps/compactness_certified

For a cheaper smoke:
    uv run python scripts/heg_compactness_quick_sweep.py \
      --repeats 3 --seconds-per-run 10 --workers 16
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_TARGETS = {10: 4, 11: 2}
DEFAULT_METRICS = "baseline,cycle-min,vertex-mean,edge-potential"


def parse_order_target(raw: str) -> tuple[int, int]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected ORDER=F, e.g. 10=4")
    a, b = raw.split("=", 1)
    order, target = int(a), int(b)
    if order < 4 or target < 0:
        raise argparse.ArgumentTypeError("require ORDER>=4 and F>=0")
    return order, target


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--target",
        action="append",
        type=parse_order_target,
        default=[],
        metavar="ORDER=F",
        help="Repeatable. Defaults to certified 10=4 and 11=2.",
    )
    p.add_argument("--metrics", default=DEFAULT_METRICS)
    p.add_argument("--threshold-margin", type=int, default=2)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--seconds-per-run", type=float, default=20.0)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--candidates-per-worker", type=int, default=8)
    p.add_argument("--phase-seconds", type=float, default=5.0)
    p.add_argument(
        "--compactness-placement",
        choices=("before-weighted", "after-weighted"),
        default="before-weighted",
    )
    p.add_argument("--geometry-node-budget", type=int, default=5_000_000)
    p.add_argument("--seed", type=int, default=260818)
    p.add_argument(
        "--benchmark-script",
        type=Path,
        default=Path("scripts/heg_compactness_benchmark.py"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/sweeps/compactness_certified"),
    )
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.threshold_margin < 0:
        p.error("--threshold-margin must be >= 0")
    if args.repeats < 1:
        p.error("--repeats must be >= 1")
    if args.seconds_per_run <= 0 or args.phase_seconds <= 0:
        p.error("time limits must be > 0")
    if args.workers < 1 or args.candidates_per_worker < 1:
        p.error("worker counts must be >= 1")
    if args.geometry_node_budget < 1:
        p.error("--geometry-node-budget must be >= 1")
    return args


def load_summary(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    repo = Path.cwd()
    benchmark = args.benchmark_script
    if not benchmark.is_absolute():
        benchmark = repo / benchmark
    if not benchmark.is_file():
        raise SystemExit(f"missing benchmark script: {benchmark}")

    targets: dict[int, int] = {}
    if args.target:
        for order, target in args.target:
            if order in targets:
                raise SystemExit(f"duplicate target for n={order}")
            targets[order] = target
    else:
        targets = dict(DEFAULT_TARGETS)

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    combined: list[dict[str, Any]] = []
    rc = 0

    for order, target in sorted(targets.items()):
        threshold = target + args.threshold_margin
        order_out = output / f"order-{order}"

        cmd = [
            sys.executable,
            str(benchmark),
            "--orders", str(order),
            "--metrics", args.metrics,
            "--target", f"{order}={target}",
            "--repeats", str(args.repeats),
            "--seconds-per-run", str(args.seconds_per_run),
            "--workers", str(args.workers),
            "--candidates-per-worker", str(args.candidates_per_worker),
            "--phase-seconds", str(args.phase_seconds),
            "--compactness-threshold", str(threshold),
            "--compactness-placement", args.compactness_placement,
            "--geometry-node-budget", str(args.geometry_node_budget),
            "--seed", str(args.seed),
            "--output-dir", str(order_out),
        ]
        if args.force:
            cmd.append("--force")
        if args.dry_run:
            cmd.append("--dry-run")

        print(
            f"\n=== n={order} target={target} compactness_threshold={threshold} ===",
            flush=True,
        )
        print(" ".join(cmd), flush=True)

        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            rc = proc.returncode
            print(f"ERROR: n={order} benchmark rc={proc.returncode}", flush=True)
            continue

        if args.dry_run:
            continue

        summary = load_summary(order_out / "summary.json")
        if summary is None:
            rc = 2
            print(f"ERROR: missing {order_out / 'summary.json'}", flush=True)
            continue

        combined.append({
            "order": order,
            "target": target,
            "compactness_threshold": threshold,
            "aggregate": summary.get("aggregate", []),
            "summary_path": str(order_out / "summary.json"),
        })

    if args.dry_run:
        return rc

    payload = {
        "schema_version": "graphoratory.heg_compactness_quick_sweep.v1",
        "targets": {str(k): v for k, v in sorted(targets.items())},
        "threshold_margin": args.threshold_margin,
        "metrics": args.metrics.split(","),
        "repeats": args.repeats,
        "seconds_per_run": args.seconds_per_run,
        "workers": args.workers,
        "phase_seconds": args.phase_seconds,
        "compactness_placement": args.compactness_placement,
        "orders": combined,
    }
    combined_path = output / "summary.json"
    combined_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print("\n=== COMBINED ===")
    for order_row in combined:
        print(
            f"n={order_row['order']} target={order_row['target']} "
            f"threshold={order_row['compactness_threshold']}"
        )
        for row in order_row["aggregate"]:
            print(
                f"  {row['metric']:<14} "
                f"success={row['successes']}/{row['runs']} "
                f"median_t={row['median_time_to_target_seconds']} "
                f"median_eval={row['median_evaluated_to_target']} "
                f"median_best={row['median_best_total']}"
            )
    print(f"combined summary: {combined_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
