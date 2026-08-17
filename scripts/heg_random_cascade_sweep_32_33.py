#!/usr/bin/env python3
"""
Convenience launcher for the current optimized HEG n=32,33 cascade runs.

Runs orders sequentially using the previous best exact graphs as starts:

  n=32 -> 300,000 evaluated candidates, 15,000 per RANDOM/ELITE phase
  n=33 -> 360,000 evaluated candidates, 16,500 per RANDOM/ELITE phase

Existing starts may be .json or .json.gz.

The child script contains the actual mutation/scoring logic.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULTS = {
    32: {
        "budget": 300_000,
        "phase": 15_000,
        "start": Path(
            "results/sweeps/random_alternating_sweep_23_33/order_32/best.json.gz"
        ),
    },
    33: {
        "budget": 360_000,
        "phase": 16_500,
        "start": Path(
            "results/sweeps/random_alternating_sweep_23_33/order_33/best.json.gz"
        ),
    },
}


def parse_orders(raw: str) -> tuple[int, ...]:
    vals = []
    for token in raw.split(","):
        token = token.strip()
        if token:
            vals.append(int(token))
    result = tuple(dict.fromkeys(vals))
    if not result or any(n not in DEFAULTS for n in result):
        raise argparse.ArgumentTypeError("this launcher accepts only 32,33")
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--orders", type=parse_orders, default=(32, 33))
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--candidates-per-worker", type=int, default=8)
    p.add_argument("--walk-min", type=int, default=4)
    p.add_argument("--walk-max", type=int, default=48)
    p.add_argument("--root-parent-prob", type=float, default=0.05)
    p.add_argument("--reservoir-size", type=int, default=4096)
    p.add_argument("--elite-size", type=int, default=128)
    p.add_argument("--node-budget", type=int, default=10_000_000)
    p.add_argument("--witness-cap", type=int, default=1_000_000)
    p.add_argument("--success-total", type=int, default=0)
    p.add_argument("--log-total", type=int, default=32)
    p.add_argument("--seed", type=int, default=8172026)
    p.add_argument("--max-seconds", type=float, default=0)
    p.add_argument(
        "--child-script",
        type=Path,
        default=Path("scripts/heg_random_cascade_budget.py"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/sweeps/random_cascade_budget_32_33"),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    for order in args.orders:
        cfg = DEFAULTS[order]
        start = cfg["start"]
        if not start.exists():
            raise SystemExit(
                f"Missing start for n={order}: {start}\n"
                "Expected the previously committed best.json.gz."
            )

        out = args.output_dir / f"order_{order}"
        out.mkdir(parents=True, exist_ok=True)
        run_log = out / "run.log"

        command = [
            sys.executable,
            str(args.child_script),
            "--start-graph", str(start),
            "--expected-order", str(order),
            "--workers", str(args.workers),
            "--candidates-per-worker", str(args.candidates_per_worker),
            "--walk-min", str(args.walk_min),
            "--walk-max", str(args.walk_max),
            "--root-parent-prob", str(args.root_parent_prob),
            "--reservoir-size", str(args.reservoir_size),
            "--elite-size", str(args.elite_size),
            "--evaluation-budget", str(cfg["budget"]),
            "--phase-evaluations", str(cfg["phase"]),
            "--node-budget", str(args.node_budget),
            "--witness-cap", str(args.witness_cap),
            "--success-total", str(args.success_total),
            "--log-total", str(args.log_total),
            "--seed", str(args.seed + order * 1_000_003),
            "--max-seconds", str(args.max_seconds),
            "--save-best", str(out / "best.json"),
            "--save-hits", str(out / "hits.jsonl"),
            "--save-pool", str(out / "pool.json"),
            "--save-summary", str(out / "summary.json"),
        ]

        print()
        print(f"=== n={order} cascade run ===", flush=True)
        print(" ".join(command), flush=True)

        with run_log.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                # Keep stdout compact: improvements, phase transitions, status,
                # and final lines only.
                if (
                    line.startswith("NEW BEST")
                    or line.startswith("PHASE ")
                    or line.startswith("STATUS ")
                    or line.startswith("DONE ")
                    or line.startswith("CALLS ")
                    or line.startswith("success:")
                    or line.startswith("evaluation budget")
                    or line.startswith("emergency ")
                ):
                    print(line, end="", flush=True)
            rc = proc.wait()

        if rc != 0:
            raise SystemExit(f"n={order} child failed with exit code {rc}; see {run_log}")

        best = out / "best.json"
        if not best.exists():
            raise SystemExit(f"n={order} finished without {best}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
