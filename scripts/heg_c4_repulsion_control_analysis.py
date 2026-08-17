#!/usr/bin/env python3
"""
Compare C4 geometry between:

  TARGET:
      C4=4 and all higher forbidden power-of-two cycle counts are zero
      (normally the (4,0,0,0) non-isomorphic representatives)

  CONTROL:
      C4=4 and at least one higher forbidden power-of-two cycle is certified present
      (normally produced by heg_random_cascade_budget_v4_c4control.py)

The scientific question is whether eliminating C8/C16/C32 correlates with stronger
spatial separation ("repulsion") of the four residual C4 cycles.

Both inputs are deduplicated up to exact graph isomorphism with NetworkX VF2.
Weisfeiler-Lehman hashing is used only as a safe pre-bucketing heuristic; it never
decides isomorphism.

Primary comparison:
    TARGET non-isomorphic classes
vs
    CONTROL RANDOM-phase non-isomorphic classes

The RANDOM-phase control is primary because its parents come from the score-blind
reservoir/root mechanism rather than the ELITE pool. ELITE controls are reported
separately as a sensitivity check.

Distance:
    d(C_i,C_j) = min_{u in C_i, v in C_j} d_G(u,v)

Outputs:
    summary.json
    per_graph.csv
    pair_distances.csv
    representatives_target.jsonl
    representatives_control_random.jsonl
    representatives_control_elite.jsonl
    representatives_control_all.jsonl
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

try:
    import networkx as nx
except ImportError as exc:
    raise SystemExit(
        "networkx is required for exact VF2 isomorphism deduplication"
    ) from exc


TARGET_PROFILE = (4, 0, 0, 0)


@dataclass(slots=True)
class GraphItem:
    source: str
    phase: str
    graph_hash: str
    order: int
    edges: tuple[tuple[int, int], ...]
    record: dict


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare residual C4 geometry against fixed-C4 dirty controls."
    )
    p.add_argument(
        "--target",
        type=Path,
        required=True,
        help=(
            "JSONL containing target graphs. The script keeps only exact "
            "(4,0,0,0) records."
        ),
    )
    p.add_argument(
        "--control",
        type=Path,
        required=True,
        help=(
            "JSONL from heg_random_cascade_budget_v4_c4control.py: "
            "C4=4 with certified higher forbidden cycle present."
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/analysis/c4_repulsion_control_n32"),
    )
    p.add_argument(
        "--top-signatures",
        type=int,
        default=20,
    )
    args = p.parse_args()
    if args.top_signatures < 1:
        p.error("--top-signatures must be >= 1")
    return args


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_jsonl(path: Path) -> Iterator[dict]:
    with open_text(path) as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_no}: invalid JSON") from exc
            if not isinstance(obj, dict):
                raise RuntimeError(f"{path}:{line_no}: expected JSON object")
            yield obj


def observed_component(value):
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        value = value.get("observed")
        return value if isinstance(value, int) else None
    return None


def exact_profile(record: dict):
    components = record.get("components")
    if not isinstance(components, dict):
        score = record.get("score")
        if isinstance(score, dict):
            components = score.get("components")
    if not isinstance(components, dict):
        return None
    vals = []
    for length in (4, 8, 16, 32):
        value = observed_component(components.get(str(length)))
        if value is None:
            return None
        vals.append(value)
    return tuple(vals)


def normalize_graph(record: dict, source: str, phase: str, fallback: str) -> GraphItem:
    raw_edges = record.get("edges")
    if not isinstance(raw_edges, list):
        raise ValueError("record has no edges")

    raw_order = record.get("order")
    if isinstance(raw_order, int):
        order = raw_order
    else:
        max_v = max((max(map(int, e)) for e in raw_edges), default=-1)
        order = max_v + 1

    edges = set()
    for raw in raw_edges:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("malformed edge")
        u, v = int(raw[0]), int(raw[1])
        if u == v:
            raise ValueError("self-loop")
        if not (0 <= u < order and 0 <= v < order):
            raise ValueError("edge endpoint outside order")
        edge = (u, v) if u < v else (v, u)
        if edge in edges:
            raise ValueError("duplicate edge")
        edges.add(edge)

    graph_hash = record.get("graph_hash")
    if not isinstance(graph_hash, str) or not graph_hash:
        graph_hash = fallback

    return GraphItem(
        source=source,
        phase=phase,
        graph_hash=graph_hash,
        order=order,
        edges=tuple(sorted(edges)),
        record=record,
    )


def load_targets(path: Path) -> list[GraphItem]:
    items = []
    for index, record in enumerate(iter_jsonl(path), 1):
        if exact_profile(record) != TARGET_PROFILE:
            continue
        items.append(normalize_graph(record, "TARGET", "TARGET", f"target-{index}"))
    return items


def load_controls(path: Path) -> list[GraphItem]:
    items = []
    for index, record in enumerate(iter_jsonl(path), 1):
        if record.get("control_kind") != "fixed_c4_higher_forbidden_present":
            continue
        if record.get("target_c4") != 4:
            continue
        phase = record.get("phase")
        if phase not in {"RANDOM", "ELITE"}:
            phase = "UNKNOWN"
        items.append(normalize_graph(record, "CONTROL", phase, f"control-{index}"))
    return items


def nx_graph(item: GraphItem):
    g = nx.Graph()
    g.add_nodes_from(range(item.order))
    g.add_edges_from(item.edges)
    return g


def invariant_key(item: GraphItem):
    g = nx_graph(item)
    degrees = tuple(sorted(dict(g.degree()).values()))
    triangles = tuple(sorted(nx.triangles(g).values()))
    wl = nx.weisfeiler_lehman_graph_hash(g, iterations=4)
    return (item.order, len(item.edges), degrees, triangles, wl)


def exact_dedup(items: Sequence[GraphItem]) -> list[GraphItem]:
    """Keep one representative per exact VF2 isomorphism class."""
    buckets = defaultdict(list)
    reps: list[GraphItem] = []

    for index, item in enumerate(items, 1):
        key = invariant_key(item)
        g = nx_graph(item)
        duplicate = False
        for rep_g, _rep_item in buckets[key]:
            if nx.is_isomorphic(g, rep_g):
                duplicate = True
                break
        if not duplicate:
            buckets[key].append((g, item))
            reps.append(item)

        if index % 500 == 0:
            print(
                f"  dedup {index:,}/{len(items):,} -> {len(reps):,} classes",
                file=sys.stderr,
            )
    return reps


def adjacency(item: GraphItem):
    adj = [set() for _ in range(item.order)]
    for u, v in item.edges:
        adj[u].add(v)
        adj[v].add(u)
    return adj


def canonical_cycle4(seq):
    seq = tuple(seq)
    rev = tuple(reversed(seq))
    variants = []
    for base in (seq, rev):
        for shift in range(4):
            variants.append(base[shift:] + base[:shift])
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
    q = deque()
    dist = [-1] * len(adj)
    for u in left:
        dist[u] = 0
        q.append(u)
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


def cycle_edge_set(cycle):
    edges = set()
    for i in range(4):
        u, v = cycle[i], cycle[(i + 1) % 4]
        edges.add((u, v) if u < v else (v, u))
    return edges


def geometry(item: GraphItem) -> dict:
    adj = adjacency(item)
    cycles = enumerate_c4(adj)
    if len(cycles) != 4:
        raise RuntimeError(
            f"{item.graph_hash}: expected exactly four C4 cycles, found {len(cycles)}"
        )

    pair_rows = []
    distances = []
    vertex_overlap_pairs = 0
    edge_overlap_pairs = 0

    cycle_vertices = [set(c) for c in cycles]
    cycle_edges = [cycle_edge_set(c) for c in cycles]

    for i in range(4):
        for j in range(i + 1, 4):
            shared_v = len(cycle_vertices[i] & cycle_vertices[j])
            shared_e = len(cycle_edges[i] & cycle_edges[j])
            distance = set_distance(adj, cycle_vertices[i], cycle_vertices[j])
            distances.append(distance)
            vertex_overlap_pairs += shared_v > 0
            edge_overlap_pairs += shared_e > 0
            pair_rows.append(
                {
                    "cycle_i": i,
                    "cycle_j": j,
                    "distance": distance,
                    "shared_vertices": shared_v,
                    "shared_edges": shared_e,
                }
            )

    union_vertices = len(set().union(*cycle_vertices))
    return {
        "graph_hash": item.graph_hash,
        "source": item.source,
        "phase": item.phase,
        "order": item.order,
        "edge_count": len(item.edges),
        "cycles": cycles,
        "pairs": pair_rows,
        "distances": distances,
        "distance_signature": tuple(sorted(distances)),
        "d_min": min(distances),
        "d_mean": statistics.fmean(distances),
        "d_max": max(distances),
        "union_vertices": union_vertices,
        "vertex_overlap_pairs": vertex_overlap_pairs,
        "edge_overlap_pairs": edge_overlap_pairs,
        "all_vertex_disjoint": vertex_overlap_pairs == 0,
        "all_edge_disjoint": edge_overlap_pairs == 0,
    }


def summarize(rows, top_signatures):
    pair_dist = []
    dmins = []
    dmeans = []
    dmaxs = []
    union_hist = Counter()
    sig_hist = Counter()
    disjoint = 0
    overlap_pairs = 0

    for row in rows:
        pair_dist.extend(row["distances"])
        dmins.append(row["d_min"])
        dmeans.append(row["d_mean"])
        dmaxs.append(row["d_max"])
        union_hist[row["union_vertices"]] += 1
        sig_hist[row["distance_signature"]] += 1
        disjoint += row["all_vertex_disjoint"]
        overlap_pairs += row["vertex_overlap_pairs"]

    pair_hist = Counter(pair_dist)
    n = len(rows)
    return {
        "graphs": n,
        "cycle_pairs": len(pair_dist),
        "pair_distance_histogram": {str(k): pair_hist[k] for k in sorted(pair_hist)},
        "pair_distance_mean": statistics.fmean(pair_dist) if pair_dist else None,
        "pair_distance_median": statistics.median(pair_dist) if pair_dist else None,
        "per_graph_d_min_mean": statistics.fmean(dmins) if dmins else None,
        "per_graph_d_min_median": statistics.median(dmins) if dmins else None,
        "per_graph_d_mean_mean": statistics.fmean(dmeans) if dmeans else None,
        "per_graph_d_mean_median": statistics.median(dmeans) if dmeans else None,
        "per_graph_d_max_mean": statistics.fmean(dmaxs) if dmaxs else None,
        "per_graph_d_max_median": statistics.median(dmaxs) if dmaxs else None,
        "all_vertex_disjoint_graphs": disjoint,
        "all_vertex_disjoint_fraction": disjoint / n if n else None,
        "vertex_overlap_pairs": overlap_pairs,
        "union_vertex_count_histogram": {
            str(k): union_hist[k] for k in sorted(union_hist)
        },
        "top_distance_signatures": [
            {"signature": list(sig), "count": count}
            for sig, count in sig_hist.most_common(top_signatures)
        ],
    }


def write_representatives(path: Path, items: Sequence[GraphItem]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            payload = dict(item.record)
            payload["_analysis_source"] = item.source
            payload["_analysis_phase"] = item.phase
            fh.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    targets_raw = load_targets(args.target)
    controls_raw = load_controls(args.control)

    controls_random_raw = [x for x in controls_raw if x.phase == "RANDOM"]
    controls_elite_raw = [x for x in controls_raw if x.phase == "ELITE"]

    print(f"target labeled:         {len(targets_raw):,}")
    print(f"control labeled total:  {len(controls_raw):,}")
    print(f"control RANDOM labeled: {len(controls_random_raw):,}")
    print(f"control ELITE labeled:  {len(controls_elite_raw):,}")
    print()

    print("Exact-isomorphism dedup TARGET...", file=sys.stderr)
    targets = exact_dedup(targets_raw)
    print("Exact-isomorphism dedup CONTROL RANDOM...", file=sys.stderr)
    controls_random = exact_dedup(controls_random_raw)
    print("Exact-isomorphism dedup CONTROL ELITE...", file=sys.stderr)
    controls_elite = exact_dedup(controls_elite_raw)
    print("Exact-isomorphism dedup CONTROL ALL...", file=sys.stderr)
    controls_all = exact_dedup(controls_raw)

    groups_items = {
        "target_clean": targets,
        "control_random_dirty": controls_random,
        "control_elite_dirty": controls_elite,
        "control_all_dirty": controls_all,
    }

    groups_rows = {}
    for name, items in groups_items.items():
        groups_rows[name] = [geometry(item) for item in items]

    summary_groups = {
        name: summarize(rows, args.top_signatures)
        for name, rows in groups_rows.items()
    }

    # Descriptive primary contrast only; no IID/p-value claim.
    target_mean = summary_groups["target_clean"]["per_graph_d_mean_mean"]
    random_mean = summary_groups["control_random_dirty"]["per_graph_d_mean_mean"]
    target_dmin = summary_groups["target_clean"]["per_graph_d_min_median"]
    random_dmin = summary_groups["control_random_dirty"]["per_graph_d_min_median"]

    summary = {
        "question": (
            "At fixed C4=4, do graphs with C8=C16=C32=0 show stronger C4 "
            "spatial separation than graphs with a certified higher forbidden cycle?"
        ),
        "distance_definition": (
            "minimum shortest-path edge distance in the full graph between "
            "vertices of two C4 cycles"
        ),
        "isomorphism": (
            "exact NetworkX VF2; WL hash and simple invariants used only for bucketing"
        ),
        "primary_control": (
            "RANDOM-phase controls; ELITE controls are a sensitivity check because "
            "ELITE is score-biased"
        ),
        "raw_counts": {
            "target": len(targets_raw),
            "control_total": len(controls_raw),
            "control_random": len(controls_random_raw),
            "control_elite": len(controls_elite_raw),
        },
        "groups": summary_groups,
        "primary_descriptive_contrast": {
            "target_mean_pair_distance_per_graph": target_mean,
            "control_random_mean_pair_distance_per_graph": random_mean,
            "difference_target_minus_control_random": (
                target_mean - random_mean
                if target_mean is not None and random_mean is not None
                else None
            ),
            "target_median_d_min": target_dmin,
            "control_random_median_d_min": random_dmin,
            "note": (
                "Descriptive only: search-derived graphs are not IID samples. "
                "A larger target distance supports, but does not prove, a repulsion hypothesis."
            ),
        },
    }

    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    per_graph_path = args.output_dir / "per_graph.csv"
    with per_graph_path.open("w", newline="", encoding="utf-8") as fh:
        fields = [
            "group", "graph_hash", "source", "phase", "order", "edge_count",
            "distance_signature", "d_min", "d_mean", "d_max", "union_vertices",
            "vertex_overlap_pairs", "edge_overlap_pairs",
            "all_vertex_disjoint", "all_edge_disjoint",
        ]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for group, rows in groups_rows.items():
            for row in rows:
                writer.writerow({
                    "group": group,
                    "graph_hash": row["graph_hash"],
                    "source": row["source"],
                    "phase": row["phase"],
                    "order": row["order"],
                    "edge_count": row["edge_count"],
                    "distance_signature": ",".join(map(str, row["distance_signature"])),
                    "d_min": row["d_min"],
                    "d_mean": row["d_mean"],
                    "d_max": row["d_max"],
                    "union_vertices": row["union_vertices"],
                    "vertex_overlap_pairs": row["vertex_overlap_pairs"],
                    "edge_overlap_pairs": row["edge_overlap_pairs"],
                    "all_vertex_disjoint": row["all_vertex_disjoint"],
                    "all_edge_disjoint": row["all_edge_disjoint"],
                })

    pair_path = args.output_dir / "pair_distances.csv"
    with pair_path.open("w", newline="", encoding="utf-8") as fh:
        fields = [
            "group", "graph_hash", "phase", "cycle_i", "cycle_j",
            "distance", "shared_vertices", "shared_edges",
        ]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for group, rows in groups_rows.items():
            for row in rows:
                for pair in row["pairs"]:
                    writer.writerow({
                        "group": group,
                        "graph_hash": row["graph_hash"],
                        "phase": row["phase"],
                        **pair,
                    })

    write_representatives(
        args.output_dir / "representatives_target.jsonl", targets
    )
    write_representatives(
        args.output_dir / "representatives_control_random.jsonl", controls_random
    )
    write_representatives(
        args.output_dir / "representatives_control_elite.jsonl", controls_elite
    )
    write_representatives(
        args.output_dir / "representatives_control_all.jsonl", controls_all
    )

    print("=== non-isomorphic classes ===")
    print(f"TARGET clean:          {len(targets):,}")
    print(f"CONTROL RANDOM dirty: {len(controls_random):,}")
    print(f"CONTROL ELITE dirty:  {len(controls_elite):,}")
    print(f"CONTROL ALL dirty:    {len(controls_all):,}")
    print()

    for name in ("target_clean", "control_random_dirty", "control_elite_dirty"):
        s = summary_groups[name]
        print(f"=== {name} ===")
        print(
            f"graphs={s['graphs']:,} pairs={s['cycle_pairs']:,} "
            f"mean_pair_d={s['pair_distance_mean']} "
            f"median_d_min={s['per_graph_d_min_median']} "
            f"median_d_mean={s['per_graph_d_mean_median']} "
            f"median_d_max={s['per_graph_d_max_median']}"
        )
        if s["graphs"]:
            print(
                f"all vertex-disjoint={s['all_vertex_disjoint_graphs']}/{s['graphs']} "
                f"({100*s['all_vertex_disjoint_fraction']:.1f}%)"
            )
        print(f"union |V(C4s)|={s['union_vertex_count_histogram']}")
        print()

    contrast = summary["primary_descriptive_contrast"]
    print("=== PRIMARY: TARGET vs RANDOM control ===")
    print(
        "mean per-graph pair distance: "
        f"{contrast['target_mean_pair_distance_per_graph']} vs "
        f"{contrast['control_random_mean_pair_distance_per_graph']}"
    )
    print(
        "difference target-control: "
        f"{contrast['difference_target_minus_control_random']}"
    )
    print(
        "median d_min: "
        f"{contrast['target_median_d_min']} vs "
        f"{contrast['control_random_median_d_min']}"
    )
    print()
    print(f"summary: {args.output_dir / 'summary.json'}")
    print(f"per graph: {per_graph_path}")
    print(f"pairs: {pair_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
