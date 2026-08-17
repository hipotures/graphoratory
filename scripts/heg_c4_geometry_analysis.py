#!/usr/bin/env python3
"""
Analyze the geometry of residual C4 cycles in HEG search hits.

Default comparison:
    (C4,C8,C16,C32) = (4,0,0,0)
vs
    (C4,C8,C16,C32) = (3,1,0,0)

Distance between two C4 cycles is defined as

    d(C_i, C_j) = min_{u in V(C_i), v in V(C_j)} d_G(u,v),

where d_G is ordinary shortest-path distance in the full graph.

Thus:
    d=0  cycles share at least one vertex
    d=1  vertex-disjoint cycles have an edge joining them
    d>=2 cycles are increasingly separated

The script:
  * reads JSONL or JSONL.GZ hit records;
  * filters requested exact profiles;
  * deduplicates by graph_hash by default;
  * independently enumerates all simple C4 cycles in each graph;
  * validates that enumerated C4 count matches the recorded profile;
  * computes all pairwise C4 distances and overlap data;
  * writes:
      summary.json
      per_graph.csv
      pair_distances.csv
      cycles.jsonl
  * prints a compact human-readable summary.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


DEFAULT_PROFILES = ((4, 0, 0, 0), (3, 1, 0, 0))
PROFILE_LENGTHS = (4, 8, 16, 32)


@dataclass(frozen=True, slots=True)
class CycleGeometry:
    vertices: tuple[int, int, int, int]
    edges: frozenset[tuple[int, int]]


@dataclass(frozen=True, slots=True)
class PairGeometry:
    i: int
    j: int
    distance: int
    shared_vertices: int
    shared_edges: int


def parse_profile(raw: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "profile must contain four comma-separated integers, e.g. 4,0,0,0"
        )
    try:
        values = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("profile entries must be integers") from exc
    if any(value < 0 for value in values):
        raise argparse.ArgumentTypeError("profile entries must be non-negative")
    return values  # type: ignore[return-value]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze distances and overlap among residual C4 cycles in HEG hits."
    )
    parser.add_argument(
        "hits",
        type=Path,
        help="Input hits.jsonl or hits.jsonl.gz",
    )
    parser.add_argument(
        "--profile",
        action="append",
        type=parse_profile,
        default=[],
        help=(
            "Profile C4,C8,C16,C32 to analyze; may be repeated. "
            "Default: 4,0,0,0 and 3,1,0,0"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/analysis/c4_geometry"),
    )
    parser.add_argument(
        "--keep-duplicate-hashes",
        action="store_true",
        help="Do not deduplicate repeated graph_hash values.",
    )
    parser.add_argument(
        "--allow-c4-mismatch",
        action="store_true",
        help=(
            "Keep records even if independent C4 enumeration disagrees with "
            "the recorded C4 count. By default such a mismatch is fatal."
        ),
    )
    parser.add_argument(
        "--top-signatures",
        type=int,
        default=20,
        help="Number of most common sorted distance signatures shown/saved.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="Optional cap on selected records for debugging; 0 means unlimited.",
    )
    args = parser.parse_args()
    if args.top_signatures < 1:
        parser.error("--top-signatures must be >= 1")
    if args.max_records < 0:
        parser.error("--max-records must be >= 0")
    if not args.profile:
        args.profile = list(DEFAULT_PROFILES)
    return args


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_jsonl(path: Path) -> Iterator[dict]:
    with open_text(path) as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise RuntimeError(f"{path}:{line_no}: JSON root is not an object")
            yield payload


def observed_component(value) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        observed = value.get("observed")
        if isinstance(observed, int):
            return observed
    return None


def profile_from_record(record: dict) -> tuple[int, int, int, int] | None:
    components = record.get("components")
    if not isinstance(components, dict):
        score = record.get("score")
        if isinstance(score, dict):
            components = score.get("components")
    if not isinstance(components, dict):
        return None

    values: list[int] = []
    for length in PROFILE_LENGTHS:
        value = observed_component(components.get(str(length)))
        if value is None:
            return None
        values.append(value)
    return tuple(values)  # type: ignore[return-value]


def normalize_edges(record: dict) -> tuple[int, tuple[tuple[int, int], ...]]:
    raw_edges = record.get("edges")
    if not isinstance(raw_edges, list):
        raise ValueError("record has no edges list")

    order_raw = record.get("order")
    if isinstance(order_raw, int):
        order = order_raw
    else:
        max_vertex = -1
        for edge in raw_edges:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2:
                raise ValueError("malformed edge")
            max_vertex = max(max_vertex, int(edge[0]), int(edge[1]))
        order = max_vertex + 1

    edges: set[tuple[int, int]] = set()
    for raw in raw_edges:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("malformed edge")
        u, v = int(raw[0]), int(raw[1])
        if u == v:
            raise ValueError("self-loop")
        if not (0 <= u < order and 0 <= v < order):
            raise ValueError("edge endpoint outside graph order")
        edge = (u, v) if u < v else (v, u)
        if edge in edges:
            raise ValueError("duplicate edge")
        edges.add(edge)

    return order, tuple(sorted(edges))


def adjacency(order: int, edges: Sequence[tuple[int, int]]) -> list[set[int]]:
    adj = [set() for _ in range(order)]
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)
    return adj


def canonical_cycle4(vertices: Sequence[int]) -> tuple[int, int, int, int]:
    if len(vertices) != 4:
        raise ValueError("expected four vertices")
    seq = tuple(vertices)
    rev = tuple(reversed(seq))
    candidates = []
    for base in (seq, rev):
        for shift in range(4):
            candidates.append(base[shift:] + base[:shift])
    return min(candidates)


def cycle_edges(vertices: Sequence[int]) -> frozenset[tuple[int, int]]:
    seq = tuple(vertices)
    result = set()
    for i in range(4):
        u = seq[i]
        v = seq[(i + 1) % 4]
        result.add((u, v) if u < v else (v, u))
    return frozenset(result)


def enumerate_c4(adj: Sequence[set[int]]) -> list[CycleGeometry]:
    """Enumerate all simple undirected 4-cycles exactly once.

    This enumerates cycles rather than 4-vertex sets, so graphs with chords
    (including K4) are handled correctly.
    """
    n = len(adj)
    seen: set[tuple[int, int, int, int]] = set()

    for a in range(n):
        for b in adj[a]:
            if b == a:
                continue
            for c in adj[b]:
                if c in (a, b):
                    continue
                for d in adj[c]:
                    if d in (a, b, c):
                        continue
                    if a not in adj[d]:
                        continue
                    seen.add(canonical_cycle4((a, b, c, d)))

    cycles = [
        CycleGeometry(vertices=cycle, edges=cycle_edges(cycle))
        for cycle in sorted(seen)
    ]
    return cycles


def shortest_set_distance(
    adj: Sequence[set[int]],
    left: frozenset[int],
    right: frozenset[int],
) -> int:
    if left & right:
        return 0

    queue = deque()
    distance = [-1] * len(adj)
    for source in left:
        distance[source] = 0
        queue.append(source)

    while queue:
        u = queue.popleft()
        next_distance = distance[u] + 1
        for v in adj[u]:
            if distance[v] != -1:
                continue
            if v in right:
                return next_distance
            distance[v] = next_distance
            queue.append(v)

    raise RuntimeError("graph appears disconnected")


def analyze_pairs(
    adj: Sequence[set[int]],
    cycles: Sequence[CycleGeometry],
) -> list[PairGeometry]:
    pairs: list[PairGeometry] = []
    vertex_sets = [frozenset(cycle.vertices) for cycle in cycles]

    for i in range(len(cycles)):
        for j in range(i + 1, len(cycles)):
            shared_vertices = len(vertex_sets[i] & vertex_sets[j])
            shared_edges = len(cycles[i].edges & cycles[j].edges)
            distance = shortest_set_distance(adj, vertex_sets[i], vertex_sets[j])
            pairs.append(
                PairGeometry(
                    i=i,
                    j=j,
                    distance=distance,
                    shared_vertices=shared_vertices,
                    shared_edges=shared_edges,
                )
            )
    return pairs


def connected_components_on_vertices(
    vertices: set[int],
    edges: Iterable[tuple[int, int]],
) -> int:
    if not vertices:
        return 0
    local_adj = {v: set() for v in vertices}
    for u, v in edges:
        if u in vertices and v in vertices:
            local_adj[u].add(v)
            local_adj[v].add(u)

    unseen = set(vertices)
    count = 0
    while unseen:
        count += 1
        start = next(iter(unseen))
        stack = [start]
        unseen.remove(start)
        while stack:
            u = stack.pop()
            for v in local_adj[u]:
                if v in unseen:
                    unseen.remove(v)
                    stack.append(v)
    return count


def mean_or_none(values: Sequence[int]) -> float | None:
    return statistics.fmean(values) if values else None


def median_or_none(values: Sequence[float | int]) -> float | None:
    return float(statistics.median(values)) if values else None


def safe_mean(values: Sequence[float | int]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def histogram_json(counter: Counter) -> dict[str, int]:
    return {str(key): int(counter[key]) for key in sorted(counter)}


def profile_label(profile: tuple[int, int, int, int]) -> str:
    return "(" + ",".join(str(value) for value in profile) + ")"


def summarize_group(rows: list[dict], top_signatures: int) -> dict:
    pair_hist: Counter[int] = Counter()
    union_hist: Counter[int] = Counter()
    signature_hist: Counter[str] = Counter()

    dmins: list[int] = []
    dmeans: list[float] = []
    dmaxs: list[int] = []
    pair_distances: list[int] = []

    all_vertex_disjoint = 0
    all_edge_disjoint = 0
    graphs_with_vertex_overlap = 0
    graphs_with_edge_overlap = 0
    total_overlap_vertex_pairs = 0
    total_overlap_edge_pairs = 0
    total_close_le1_pairs = 0
    total_pairs = 0

    for row in rows:
        distances = row["distances"]
        pair_distances.extend(distances)
        pair_hist.update(distances)
        union_hist[row["union_vertices"]] += 1
        signature_hist[row["distance_signature"]] += 1

        if distances:
            dmins.append(min(distances))
            dmeans.append(statistics.fmean(distances))
            dmaxs.append(max(distances))

        total_pairs += row["pair_count"]
        total_overlap_vertex_pairs += row["vertex_overlap_pairs"]
        total_overlap_edge_pairs += row["edge_overlap_pairs"]
        total_close_le1_pairs += row["distance_le1_pairs"]

        if row["all_vertex_disjoint"]:
            all_vertex_disjoint += 1
        else:
            graphs_with_vertex_overlap += 1
        if row["all_edge_disjoint"]:
            all_edge_disjoint += 1
        else:
            graphs_with_edge_overlap += 1

    graph_count = len(rows)
    return {
        "graphs": graph_count,
        "cycle_pairs": total_pairs,
        "pair_distance_histogram": histogram_json(pair_hist),
        "pair_distance_mean": safe_mean(pair_distances),
        "pair_distance_median": median_or_none(pair_distances),
        "per_graph_d_min_mean": safe_mean(dmins),
        "per_graph_d_min_median": median_or_none(dmins),
        "per_graph_d_mean_mean": safe_mean(dmeans),
        "per_graph_d_mean_median": median_or_none(dmeans),
        "per_graph_d_max_mean": safe_mean(dmaxs),
        "per_graph_d_max_median": median_or_none(dmaxs),
        "union_vertex_count_histogram": histogram_json(union_hist),
        "all_vertex_disjoint_graphs": all_vertex_disjoint,
        "all_vertex_disjoint_fraction": (
            all_vertex_disjoint / graph_count if graph_count else None
        ),
        "all_edge_disjoint_graphs": all_edge_disjoint,
        "all_edge_disjoint_fraction": (
            all_edge_disjoint / graph_count if graph_count else None
        ),
        "graphs_with_vertex_overlap": graphs_with_vertex_overlap,
        "graphs_with_edge_overlap": graphs_with_edge_overlap,
        "vertex_overlap_pairs": total_overlap_vertex_pairs,
        "edge_overlap_pairs": total_overlap_edge_pairs,
        "distance_le1_pairs": total_close_le1_pairs,
        "distance_le1_pair_fraction": (
            total_close_le1_pairs / total_pairs if total_pairs else None
        ),
        "top_distance_signatures": [
            {"signature": signature, "count": count}
            for signature, count in signature_hist.most_common(top_signatures)
        ],
    }


def main() -> int:
    args = parse_args()
    wanted = set(args.profile)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    selected: list[dict] = []
    seen_hashes: set[str] = set()
    scanned = 0
    skipped_profiles = 0
    duplicate_hashes = 0

    for record in iter_jsonl(args.hits):
        scanned += 1
        profile = profile_from_record(record)
        if profile not in wanted:
            skipped_profiles += 1
            continue

        graph_hash = record.get("graph_hash")
        if not isinstance(graph_hash, str) or not graph_hash:
            graph_hash = f"record-{scanned}"

        if not args.keep_duplicate_hashes and graph_hash in seen_hashes:
            duplicate_hashes += 1
            continue
        seen_hashes.add(graph_hash)

        order, edges = normalize_edges(record)
        adj = adjacency(order, edges)
        cycles = enumerate_c4(adj)

        expected_c4 = profile[0]
        if len(cycles) != expected_c4 and not args.allow_c4_mismatch:
            raise RuntimeError(
                f"{graph_hash}: recorded C4={expected_c4}, "
                f"independent enumeration found {len(cycles)}"
            )

        pairs = analyze_pairs(adj, cycles)
        distances = [pair.distance for pair in pairs]

        cycle_vertices = set().union(*(set(cycle.vertices) for cycle in cycles))
        cycle_edge_union = set().union(*(set(cycle.edges) for cycle in cycles))
        ambient_induced_edges = {
            edge
            for edge in edges
            if edge[0] in cycle_vertices and edge[1] in cycle_vertices
        }

        row = {
            "graph_hash": graph_hash,
            "profile": profile,
            "profile_label": profile_label(profile),
            "order": order,
            "edge_count": len(edges),
            "c4_count_recorded": expected_c4,
            "c4_count_enumerated": len(cycles),
            "cycles": cycles,
            "pairs": pairs,
            "distances": distances,
            "pair_count": len(pairs),
            "distance_signature": ",".join(str(x) for x in sorted(distances)),
            "d_min": min(distances) if distances else None,
            "d_mean": statistics.fmean(distances) if distances else None,
            "d_max": max(distances) if distances else None,
            "union_vertices": len(cycle_vertices),
            "cycle_edge_union_count": len(cycle_edge_union),
            "ambient_induced_edge_count": len(ambient_induced_edges),
            "cycle_edge_union_components": connected_components_on_vertices(
                cycle_vertices, cycle_edge_union
            ),
            "ambient_induced_components": connected_components_on_vertices(
                cycle_vertices, ambient_induced_edges
            ),
            "vertex_overlap_pairs": sum(p.shared_vertices > 0 for p in pairs),
            "edge_overlap_pairs": sum(p.shared_edges > 0 for p in pairs),
            "distance_le1_pairs": sum(p.distance <= 1 for p in pairs),
            "all_vertex_disjoint": all(p.shared_vertices == 0 for p in pairs),
            "all_edge_disjoint": all(p.shared_edges == 0 for p in pairs),
        }
        selected.append(row)

        if args.max_records and len(selected) >= args.max_records:
            break

    per_graph_path = args.output_dir / "per_graph.csv"
    with per_graph_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "graph_hash",
                "profile",
                "order",
                "edge_count",
                "c4_count_recorded",
                "c4_count_enumerated",
                "pair_count",
                "distance_signature",
                "d_min",
                "d_mean",
                "d_max",
                "union_vertices",
                "cycle_edge_union_count",
                "ambient_induced_edge_count",
                "cycle_edge_union_components",
                "ambient_induced_components",
                "vertex_overlap_pairs",
                "edge_overlap_pairs",
                "distance_le1_pairs",
                "all_vertex_disjoint",
                "all_edge_disjoint",
            ],
        )
        writer.writeheader()
        for row in selected:
            writer.writerow(
                {
                    key: (
                        row["profile_label"]
                        if key == "profile"
                        else row[key]
                    )
                    for key in writer.fieldnames
                }
            )

    pair_path = args.output_dir / "pair_distances.csv"
    with pair_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "graph_hash",
                "profile",
                "cycle_i",
                "cycle_j",
                "distance",
                "shared_vertices",
                "shared_edges",
            ],
        )
        writer.writeheader()
        for row in selected:
            for pair in row["pairs"]:
                writer.writerow(
                    {
                        "graph_hash": row["graph_hash"],
                        "profile": row["profile_label"],
                        "cycle_i": pair.i,
                        "cycle_j": pair.j,
                        "distance": pair.distance,
                        "shared_vertices": pair.shared_vertices,
                        "shared_edges": pair.shared_edges,
                    }
                )

    cycles_path = args.output_dir / "cycles.jsonl"
    with cycles_path.open("w", encoding="utf-8") as handle:
        for row in selected:
            payload = {
                "graph_hash": row["graph_hash"],
                "profile": list(row["profile"]),
                "order": row["order"],
                "edge_count": row["edge_count"],
                "cycles": [list(cycle.vertices) for cycle in row["cycles"]],
                "distance_signature": [int(x) for x in sorted(row["distances"])],
                "union_vertices": row["union_vertices"],
            }
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    grouped: dict[tuple[int, int, int, int], list[dict]] = {
        profile: [] for profile in args.profile
    }
    for row in selected:
        grouped[row["profile"]].append(row)

    summary = {
        "input": str(args.hits),
        "definition": {
            "cycle_distance": (
                "min shortest-path edge distance in the full graph between "
                "a vertex of one C4 and a vertex of the other C4"
            ),
            "distance_0": "the cycles share at least one vertex",
            "distance_1": (
                "the cycles are vertex-disjoint and at least one graph edge "
                "joins the two cycle vertex sets"
            ),
        },
        "scanned_records": scanned,
        "selected_records": len(selected),
        "duplicate_hashes_skipped": duplicate_hashes,
        "profiles": {
            profile_label(profile): summarize_group(rows, args.top_signatures)
            for profile, rows in grouped.items()
        },
    }

    # Simple descriptive contrast on the per-graph mean pair distance.
    if len(args.profile) >= 2:
        a = args.profile[0]
        b = args.profile[1]
        a_values = [row["d_mean"] for row in grouped[a] if row["d_mean"] is not None]
        b_values = [row["d_mean"] for row in grouped[b] if row["d_mean"] is not None]
        summary["comparison_first_two_profiles"] = {
            "a": profile_label(a),
            "b": profile_label(b),
            "a_graphs": len(a_values),
            "b_graphs": len(b_values),
            "a_mean_of_graph_mean_distances": safe_mean(a_values),
            "b_mean_of_graph_mean_distances": safe_mean(b_values),
            "difference_a_minus_b": (
                safe_mean(a_values) - safe_mean(b_values)
                if a_values and b_values
                else None
            ),
            "note": (
                "Descriptive only. Search hits are not IID samples and the "
                "ELITE policy biases profile frequencies."
            ),
        }

    summary_path = args.output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"Scanned:  {scanned:,}")
    print(f"Selected: {len(selected):,}")
    if duplicate_hashes:
        print(f"Duplicate hashes skipped: {duplicate_hashes:,}")
    print()

    for profile in args.profile:
        label = profile_label(profile)
        group = summary["profiles"][label]
        print(f"=== profile {label} ===")
        print(f"graphs={group['graphs']:,} cycle_pairs={group['cycle_pairs']:,}")
        print(
            "pair distances: "
            + ", ".join(
                f"d={distance}:{count}"
                for distance, count in group["pair_distance_histogram"].items()
            )
        )
        print(
            "per graph: "
            f"median d_min={group['per_graph_d_min_median']}  "
            f"median d_mean={group['per_graph_d_mean_median']}  "
            f"median d_max={group['per_graph_d_max_median']}"
        )
        print(
            f"all vertex-disjoint: "
            f"{group['all_vertex_disjoint_graphs']}/{group['graphs']} "
            f"({100.0 * group['all_vertex_disjoint_fraction']:.1f}%)"
            if group["graphs"]
            else "all vertex-disjoint: n/a"
        )
        print(
            "union |V(C4s)|: "
            + ", ".join(
                f"{k}:{v}"
                for k, v in group["union_vertex_count_histogram"].items()
            )
        )
        print("top signatures:")
        for item in group["top_distance_signatures"][:10]:
            print(f"  {item['count']:>6}  [{item['signature']}]")
        print()

    print(f"summary:        {summary_path}")
    print(f"per graph:      {per_graph_path}")
    print(f"pair distances: {pair_path}")
    print(f"cycles:         {cycles_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
