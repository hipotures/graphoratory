#!/usr/bin/env python3
"""
Exact small-order computation of

    F(n) = min_G sum_{L in {4,8,16,...}, L<=n} C_L(G)

over non-isomorphic simple connected n-vertex graphs with minimum degree >= 3.

Method
------
* nauty/gtools `geng` exhaustively generates one representative of every
  isomorphism class satisfying:
      - simple graph
      - connected
      - minimum degree >= 3
* generation is split into `res/mod` shards and processed in parallel;
* cycle counting is exact;
* branch-and-bound is safe: once a real incumbent U has been found, a graph is
  abandoned as soon as its already-counted forbidden cycles reach U;
* completed shards are checkpointed, so interrupted runs can be resumed.

If every shard completes successfully, the reported F(n) is exact.

This is intended for SMALL n.  n=10 is realistic; the search space grows very
quickly.  n=20 is supported syntactically but exhaustive completion can be
prohibitively expensive.

The script uses only the Python standard library plus an external `geng`
binary from nauty/gtools.

Example:
    python scripts/heg_exact_fn_nauty.py \
        --orders 10-12 \
        --workers 8 \
        --shards 256 \
        --output-dir results/exact/fn_10_20
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

INF = (1 << 62) - 1
MAX_GRAPH6_ONEBYTE_N = 62

# Worker globals, initialized once per process.
_GENG: str | None = None
_ORDER: int | None = None
_LENGTHS: tuple[int, ...] = ()
_SHARDS: int | None = None
_SHARED_BEST = None
_SHARED_LOCK = None


@dataclass(slots=True)
class BestGraph:
    total: int
    components: dict[str, int]
    graph6: str
    edges: list[list[int]]


@dataclass(slots=True)
class ShardResult:
    shard: int
    generated: int
    fully_scored: int
    pruned_after_c4: int
    pruned_after_c8: int
    pruned_after_c16: int
    elapsed_seconds: float
    best: BestGraph | None


def parse_orders(raw: str) -> tuple[int, ...]:
    out: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            lo, hi = int(a), int(b)
            if hi < lo:
                raise argparse.ArgumentTypeError(f"descending range: {token}")
            out.update(range(lo, hi + 1))
        else:
            out.add(int(token))
    if not out:
        raise argparse.ArgumentTypeError("no orders supplied")
    values = tuple(sorted(out))
    if values[0] < 4 or values[-1] > MAX_GRAPH6_ONEBYTE_N:
        raise argparse.ArgumentTypeError(
            f"orders must be in [4,{MAX_GRAPH6_ONEBYTE_N}]"
        )
    return values


def forbidden_lengths(n: int) -> tuple[int, ...]:
    vals: list[int] = []
    length = 4
    while length <= n:
        vals.append(length)
        length *= 2
    return tuple(vals)


def resolve_geng(explicit: str | None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(["geng", "nauty-geng"])
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit(
        "Cannot find nauty `geng`.\n"
        "Install nauty/gtools or pass --geng /path/to/geng.\n"
        "Check with: command -v geng || command -v nauty-geng"
    )


def decode_graph6(line: bytes) -> tuple[int, tuple[int, ...], list[list[int]]]:
    """Decode graph6 for n<=62 into adjacency bitmasks and an edge list."""
    line = line.strip()
    if not line:
        raise ValueError("empty graph6 line")
    if line.startswith(b">>graph6<<"):
        line = line[len(b">>graph6<<"):]
    if not line:
        raise ValueError("missing graph6 payload")

    first = line[0] - 63
    if first < 0 or first > 62:
        raise ValueError("this script expects one-byte graph6 order (n<=62)")
    n = first

    values = [byte - 63 for byte in line[1:]]
    if any(v < 0 or v > 63 for v in values):
        raise ValueError("invalid graph6 character")

    needed = n * (n - 1) // 2
    bits: list[int] = []
    for value in values:
        bits.extend(
            [
                (value >> 5) & 1,
                (value >> 4) & 1,
                (value >> 3) & 1,
                (value >> 2) & 1,
                (value >> 1) & 1,
                value & 1,
            ]
        )
        if len(bits) >= needed:
            break
    if len(bits) < needed:
        raise ValueError("truncated graph6 payload")

    adj = [0] * n
    edges: list[list[int]] = []
    k = 0

    # graph6 stores the upper triangle in column-major order:
    # (0,1), (0,2),(1,2), (0,3),(1,3),(2,3), ...
    for j in range(1, n):
        for i in range(j):
            if bits[k]:
                adj[i] |= 1 << j
                adj[j] |= 1 << i
                edges.append([i, j])
            k += 1

    return n, tuple(adj), edges


def count_c4(adj: tuple[int, ...]) -> int:
    """
    Exact number of simple 4-cycles.

    For every unordered pair {u,v}, choose two common neighbours.
    Each C4 is seen twice, once for each pair of opposite vertices.
    """
    n = len(adj)
    doubled = 0
    for u in range(n):
        au = adj[u]
        for v in range(u + 1, n):
            common = (au & adj[v]).bit_count()
            if common >= 2:
                doubled += common * (common - 1) // 2
    if doubled & 1:
        raise RuntimeError("internal C4 counting parity error")
    return doubled // 2


def count_cycles_bounded(
    adj: tuple[int, ...],
    length: int,
    stop_at: int | None,
) -> tuple[int, bool]:
    """
    Count simple undirected cycles of exactly `length`.

    Returns (count, complete).  If stop_at is not None, enumeration may stop
    once count >= stop_at; then complete=False.  This is safe for lower-bound
    pruning because all cycle counts are non-negative.

    Canonicalization:
      * the start vertex is the smallest vertex on the cycle;
      * among the two orientations, keep only first_neighbour < last_neighbour.
    """
    n = len(adj)
    if length > n:
        return 0, True
    if length < 3:
        return 0, True
    if stop_at is not None and stop_at <= 0:
        return 0, False

    full_mask = (1 << n) - 1
    count = 0

    for start in range(n):
        # All other cycle vertices must be > start, making start canonical.
        allowed = full_mask ^ ((1 << (start + 1)) - 1)
        first_mask = adj[start] & allowed

        while first_mask:
            first_bit = first_mask & -first_mask
            first_mask ^= first_bit
            first = first_bit.bit_length() - 1
            visited0 = (1 << start) | first_bit

            def dfs(cur: int, depth: int, visited: int) -> bool:
                nonlocal count

                if depth == length:
                    if (adj[cur] >> start) & 1 and first < cur:
                        count += 1
                        if stop_at is not None and count >= stop_at:
                            return True
                    return False

                candidates = adj[cur] & allowed & ~visited

                # The next vertex is the last one: it must close to start.
                if depth == length - 1:
                    candidates &= adj[start]

                while candidates:
                    bit = candidates & -candidates
                    candidates ^= bit
                    nxt = bit.bit_length() - 1
                    if dfs(nxt, depth + 1, visited | bit):
                        return True
                return False

            if dfs(first, 2, visited0):
                return count, False

    return count, True


def score_with_incumbent(
    adj: tuple[int, ...],
    lengths: tuple[int, ...],
    incumbent: int,
) -> tuple[int | None, dict[str, int], str | None]:
    """
    Exact score if it can beat `incumbent`, otherwise a certified lower-bound
    prune.  Returns (total_or_None, observed_components, prune_stage).
    """
    components: dict[str, int] = {}

    c4 = count_c4(adj)
    components["4"] = c4
    if incumbent < INF and c4 >= incumbent:
        return None, components, "c4"

    running = c4
    for length in lengths:
        if length == 4:
            continue

        room = None if incumbent >= INF else incumbent - running
        count, complete = count_cycles_bounded(adj, length, room)
        components[str(length)] = count
        running += count

        if not complete:
            return None, components, f"c{length}"
        if incumbent < INF and running >= incumbent:
            return None, components, f"c{length}"

    return running, components, None


def _init_worker(
    geng: str,
    order: int,
    lengths: tuple[int, ...],
    shards: int,
    shared_best,
    shared_lock,
) -> None:
    global _GENG, _ORDER, _LENGTHS, _SHARDS, _SHARED_BEST, _SHARED_LOCK
    _GENG = geng
    _ORDER = order
    _LENGTHS = lengths
    _SHARDS = shards
    _SHARED_BEST = shared_best
    _SHARED_LOCK = shared_lock


def _run_shard(shard: int) -> ShardResult:
    if (
        _GENG is None
        or _ORDER is None
        or _SHARDS is None
        or _SHARED_BEST is None
        or _SHARED_LOCK is None
    ):
        raise RuntimeError("worker not initialized")

    command = [
        _GENG,
        "-c",          # connected
        "-q",          # suppress auxiliary chatter
        "-d3",         # minimum degree >= 3
        str(_ORDER),
        f"{shard}/{_SHARDS}",
    ]

    started = time.perf_counter()
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1024 * 1024,
    )
    assert proc.stdout is not None

    generated = 0
    fully_scored = 0
    pruned = {"c4": 0, "c8": 0, "c16": 0}
    local_best: BestGraph | None = None

    try:
        for raw in proc.stdout:
            raw = raw.strip()
            if not raw or raw.startswith(b">>"):
                continue

            generated += 1
            n, adj, edges = decode_graph6(raw)
            if n != _ORDER:
                raise RuntimeError(f"geng emitted n={n}, expected {_ORDER}")

            incumbent = int(_SHARED_BEST.value)
            total, components, stage = score_with_incumbent(
                adj, _LENGTHS, incumbent
            )

            if stage is not None:
                pruned[stage] = pruned.get(stage, 0) + 1
                continue

            assert total is not None
            fully_scored += 1

            if total < int(_SHARED_BEST.value):
                with _SHARED_LOCK:
                    if total < int(_SHARED_BEST.value):
                        _SHARED_BEST.value = total

            if local_best is None or total < local_best.total:
                local_best = BestGraph(
                    total=total,
                    components=components,
                    graph6=raw.decode("ascii"),
                    edges=edges,
                )

        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(
                f"geng shard {shard}/{_SHARDS} exited {rc}: {stderr.strip()}"
            )
    except BaseException:
        proc.kill()
        proc.wait()
        raise

    return ShardResult(
        shard=shard,
        generated=generated,
        fully_scored=fully_scored,
        pruned_after_c4=pruned.get("c4", 0),
        pruned_after_c8=pruned.get("c8", 0),
        pruned_after_c16=pruned.get("c16", 0),
        elapsed_seconds=time.perf_counter() - started,
        best=local_best,
    )


def load_completed(path: Path) -> tuple[set[int], dict[str, int]]:
    completed: set[int] = set()
    totals = {
        "generated": 0,
        "fully_scored": 0,
        "pruned_after_c4": 0,
        "pruned_after_c8": 0,
        "pruned_after_c16": 0,
    }
    if not path.exists():
        return completed, totals

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        completed.add(int(item["shard"]))
        for key in totals:
            totals[key] += int(item.get(key, 0))
    return completed, totals


def load_best(path: Path) -> BestGraph | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return BestGraph(
        total=int(payload["total"]),
        components={str(k): int(v) for k, v in payload["components"].items()},
        graph6=str(payload["graph6"]),
        edges=[[int(u), int(v)] for u, v in payload["edges"]],
    )


def save_best(path: Path, order: int, best: BestGraph) -> None:
    payload = {
        "schema_version": "heg.exact_fn.best.v1",
        "order": order,
        "total": best.total,
        "components": best.components,
        "graph6": best.graph6,
        "edges": best.edges,
        "constraints": {
            "simple": True,
            "connected": True,
            "minimum_degree": 3,
        },
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_completed(path: Path, result: ShardResult) -> None:
    item = asdict(result)
    # Best is already persisted separately; omit it from the shard ledger.
    item.pop("best", None)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def exact_order(
    *,
    order: int,
    workers: int,
    shards: int,
    geng: str,
    output_root: Path,
) -> None:
    lengths = forbidden_lengths(order)
    order_dir = output_root / f"order_{order}"
    order_dir.mkdir(parents=True, exist_ok=True)

    ledger = order_dir / "completed_shards.jsonl"
    best_path = order_dir / "best.json"
    summary_path = order_dir / "summary.json"

    completed, totals = load_completed(ledger)
    best = load_best(best_path)
    initial_best = best.total if best is not None else INF

    remaining = [s for s in range(shards) if s not in completed]

    print()
    print(f"=== exact F({order}) ===", flush=True)
    print(
        f"forbidden={lengths} workers={workers} shards={shards} "
        f"completed={len(completed)} remaining={len(remaining)} "
        f"incumbent={'none' if best is None else best.total}",
        flush=True,
    )

    if not remaining:
        if best is None:
            raise RuntimeError("all shards marked complete but best.json is missing")
        print(
            f"ALREADY COMPLETE: F({order})={best.total} components={best.components}",
            flush=True,
        )
        return

    ctx = mp.get_context("fork")
    shared_best = ctx.Value("q", initial_best)
    shared_lock = ctx.Lock()

    started = time.perf_counter()
    done_now = 0

    with ctx.Pool(
        processes=workers,
        initializer=_init_worker,
        initargs=(geng, order, lengths, shards, shared_best, shared_lock),
    ) as pool:
        # imap_unordered avoids scheduling all result handling serially while
        # retaining a bounded worker pool.
        for result in pool.imap_unordered(_run_shard, remaining, chunksize=1):
            done_now += 1
            append_completed(ledger, result)
            completed.add(result.shard)

            for key in totals:
                totals[key] += int(getattr(result, key))

            if result.best is not None and (
                best is None or result.best.total < best.total
            ):
                best = result.best
                save_best(best_path, order, best)
                print(
                    f"NEW EXACT UPPER F({order}) <= {best.total} "
                    f"components={best.components} "
                    f"after {totals['generated']:,} generated",
                    flush=True,
                )

            if done_now == 1 or done_now % max(1, shards // 32) == 0:
                elapsed = time.perf_counter() - started
                rate = (
                    totals["generated"] / elapsed
                    if elapsed > 0
                    else 0.0
                )
                print(
                    f"progress {len(completed)}/{shards} shards "
                    f"graphs={totals['generated']:,} "
                    f"best={int(shared_best.value) if int(shared_best.value) < INF else 'none'} "
                    f"rate={rate:,.0f} graphs/s",
                    flush=True,
                )

    if best is None:
        raise RuntimeError("geng produced no eligible graphs")

    all_complete = len(completed) == shards
    summary = {
        "schema_version": "heg.exact_fn.summary.v1",
        "order": order,
        "forbidden_lengths": list(lengths),
        "workers": workers,
        "shards": shards,
        "completed_shards": len(completed),
        "all_shards_complete": all_complete,
        "exact": all_complete,
        "F_n": best.total if all_complete else None,
        "best_upper_bound": best.total,
        "best_components": best.components,
        **totals,
        "elapsed_this_invocation_seconds": time.perf_counter() - started,
        "geng": geng,
        "method": "nauty geng -c -d3 exhaustive non-isomorphic generation",
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if all_complete:
        print(
            f"CERTIFIED EXACT: F({order})={best.total} "
            f"components={best.components} "
            f"graphs={totals['generated']:,}",
            flush=True,
        )
    else:
        print(
            f"PARTIAL: F({order}) <= {best.total}; "
            f"{len(completed)}/{shards} shards complete",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Exact small-order HEG F(n) by exhaustive nauty geng enumeration."
    )
    p.add_argument("--orders", type=parse_orders, default=(10,))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument(
        "--shards",
        type=int,
        default=256,
        help="geng res/mod partition count; more shards improve resume granularity",
    )
    p.add_argument(
        "--geng",
        default=None,
        help="path/name of geng; auto-detects geng or nauty-geng by default",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/exact/fn_small"),
    )
    args = p.parse_args()

    if args.workers < 1:
        p.error("--workers must be >= 1")
    if args.shards < args.workers:
        p.error("--shards must be >= --workers")
    if args.shards < 1:
        p.error("--shards must be >= 1")
    return args


def main() -> int:
    args = parse_args()
    geng = resolve_geng(args.geng)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"geng={geng}", flush=True)
    print(
        "Exactness criterion: every res/mod shard must complete.",
        flush=True,
    )

    for order in args.orders:
        exact_order(
            order=order,
            workers=args.workers,
            shards=args.shards,
            geng=geng,
            output_root=args.output_dir,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
