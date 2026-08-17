#!/usr/bin/env python3
"""
Exact isomorphism deduplication for HEG search hits.

Typical use:

    uv run python scripts/heg_isomorphism_dedup.py \
      results/sweeps/random_cascade_long/order_32/hits.jsonl \
      --profile 4,0,0,0 \
      --profile 3,1,0,0 \
      --output-dir results/analysis/isomorphism_n32

The script deduplicates graphs up to unlabeled graph isomorphism. It never treats a
hash collision or a Weisfeiler-Lehman collision as proof of isomorphism.

Backends:
  * pynauty  - preferred when installed; uses nauty canonical certificates.
  * networkx - exact VF2 isomorphism after safe invariant/WL bucketing.
  * auto     - use pynauty if available, otherwise networkx.

Outputs:
  summary.json
      Per-profile counts of labeled graphs and non-isomorphic classes.

  representatives.jsonl
      One original hit record for each isomorphism class, augmented with
      _iso_class_id and _iso_class_size.

  class_members.csv
      Every selected graph_hash mapped to its isomorphism class.

  classes.jsonl
      Compact class metadata, including representative hash and all member hashes.

Notes:
  * Deduplication is performed independently inside each requested cycle profile.
    This is exact because the profile itself is an isomorphism invariant.
  * Duplicate graph_hash records are removed before isomorphism analysis by default.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

PROFILE_LENGTHS = (4, 8, 16, 32)
DEFAULT_PROFILES = ((4, 0, 0, 0), (3, 1, 0, 0))


@dataclass(slots=True)
class GraphRecord:
    record: dict
    graph_hash: str
    profile: tuple[int, int, int, int]
    order: int
    edges: tuple[tuple[int, int], ...]
    degree_sequence: tuple[int, ...]
    edge_count: int


@dataclass(slots=True)
class IsoClass:
    class_id: str
    profile: tuple[int, int, int, int]
    representative: GraphRecord
    member_hashes: list[str] = field(default_factory=list)
    canonical_key: str | None = None


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


def profile_label(profile: tuple[int, int, int, int]) -> str:
    return "(" + ",".join(str(v) for v in profile) + ")"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exactly deduplicate HEG hit graphs up to graph isomorphism."
    )
    parser.add_argument("hits", type=Path, help="Input hits.jsonl or hits.jsonl.gz")
    parser.add_argument(
        "--profile",
        action="append",
        type=parse_profile,
        default=[],
        help=(
            "Cycle profile C4,C8,C16,C32 to include; repeat as needed. "
            "Default: 4,0,0,0 and 3,1,0,0"
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "pynauty", "networkx"),
        default="auto",
        help="Exact isomorphism backend. Default: auto",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/analysis/isomorphism_dedup"),
    )
    parser.add_argument(
        "--keep-duplicate-hashes",
        action="store_true",
        help="Keep repeated graph_hash records before isomorphism deduplication.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="Optional selected-record cap for debugging; 0 means unlimited.",
    )
    args = parser.parse_args()
    if not args.profile:
        args.profile = list(DEFAULT_PROFILES)
    if args.max_records < 0:
        parser.error("--max-records must be >= 0")
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
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise RuntimeError(f"{path}:{line_no}: JSON root is not an object")
            yield obj


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


def normalize_graph(record: dict, fallback_hash: str, profile) -> GraphRecord:
    raw_edges = record.get("edges")
    if not isinstance(raw_edges, list):
        raise ValueError("record has no edges list")

    raw_order = record.get("order")
    if isinstance(raw_order, int):
        order = raw_order
    else:
        max_vertex = -1
        for raw in raw_edges:
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise ValueError("malformed edge")
            max_vertex = max(max_vertex, int(raw[0]), int(raw[1]))
        order = max_vertex + 1

    if order < 0:
        raise ValueError("invalid order")

    edges: set[tuple[int, int]] = set()
    degrees = [0] * order
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
        degrees[u] += 1
        degrees[v] += 1

    graph_hash = record.get("graph_hash")
    if not isinstance(graph_hash, str) or not graph_hash:
        graph_hash = fallback_hash

    return GraphRecord(
        record=record,
        graph_hash=graph_hash,
        profile=profile,
        order=order,
        edges=tuple(sorted(edges)),
        degree_sequence=tuple(sorted(degrees)),
        edge_count=len(edges),
    )


def adjacency_dict(graph: GraphRecord) -> dict[int, list[int]]:
    adj = {v: [] for v in range(graph.order)}
    for u, v in graph.edges:
        adj[u].append(v)
        adj[v].append(u)
    return adj


def choose_backend(requested: str) -> str:
    have_pynauty = importlib.util.find_spec("pynauty") is not None
    have_networkx = importlib.util.find_spec("networkx") is not None

    if requested == "pynauty":
        if not have_pynauty:
            raise RuntimeError(
                "pynauty backend requested but package is not installed. "
                "Install it or use --backend networkx."
            )
        return "pynauty"

    if requested == "networkx":
        if not have_networkx:
            raise RuntimeError(
                "networkx backend requested but package is not installed."
            )
        return "networkx"

    if have_pynauty:
        return "pynauty"
    if have_networkx:
        return "networkx"
    raise RuntimeError(
        "Neither pynauty nor networkx is installed. Install one of them."
    )


def pynauty_certificate(graph: GraphRecord) -> bytes:
    import pynauty

    g = pynauty.Graph(
        number_of_vertices=graph.order,
        directed=False,
        adjacency_dict=adjacency_dict(graph),
    )
    return pynauty.certificate(g)


def networkx_graph(graph: GraphRecord):
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(range(graph.order))
    g.add_edges_from(graph.edges)
    return g


def networkx_bucket_key(graph: GraphRecord) -> tuple:
    """Safe isomorphism-invariant prebucket.

    The WL hash is only a prefilter. Exact VF2 decides isomorphism.
    """
    import networkx as nx

    g = networkx_graph(graph)
    wl = nx.weisfeiler_lehman_graph_hash(g, iterations=4)
    triangles = tuple(sorted(nx.triangles(g).values()))
    return (
        graph.order,
        graph.edge_count,
        graph.degree_sequence,
        triangles,
        wl,
    )


def dedup_pynauty(
    graphs: Sequence[GraphRecord],
    profile: tuple[int, int, int, int],
) -> list[IsoClass]:
    by_cert: dict[bytes, IsoClass] = {}
    next_id = 1

    for index, graph in enumerate(graphs, start=1):
        cert = pynauty_certificate(graph)
        iso_class = by_cert.get(cert)
        if iso_class is None:
            class_id = f"{profile_label(profile)}:iso-{next_id:05d}"
            next_id += 1
            iso_class = IsoClass(
                class_id=class_id,
                profile=profile,
                representative=graph,
                member_hashes=[],
                canonical_key=hashlib.sha256(cert).hexdigest(),
            )
            by_cert[cert] = iso_class
        iso_class.member_hashes.append(graph.graph_hash)

        if index % 1000 == 0:
            print(
                f"  {profile_label(profile)}: {index:,}/{len(graphs):,} "
                f"graphs -> {len(by_cert):,} iso classes",
                file=sys.stderr,
            )

    return list(by_cert.values())


def dedup_networkx(
    graphs: Sequence[GraphRecord],
    profile: tuple[int, int, int, int],
) -> list[IsoClass]:
    import networkx as nx

    # Each invariant bucket stores pairs (representative nx.Graph, IsoClass).
    buckets: dict[tuple, list[tuple[object, IsoClass]]] = defaultdict(list)
    classes: list[IsoClass] = []
    next_id = 1

    for index, graph in enumerate(graphs, start=1):
        key = networkx_bucket_key(graph)
        g = networkx_graph(graph)

        match: IsoClass | None = None
        for rep_graph, iso_class in buckets[key]:
            # VF2 is the exact decision. The WL/invariant key is only a safe prefilter.
            if nx.is_isomorphic(g, rep_graph):
                match = iso_class
                break

        if match is None:
            class_id = f"{profile_label(profile)}:iso-{next_id:05d}"
            next_id += 1
            match = IsoClass(
                class_id=class_id,
                profile=profile,
                representative=graph,
                member_hashes=[],
                canonical_key=None,
            )
            buckets[key].append((g, match))
            classes.append(match)

        match.member_hashes.append(graph.graph_hash)

        if index % 250 == 0:
            print(
                f"  {profile_label(profile)}: {index:,}/{len(graphs):,} "
                f"graphs -> {len(classes):,} iso classes",
                file=sys.stderr,
            )

    return classes


def main() -> int:
    args = parse_args()
    backend = choose_backend(args.backend)
    wanted = set(args.profile)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    selected: dict[tuple[int, int, int, int], list[GraphRecord]] = {
        profile: [] for profile in args.profile
    }
    seen_hashes: set[str] = set()

    scanned = 0
    duplicate_hashes = 0

    for record in iter_jsonl(args.hits):
        scanned += 1
        profile = profile_from_record(record)
        if profile not in wanted:
            continue

        graph_hash = record.get("graph_hash")
        if not isinstance(graph_hash, str) or not graph_hash:
            graph_hash = f"record-{scanned}"

        if not args.keep_duplicate_hashes and graph_hash in seen_hashes:
            duplicate_hashes += 1
            continue
        seen_hashes.add(graph_hash)

        graph = normalize_graph(record, graph_hash, profile)
        selected[profile].append(graph)

        if args.max_records and sum(len(v) for v in selected.values()) >= args.max_records:
            break

    print(f"backend:  {backend}")
    print(f"scanned:  {scanned:,}")
    print(f"selected: {sum(len(v) for v in selected.values()):,}")
    if duplicate_hashes:
        print(f"duplicate graph_hash records skipped: {duplicate_hashes:,}")
    print()

    all_classes: list[IsoClass] = []
    for profile in args.profile:
        graphs = selected[profile]
        print(
            f"Deduplicating {profile_label(profile)}: {len(graphs):,} graphs...",
            file=sys.stderr,
        )
        if backend == "pynauty":
            classes = dedup_pynauty(graphs, profile)
        else:
            classes = dedup_networkx(graphs, profile)
        all_classes.extend(classes)

    members_path = args.output_dir / "class_members.csv"
    with members_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "profile",
                "iso_class_id",
                "representative_hash",
                "class_size",
                "graph_hash",
            ],
        )
        writer.writeheader()
        for iso_class in all_classes:
            for member_hash in iso_class.member_hashes:
                writer.writerow(
                    {
                        "profile": profile_label(iso_class.profile),
                        "iso_class_id": iso_class.class_id,
                        "representative_hash": iso_class.representative.graph_hash,
                        "class_size": len(iso_class.member_hashes),
                        "graph_hash": member_hash,
                    }
                )

    reps_path = args.output_dir / "representatives.jsonl"
    with reps_path.open("w", encoding="utf-8") as handle:
        for iso_class in all_classes:
            payload = dict(iso_class.representative.record)
            payload["_iso_class_id"] = iso_class.class_id
            payload["_iso_class_size"] = len(iso_class.member_hashes)
            payload["_iso_profile"] = list(iso_class.profile)
            if iso_class.canonical_key is not None:
                payload["_nauty_certificate_sha256"] = iso_class.canonical_key
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    classes_path = args.output_dir / "classes.jsonl"
    with classes_path.open("w", encoding="utf-8") as handle:
        for iso_class in all_classes:
            payload = {
                "profile": list(iso_class.profile),
                "iso_class_id": iso_class.class_id,
                "class_size": len(iso_class.member_hashes),
                "representative_hash": iso_class.representative.graph_hash,
                "member_hashes": iso_class.member_hashes,
            }
            if iso_class.canonical_key is not None:
                payload["nauty_certificate_sha256"] = iso_class.canonical_key
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    profile_summary = {}
    for profile in args.profile:
        graphs = selected[profile]
        classes = [c for c in all_classes if c.profile == profile]
        size_hist = Counter(len(c.member_hashes) for c in classes)
        largest = sorted(
            classes,
            key=lambda c: (-len(c.member_hashes), c.representative.graph_hash),
        )[:20]
        profile_summary[profile_label(profile)] = {
            "labeled_graphs": len(graphs),
            "nonisomorphic_classes": len(classes),
            "isomorphism_reduction_factor": (
                len(graphs) / len(classes) if classes else None
            ),
            "class_size_histogram": {
                str(size): count for size, count in sorted(size_hist.items())
            },
            "largest_classes": [
                {
                    "iso_class_id": c.class_id,
                    "size": len(c.member_hashes),
                    "representative_hash": c.representative.graph_hash,
                }
                for c in largest
            ],
        }

    summary = {
        "input": str(args.hits),
        "backend": backend,
        "exact_isomorphism": True,
        "scanned_records": scanned,
        "selected_records": sum(len(v) for v in selected.values()),
        "duplicate_graph_hash_records_skipped": duplicate_hashes,
        "profiles": profile_summary,
        "notes": [
            "Profiles are deduplicated independently because cycle-count profiles are isomorphism invariants.",
            (
                "With networkx, Weisfeiler-Lehman and other invariants are used only "
                "for bucketing; VF2 makes the exact isomorphism decision."
            )
            if backend == "networkx"
            else "pynauty/nauty canonical certificates define the isomorphism classes.",
        ],
    }

    summary_path = args.output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print()
    for profile in args.profile:
        label = profile_label(profile)
        item = profile_summary[label]
        print(f"=== {label} ===")
        print(f"labeled graphs:          {item['labeled_graphs']:,}")
        print(f"non-isomorphic classes: {item['nonisomorphic_classes']:,}")
        if item["isomorphism_reduction_factor"] is not None:
            print(
                "reduction factor:       "
                f"{item['isomorphism_reduction_factor']:.3f}x"
            )
        print("largest classes:")
        for c in item["largest_classes"][:10]:
            print(
                f"  {c['size']:>6}  {c['iso_class_id']}  "
                f"rep={c['representative_hash'][:12]}"
            )
        print()

    print(f"summary:         {summary_path}")
    print(f"representatives: {reps_path}")
    print(f"class members:   {members_path}")
    print(f"classes:         {classes_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
