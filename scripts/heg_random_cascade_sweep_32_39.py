#!/usr/bin/env python3
"""Quick HEG cascade sweep for orders 32..39.

Uses the bounded-memory v2 cascade mutator.  Start selection:
  1. explicit --start ORDER=PATH
  2. best graph from random_cascade_long
  3. best graph from random_cascade_budget_32_33
  4. best graph from random_alternating_sweep_23_33
  5. otherwise create a reproducible random minimum-edge legal seed

Fresh seeds are score-blind:
  even n -> cubic
  odd n  -> one degree-4 vertex, all others degree 3
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
from pathlib import Path
from typing import Iterable

MAX_ORDER = 128


def parse_orders(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            lo, hi = int(a), int(b)
            if hi < lo:
                raise argparse.ArgumentTypeError(f'descending range: {part}')
            values.extend(range(lo, hi + 1))
        else:
            values.append(int(part))
    values = sorted(set(values))
    if not values or values[0] < 4 or values[-1] > MAX_ORDER:
        raise argparse.ArgumentTypeError(f'orders must be in [4,{MAX_ORDER}]')
    return tuple(values)


def parse_start(raw: str) -> tuple[int, Path]:
    if '=' not in raw:
        raise argparse.ArgumentTypeError('expected ORDER=PATH')
    left, right = raw.split('=', 1)
    return int(left), Path(right)


def norm_edge(u: int, v: int) -> tuple[int, int]:
    if u == v:
        raise ValueError('self-loop')
    return (u, v) if u < v else (v, u)


def graph_is_legal(order: int, edges: Iterable[tuple[int, int]]) -> bool:
    edge_set = set(edges)
    degree = [0] * order
    adj = [[] for _ in range(order)]
    for u, v in edge_set:
        if u == v or u < 0 or v >= order:
            return False
        degree[u] += 1
        degree[v] += 1
        adj[u].append(v)
        adj[v].append(u)
    if min(degree, default=0) < 3:
        return False
    seen = {0}
    stack = [0]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return len(seen) == order


def random_minimal_legal_seed(
    order: int,
    seed: int,
    max_attempts: int = 100_000,
) -> tuple[tuple[int, int], ...]:
    rng = random.Random(seed)
    expected_edges = math.ceil(3 * order / 2)
    for _ in range(max_attempts):
        degrees = [3] * order
        if order % 2:
            degrees[rng.randrange(order)] = 4
        stubs: list[int] = []
        for vertex, degree in enumerate(degrees):
            stubs.extend([vertex] * degree)
        rng.shuffle(stubs)

        edges: set[tuple[int, int]] = set()
        valid = True
        for i in range(0, len(stubs), 2):
            u, v = stubs[i], stubs[i + 1]
            if u == v:
                valid = False
                break
            edge = norm_edge(u, v)
            if edge in edges:
                valid = False
                break
            edges.add(edge)
        if valid and len(edges) == expected_edges and graph_is_legal(order, edges):
            return tuple(sorted(edges))
    raise RuntimeError(f'could not generate legal seed for n={order}')


def default_start_candidates(order: int) -> tuple[Path, ...]:
    return (
        Path(f'results/sweeps/random_cascade_long/order_{order}/best.json'),
        Path(f'results/sweeps/random_cascade_budget_32_33/order_{order}/best.json'),
        Path(f'results/sweeps/random_alternating_sweep_23_33/order_{order}/best.json.gz'),
    )


def choose_start(
    order: int,
    explicit: dict[int, Path],
    out_dir: Path,
    master_seed: int,
) -> tuple[Path, str]:
    if order in explicit:
        path = explicit[order]
        if not path.exists():
            raise FileNotFoundError(path)
        return path, 'explicit'

    for path in default_start_candidates(order):
        if path.exists():
            return path, 'existing-best'

    seed_value = master_seed + order * 1_000_003
    edges = random_minimal_legal_seed(order, seed_value)
    path = out_dir / f'order_{order}' / 'fresh_seed.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema_version': 'heg.random_minimum_edge_seed.v1',
        'order': order,
        'edges': [list(edge) for edge in edges],
        'seed': seed_value,
        'source': 'score-blind configuration-model rejection sampler',
        'edge_count': len(edges),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return path, 'fresh-minimum-edge'


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument('--orders', type=parse_orders, default=tuple(range(32, 40)))
    p.add_argument('--workers', type=int, default=16)
    p.add_argument('--candidates-per-worker', type=int, default=32)
    p.add_argument('--evaluation-budget', type=int, default=500_000)
    p.add_argument('--phase-evaluations', type=int, default=50_000)
    p.add_argument('--walk-min', type=int, default=4)
    p.add_argument('--walk-max', type=int, default=48)
    p.add_argument('--root-parent-prob', type=float, default=0.05)
    p.add_argument('--reservoir-size', type=int, default=4096)
    p.add_argument('--elite-size', type=int, default=128)
    p.add_argument('--node-budget', type=int, default=10_000_000)
    p.add_argument('--witness-cap', type=int, default=1_000_000)
    p.add_argument('--success-total', type=int, default=0)
    p.add_argument('--log-total', type=int, default=32)
    p.add_argument('--seed', type=int, default=8172039)
    p.add_argument('--max-seconds', type=float, default=0)
    p.add_argument('--start', action='append', type=parse_start, default=[])
    p.add_argument(
        '--child-script',
        type=Path,
        default=Path('scripts/heg_random_cascade_budget_v2.py'),
    )
    p.add_argument(
        '--output-dir',
        type=Path,
        default=Path('results/sweeps/random_cascade_quick_32_39'),
    )
    args = p.parse_args()
    if args.workers < 1 or args.candidates_per_worker < 1:
        p.error('worker counts must be >= 1')
    if args.evaluation_budget < 1 or args.phase_evaluations < 1:
        p.error('evaluation budgets must be >= 1')
    explicit_orders = [n for n, _ in args.start]
    if len(explicit_orders) != len(set(explicit_orders)):
        p.error('duplicate --start for same order')
    return args


def main() -> int:
    args = parse_args()
    explicit = dict(args.start)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    sweep_summary: list[dict[str, object]] = []

    for order in args.orders:
        out = args.output_dir / f'order_{order}'
        out.mkdir(parents=True, exist_ok=True)
        start, source = choose_start(order, explicit, args.output_dir, args.seed)
        run_log = out / 'run.log'

        command = [
            sys.executable,
            str(args.child_script),
            '--start-graph', str(start),
            '--expected-order', str(order),
            '--workers', str(args.workers),
            '--candidates-per-worker', str(args.candidates_per_worker),
            '--walk-min', str(args.walk_min),
            '--walk-max', str(args.walk_max),
            '--root-parent-prob', str(args.root_parent_prob),
            '--reservoir-size', str(args.reservoir_size),
            '--elite-size', str(args.elite_size),
            '--evaluation-budget', str(args.evaluation_budget),
            '--phase-evaluations', str(args.phase_evaluations),
            '--node-budget', str(args.node_budget),
            '--witness-cap', str(args.witness_cap),
            '--success-total', str(args.success_total),
            '--log-total', str(args.log_total),
            '--seed', str(args.seed + order * 2_000_003),
            '--max-seconds', str(args.max_seconds),
            '--save-best', str(out / 'best.json'),
            '--save-hits', str(out / 'hits.jsonl'),
            '--save-pool', str(out / 'pool.json'),
            '--save-summary', str(out / 'summary.json'),
        ]

        print(f'\n=== n={order} source={source} start={start} ===', flush=True)
        with run_log.open('w', encoding='utf-8') as log:
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
                if (
                    line.startswith('START ')
                    or line.startswith('NEW BEST')
                    or line.startswith('PHASE ')
                    or line.startswith('STATUS ')
                    or line.startswith('DONE ')
                    or line.startswith('CALLS ')
                    or line.startswith('success:')
                    or line.startswith('evaluation budget')
                    or line.startswith('emergency ')
                ):
                    print(line, end='', flush=True)
            rc = proc.wait()

        record: dict[str, object] = {
            'order': order,
            'start': str(start),
            'start_source': source,
            'exit_code': rc,
            'run_log': str(run_log),
        }
        best_path = out / 'best.json'
        if best_path.exists():
            try:
                payload = json.loads(best_path.read_text(encoding='utf-8'))
                score = payload.get('score', {})
                if isinstance(score, dict):
                    record['best_total'] = score.get('total')
                    record['best_weighted'] = score.get('weighted')
                    components = score.get('components')
                    if isinstance(components, dict):
                        record['best_components'] = {
                            k: (v.get('observed') if isinstance(v, dict) else v)
                            for k, v in components.items()
                        }
                record['edge_count'] = len(payload.get('edges', []))
            except Exception as exc:
                record['best_read_error'] = str(exc)
        sweep_summary.append(record)

        (args.output_dir / 'sweep_summary.json').write_text(
            json.dumps(sweep_summary, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )

        if rc != 0:
            print(f'n={order} failed with exit code {rc}; see {run_log}', file=sys.stderr)
            return rc
        if record.get('best_total') == 0:
            print(f'COUNTEREXAMPLE candidate found at n={order}; stopping sweep.', flush=True)
            break

    print('\n=== sweep summary ===')
    for row in sweep_summary:
        print(
            f"n={row['order']} T={row.get('best_total')} "
            f"components={row.get('best_components')} m={row.get('edge_count')}"
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
