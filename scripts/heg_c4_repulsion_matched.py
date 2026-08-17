#!/usr/bin/env python3
"""
Matched C4-geometry comparison for n=32.

Compares already deduplicated non-isomorphic representatives:

  TARGET clean:
      C4=4, C8=C16=C32=0

  CONTROL RANDOM dirty:
      C4=4 and at least one of C8,C16,C32 is certified present

Primary matched analyses:
  * exact edge count m=48
  * exact edge count m=49
  * exact degree sequence within each m

No new search is performed.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, deque, defaultdict
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--control", type=Path, required=True)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/analysis/c4_repulsion_matched_n32"),
    )
    return p.parse_args()


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj


def normalize(record):
    edges = set()
    for raw in record["edges"]:
        u, v = map(int, raw)
        if u > v:
            u, v = v, u
        edges.add((u, v))

    order = record.get("order")
    if not isinstance(order, int):
        order = 1 + max(max(e) for e in edges)

    deg = [0] * order
    adj = [set() for _ in range(order)]
    for u, v in edges:
        deg[u] += 1
        deg[v] += 1
        adj[u].add(v)
        adj[v].add(u)

    return {
        "graph_hash": record.get("graph_hash", ""),
        "order": order,
        "edges": tuple(sorted(edges)),
        "edge_count": len(edges),
        "degree_sequence": tuple(sorted(deg)),
        "adj": adj,
    }


def canonical_cycle4(seq):
    seq = tuple(seq)
    rev = tuple(reversed(seq))
    variants = []
    for base in (seq, rev):
        for k in range(4):
            variants.append(base[k:] + base[:k])
    return min(variants)


def enumerate_c4(adj):
    seen = set()
    n = len(adj)
    for a in range(n):
        for b in adj[a]:
            for c in adj[b]:
                if c in (a, b):
                    continue
                for d in adj[c]:
                    if d in (a, b, c):
                        continue
                    if a in adj[d]:
                        seen.add(canonical_cycle4((a, b, c, d)))
    return sorted(seen)


def set_distance(adj, left, right):
    left = set(left)
    right = set(right)
    if left & right:
        return 0
    q = deque(left)
    dist = [-1] * len(adj)
    for u in left:
        dist[u] = 0
    while q:
        u = q.popleft()
        nd = dist[u] + 1
        for v in adj[u]:
            if dist[v] != -1:
                continue
            if v in right:
                return nd
            dist[v] = nd
            q.append(v)
    raise RuntimeError("disconnected graph")


def geometry(graph):
    cycles = enumerate_c4(graph["adj"])
    if len(cycles) != 4:
        raise RuntimeError(
            f"{graph['graph_hash']}: expected exactly 4 C4, found {len(cycles)}"
        )
    csets = [set(c) for c in cycles]
    distances = []
    overlaps = 0
    for i in range(4):
        for j in range(i + 1, 4):
            if csets[i] & csets[j]:
                overlaps += 1
            distances.append(set_distance(graph["adj"], csets[i], csets[j]))
    return {
        "d_min": min(distances),
        "d_mean": statistics.fmean(distances),
        "d_max": max(distances),
        "union_vertices": len(set().union(*csets)),
        "all_vertex_disjoint": overlaps == 0,
    }


def summarize(rows):
    if not rows:
        return {
            "n": 0,
            "mean_d_mean": None,
            "median_d_min": None,
            "median_d_mean": None,
            "median_d_max": None,
            "vertex_disjoint_fraction": None,
            "union16_fraction": None,
        }
    return {
        "n": len(rows),
        "mean_d_mean": statistics.fmean(r["d_mean"] for r in rows),
        "median_d_min": statistics.median(r["d_min"] for r in rows),
        "median_d_mean": statistics.median(r["d_mean"] for r in rows),
        "median_d_max": statistics.median(r["d_max"] for r in rows),
        "vertex_disjoint_fraction": statistics.fmean(
            1.0 if r["all_vertex_disjoint"] else 0.0 for r in rows
        ),
        "union16_fraction": statistics.fmean(
            1.0 if r["union_vertices"] == 16 else 0.0 for r in rows
        ),
    }


def load(path, label):
    rows = []
    for rec in iter_jsonl(path):
        g = normalize(rec)
        geom = geometry(g)
        rows.append({
            "group": label,
            "graph_hash": g["graph_hash"],
            "edge_count": g["edge_count"],
            "degree_sequence": g["degree_sequence"],
            **geom,
        })
    return rows


def print_summary(title, target_rows, control_rows):
    t = summarize(target_rows)
    c = summarize(control_rows)
    print(f"\n=== {title} ===")
    print(
        f"TARGET  n={t['n']:>4}  mean(d_mean)={t['mean_d_mean']}  "
        f"median d_min={t['median_d_min']}  "
        f"vertex-disjoint={t['vertex_disjoint_fraction']}"
    )
    print(
        f"CONTROL n={c['n']:>4}  mean(d_mean)={c['mean_d_mean']}  "
        f"median d_min={c['median_d_min']}  "
        f"vertex-disjoint={c['vertex_disjoint_fraction']}"
    )
    if t["mean_d_mean"] is not None and c["mean_d_mean"] is not None:
        print(
            "difference TARGET-CONTROL mean(d_mean)="
            f"{t['mean_d_mean'] - c['mean_d_mean']}"
        )
    if t["vertex_disjoint_fraction"] is not None and c["vertex_disjoint_fraction"] is not None:
        print(
            "difference TARGET-CONTROL vertex-disjoint="
            f"{t['vertex_disjoint_fraction'] - c['vertex_disjoint_fraction']}"
        )


def main():
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    target = load(args.target, "target_clean")
    control = load(args.control, "control_random_dirty")

    print(f"target total:  {len(target)}")
    print(f"control total: {len(control)}")

    all_rows = target + control

    # Overall edge-count-matched strata.
    for m in (48, 49):
        t = [r for r in target if r["edge_count"] == m]
        c = [r for r in control if r["edge_count"] == m]
        print_summary(f"EXACT EDGE COUNT m={m}", t, c)

    # Exact degree sequence strata that exist in BOTH groups.
    by_t = defaultdict(list)
    by_c = defaultdict(list)
    for r in target:
        by_t[(r["edge_count"], r["degree_sequence"])].append(r)
    for r in control:
        by_c[(r["edge_count"], r["degree_sequence"])].append(r)

    common = sorted(set(by_t) & set(by_c), key=lambda x: (x[0], x[1]))

    print("\n=== COMMON EXACT DEGREE-SEQUENCE STRATA ===")
    matched_summary = []
    for m, ds in common:
        if m not in (48, 49):
            continue
        trows = by_t[(m, ds)]
        crows = by_c[(m, ds)]
        ts = summarize(trows)
        cs = summarize(crows)
        ds_compact = Counter(ds)
        ds_text = ",".join(f"{d}^{count}" for d, count in sorted(ds_compact.items()))
        print(
            f"m={m} deg={ds_text:18s} "
            f"TARGET={len(trows):3d} CONTROL={len(crows):3d} "
            f"mean_d={ts['mean_d_mean']:.3f}/{cs['mean_d_mean']:.3f} "
            f"med_dmin={ts['median_d_min']}/{cs['median_d_min']} "
            f"disjoint={ts['vertex_disjoint_fraction']:.3f}/{cs['vertex_disjoint_fraction']:.3f}"
        )
        matched_summary.append({
            "edge_count": m,
            "degree_sequence": list(ds),
            "degree_sequence_compact": ds_text,
            "target": ts,
            "control": cs,
        })

    # CSV for inspection.
    csv_path = args.output_dir / "per_graph_matched.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "group", "graph_hash", "edge_count", "degree_sequence",
            "d_min", "d_mean", "d_max", "union_vertices", "all_vertex_disjoint",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in all_rows:
            if r["edge_count"] not in (48, 49):
                continue
            w.writerow({
                **{k: r[k] for k in fields if k != "degree_sequence"},
                "degree_sequence": ",".join(map(str, r["degree_sequence"])),
            })

    summary = {
        "target_total": len(target),
        "control_total": len(control),
        "m48": {
            "target": summarize([r for r in target if r["edge_count"] == 48]),
            "control": summarize([r for r in control if r["edge_count"] == 48]),
        },
        "m49": {
            "target": summarize([r for r in target if r["edge_count"] == 49]),
            "control": summarize([r for r in control if r["edge_count"] == 49]),
        },
        "common_degree_sequence_strata": matched_summary,
        "interpretation_note": (
            "Descriptive matched comparison on search-derived non-isomorphic graphs; "
            "not an IID sample and not a proof of causality."
        ),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"\nsummary: {args.output_dir / 'summary.json'}")
    print(f"per graph: {csv_path}")


if __name__ == "__main__":
    main()
