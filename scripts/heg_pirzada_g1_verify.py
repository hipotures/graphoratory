#!/usr/bin/env python3
"""
Reconstruct and exactly verify the G1 construction from:

    S. Pirzada, M. A. Shah, E. T. Baskoro,
    "On 2-power unicyclic cubic graphs",
    Electronic Journal of Graph Theory and Applications 10(1), 2022, 337-344.
    DOI: 10.5614/ejgta.2022.10.1.24

The paper describes G1 as follows:

1. Start from the 8-vertex cubic graph shown in Fig. 2.
2. Remove e = uv to obtain K (Fig. 3).
3. Take five copies of K named H1, H2, J1, J2, H.
4. Add a K3 on w1,w2,w3.
5. Add K_{1,3}+x on z1,z2,z3,z4, with z1 pendant.
6. Add the eight edges listed in the proof.
7. This gives X1 on 47 vertices, with z1 of degree 2.
8. Take two copies X1, X1' and join z1-z1', obtaining cubic G1 on 94 vertices.

This script transcribes the PUBLISHED FIGURES + EDGE LIST literally.

Important design choices
------------------------
- Exact cycle counting is performed with a low-memory DFS enumerator.
- We exploit the bridge z1-z1': every cycle is wholly inside one 47-vertex
  half. Therefore C_k(G1) = 2*C_k(X1) for k <= 47 and C_k(G1)=0 for k>47.
- This avoids doing a full n=94 C64 search.
- Optional --score-worker cross-checks C4/C8/C16/C32 using the repository's
  authoritative ScoreWorker, again only on the 47-vertex half and one length
  at a time.
- The script explicitly checks the paper's intermediate claim about K, rather
  than assuming it.

Typical use inside graphoratory:

    uv run python scripts/heg_pirzada_g1_verify.py

With independent ScoreWorker cross-check:

    uv run python scripts/heg_pirzada_g1_verify.py \
      --score-worker \
      --node-budget 10000000 \
      --witness-cap 1000000

The output JSON contains the full 94-vertex edge list and exact counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

Edge = tuple[int, int]


def norm_edge(u: int, v: int) -> Edge:
    if u == v:
        raise ValueError(f"self-loop {u}-{v}")
    return (u, v) if u < v else (v, u)


@dataclass
class NamedBuilder:
    names: list[str]
    name_to_id: dict[str, int]
    edges: set[Edge]

    @classmethod
    def empty(cls) -> "NamedBuilder":
        return cls([], {}, set())

    def vertex(self, name: str) -> int:
        if name in self.name_to_id:
            return self.name_to_id[name]
        idx = len(self.names)
        self.names.append(name)
        self.name_to_id[name] = idx
        return idx

    def edge(self, a: str, b: str) -> None:
        u = self.vertex(a)
        v = self.vertex(b)
        edge = norm_edge(u, v)
        if edge in self.edges:
            raise ValueError(f"duplicate edge {a}-{b}")
        self.edges.add(edge)

    def edge_ids(self, u: int, v: int) -> None:
        edge = norm_edge(u, v)
        if edge in self.edges:
            raise ValueError(f"duplicate edge {edge}")
        self.edges.add(edge)


def adjacency(order: int, edges: Iterable[Edge]) -> list[list[int]]:
    adj = [[] for _ in range(order)]
    for u, v in edges:
        adj[u].append(v)
        adj[v].append(u)
    for row in adj:
        row.sort()
    return adj


def degree_sequence(order: int, edges: Iterable[Edge]) -> tuple[int, ...]:
    deg = [0] * order
    for u, v in edges:
        deg[u] += 1
        deg[v] += 1
    return tuple(deg)


def is_connected(order: int, edges: Iterable[Edge]) -> bool:
    if order == 0:
        return True
    adj = adjacency(order, edges)
    seen = {0}
    stack = [0]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return len(seen) == order


def shortest_path_length(
    order: int,
    edges: Iterable[Edge],
    source: int,
    target: int,
) -> int | None:
    adj = adjacency(order, edges)
    dist = [-1] * order
    dist[source] = 0
    queue = [source]
    head = 0
    while head < len(queue):
        u = queue[head]
        head += 1
        if u == target:
            return dist[u]
        for v in adj[u]:
            if dist[v] < 0:
                dist[v] = dist[u] + 1
                queue.append(v)
    return None


def bridge_components(
    order: int,
    edges: set[Edge],
    bridge: Edge,
) -> list[set[int]]:
    if bridge not in edges:
        raise ValueError(f"bridge {bridge} not present")
    reduced = set(edges)
    reduced.remove(bridge)
    adj = adjacency(order, reduced)

    unseen = set(range(order))
    comps: list[set[int]] = []
    while unseen:
        root = next(iter(unseen))
        comp = {root}
        stack = [root]
        unseen.remove(root)
        while stack:
            u = stack.pop()
            for v in adj[u]:
                if v in unseen:
                    unseen.remove(v)
                    comp.add(v)
                    stack.append(v)
        comps.append(comp)
    return comps


def count_simple_cycles_exact(
    order: int,
    edges: Iterable[Edge],
    length: int,
    *,
    vertices: set[int] | None = None,
) -> int:
    """
    Exact simple-cycle count with O(order + edges + length) working memory.

    Canonicalization:
    - the smallest vertex of the cycle is the DFS root;
    - among the two orientations, only path[1] < path[-1] is counted.

    Thus every undirected simple cycle is counted exactly once.
    """
    if length < 3:
        return 0

    if vertices is None:
        allowed = set(range(order))
    else:
        allowed = set(vertices)

    if length > len(allowed):
        return 0

    adj_all = adjacency(order, edges)
    adj = [
        [v for v in adj_all[u] if v in allowed]
        if u in allowed
        else []
        for u in range(order)
    ]

    count = 0
    for start in sorted(allowed):
        path = [start]
        used = {start}

        def dfs(current: int, depth: int) -> None:
            nonlocal count
            if depth == length:
                if start in adj[current] and path[1] < path[-1]:
                    count += 1
                return

            remaining_after_pick = length - (depth + 1)
            for nxt in adj[current]:
                # Enforce start = minimum vertex in the cycle.
                if nxt <= start or nxt in used:
                    continue

                # Tiny safe feasibility prune: after choosing nxt there must be
                # enough unused allowed vertices to finish the path.
                if len(allowed) - len(used) - 1 < remaining_after_pick:
                    continue

                used.add(nxt)
                path.append(nxt)
                dfs(nxt, depth + 1)
                path.pop()
                used.remove(nxt)

        dfs(start, 1)

    return count


def graph_hash(order: int, edges: Iterable[Edge]) -> str:
    payload = {
        "order": order,
        "edges": [list(edge) for edge in sorted(edges)],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def add_base_K(builder: NamedBuilder, prefix: str) -> tuple[int, int]:
    """
    Literal transcription of Fig. 3.

    Semantic layout:

                 d
               / | \\
              /  b  \\
             /  / \\  \\
            g--?   ?--h      (crossings are not vertices)

        a---b---c plus arc a---c
        a-u, u-h
        c-v, v-g
        d-g, d-h
        g-h

    More explicitly, the 11 edges of K are below.

    u and v are exactly the two degree-2 vertices obtained by removing
    the Fig. 2 edge uv.
    """
    a = f"{prefix}a"
    b = f"{prefix}b"
    c = f"{prefix}c"
    d = f"{prefix}d"
    u = f"{prefix}u"
    v = f"{prefix}v"
    g = f"{prefix}g"
    h = f"{prefix}h"

    for x, y in (
        (a, b),
        (b, c),
        (a, c),
        (d, b),
        (d, g),
        (d, h),
        (a, u),
        (u, h),
        (c, v),
        (v, g),
        (g, h),
    ):
        builder.edge(x, y)

    return builder.vertex(u), builder.vertex(v)


def build_base_G_and_K() -> tuple[
    int, set[Edge], set[Edge], int, int, list[str]
]:
    """
    Build Fig. 2 graph G and Fig. 3 graph K=G-uv.
    """
    b = NamedBuilder.empty()
    u, v = add_base_K(b, "")
    k_edges = set(b.edges)
    g_edges = set(k_edges)
    g_edges.add(norm_edge(u, v))
    return len(b.names), g_edges, k_edges, u, v, list(b.names)


def add_z_gadget(builder: NamedBuilder, prefix: str) -> tuple[int, int, int, int]:
    """
    K_{1,3}+x exactly as required by the degree bookkeeping in the proof.

    z2 is the K1,3 center.
    z1 is the pendant leaf.
    z3-z4 is the added edge x between the other two leaves.

    Before external connections:
        deg(z1)=1, deg(z2)=3, deg(z3)=2, deg(z4)=2.

    After u-z1, u1-z3, x1-z4:
        z1 has degree 2 and z2,z3,z4 have degree 3,
    exactly as the paper states for X1.
    """
    z1 = f"{prefix}z1"
    z2 = f"{prefix}z2"
    z3 = f"{prefix}z3"
    z4 = f"{prefix}z4"
    for a, b in (
        (z2, z1),
        (z2, z3),
        (z2, z4),
        (z3, z4),
    ):
        builder.edge(a, b)
    return tuple(builder.vertex(z) for z in (z1, z2, z3, z4))  # type: ignore[return-value]


def add_X1(builder: NamedBuilder, prefix: str) -> tuple[set[int], int]:
    start = len(builder.names)

    u1, v1 = add_base_K(builder, f"{prefix}H1_")
    u2, v2 = add_base_K(builder, f"{prefix}H2_")
    x1, y1 = add_base_K(builder, f"{prefix}J1_")
    x2, y2 = add_base_K(builder, f"{prefix}J2_")
    u, v = add_base_K(builder, f"{prefix}H_")

    w1 = builder.vertex(f"{prefix}w1")
    w2 = builder.vertex(f"{prefix}w2")
    w3 = builder.vertex(f"{prefix}w3")
    builder.edge_ids(w1, w2)
    builder.edge_ids(w2, w3)
    builder.edge_ids(w3, w1)

    z1, _z2, z3, z4 = add_z_gadget(builder, prefix)

    # The eight edges listed in the proof of Theorem 2.1.
    for a, b in (
        (v1, u2),
        (y1, x2),
        (v2, w1),
        (y2, w2),
        (w3, v),
        (u, z1),
        (u1, z3),
        (x1, z4),
    ):
        builder.edge_ids(a, b)

    stop = len(builder.names)
    vertices = set(range(start, stop))
    if len(vertices) != 47:
        raise RuntimeError(f"X1 order mismatch: {len(vertices)} != 47")

    return vertices, z1


def build_G1() -> tuple[NamedBuilder, set[int], set[int], Edge]:
    b = NamedBuilder.empty()
    left, z1 = add_X1(b, "L_")
    right, z1p = add_X1(b, "R_")
    bridge = norm_edge(z1, z1p)
    b.edge_ids(*bridge)
    return b, left, right, bridge


def scoreworker_half(
    half_order: int,
    half_edges: Sequence[Edge],
    lengths: Sequence[int],
    *,
    witness_cap: int,
    node_budget: int,
) -> dict[int, dict[str, object]]:
    """
    Optional repository ScoreWorker cross-check.

    Each length is scored in a separate call to keep peak working state small.
    """
    from sglab.model import BitGraph  # type: ignore[import-untyped]
    from graphoratory.science.worker import ScoreWorker

    bit_graph = BitGraph.from_edges(half_order, tuple(half_edges))
    out: dict[int, dict[str, object]] = {}

    with ScoreWorker() as worker:
        for length in lengths:
            started = time.perf_counter()
            response = worker.score(
                bit_graph,
                lengths=(length,),
                witness_cap=witness_cap,
                node_budget=node_budget,
            )
            wall = time.perf_counter() - started

            if len(response.results) != 1:
                raise RuntimeError(
                    f"ScoreWorker returned {len(response.results)} results "
                    f"for C{length}"
                )
            result = response.results[0]
            out[length] = {
                "count": int(result.count),
                "complete": bool(result.complete),
                "nodes": int(result.nodes),
                "elapsed_ns": int(result.elapsed_ns),
                "wall_seconds": wall,
                "saturated": int(result.count) >= witness_cap,
            }

    return out


def relabel_subgraph(
    vertices: set[int],
    edges: Iterable[Edge],
) -> tuple[list[int], list[Edge]]:
    old = sorted(vertices)
    mapping = {v: i for i, v in enumerate(old)}
    sub_edges: list[Edge] = []
    for u, v in edges:
        if u in mapping and v in mapping:
            sub_edges.append(norm_edge(mapping[u], mapping[v]))
    return old, sorted(sub_edges)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--output",
        type=Path,
        default=Path("pirzada_g1_n94_verification.json"),
    )
    p.add_argument(
        "--score-worker",
        action="store_true",
        help=(
            "Cross-check the 47-vertex half with graphoratory ScoreWorker. "
            "The pure exact low-memory enumerator always runs."
        ),
    )
    p.add_argument("--witness-cap", type=int, default=1_000_000)
    p.add_argument("--node-budget", type=int, default=10_000_000)
    p.add_argument(
        "--fail-on-paper-claim-mismatch",
        action="store_true",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.witness_cap < 2:
        raise SystemExit("--witness-cap must be >= 2")
    if args.node_budget < 1:
        raise SystemExit("--node-budget must be >= 1")

    # ------------------------------------------------------------------
    # 1. Verify literal Fig. 2 / Fig. 3 transcription.
    # ------------------------------------------------------------------
    k_order, g_edges, k_edges, u, v, k_names = build_base_G_and_K()
    g_deg = degree_sequence(k_order, g_edges)
    k_deg = degree_sequence(k_order, k_edges)

    if k_order != 8 or len(g_edges) != 12 or set(g_deg) != {3}:
        raise RuntimeError(
            f"Fig.2 transcription is not cubic order 8: "
            f"m={len(g_edges)}, degrees={g_deg}"
        )
    if len(k_edges) != 11:
        raise RuntimeError(f"K edge count {len(k_edges)} != 11")

    degree2 = [i for i, d in enumerate(k_deg) if d == 2]
    if sorted(degree2) != sorted([u, v]):
        raise RuntimeError(
            f"K degree-2 vertices {degree2}, expected {[u, v]}"
        )

    g_cycles = {
        k: count_simple_cycles_exact(k_order, g_edges, k)
        for k in range(4, 9)
    }
    k_cycles = {
        k: count_simple_cycles_exact(k_order, k_edges, k)
        for k in (4, 8)
    }
    uv_distance = shortest_path_length(k_order, k_edges, u, v)

    # ------------------------------------------------------------------
    # 2. Build G1 exactly from the proof.
    # ------------------------------------------------------------------
    b, left, right, bridge = build_G1()
    order = len(b.names)
    edges = set(b.edges)
    degrees = degree_sequence(order, edges)

    if order != 94:
        raise RuntimeError(f"G1 order {order} != 94")
    if len(edges) != 141:
        raise RuntimeError(f"G1 edge count {len(edges)} != 141")
    if set(degrees) != {3}:
        raise RuntimeError(
            f"G1 is not cubic; degree histogram="
            f"{ {d: degrees.count(d) for d in sorted(set(degrees))} }"
        )
    if not is_connected(order, edges):
        raise RuntimeError("G1 is disconnected")

    comps = bridge_components(order, edges, bridge)
    comp_sizes = sorted(len(c) for c in comps)
    if comp_sizes != [47, 47]:
        raise RuntimeError(
            f"published bridge does not split 47+47: {comp_sizes}"
        )

    # ------------------------------------------------------------------
    # 3. Exact low-memory counting.
    #    The halves are isomorphic, so enumerate one 47-vertex X1 only.
    # ------------------------------------------------------------------
    old_left, left_edges = relabel_subgraph(left, edges)
    if old_left != list(range(47)):
        # Not required mathematically, but expected from builder order.
        raise RuntimeError("left X1 is unexpectedly not vertices 0..46")

    forbidden = (4, 8, 16, 32, 64)
    half_counts: dict[int, int] = {}
    exact_counts: dict[int, int] = {}
    timings: dict[int, float] = {}

    for length in (4, 8, 16, 32):
        started = time.perf_counter()
        count = count_simple_cycles_exact(47, left_edges, length)
        timings[length] = time.perf_counter() - started
        half_counts[length] = count
        exact_counts[length] = 2 * count

    # Certified by the bridge: every cycle is contained in a 47-vertex half.
    half_counts[64] = 0
    exact_counts[64] = 0
    timings[64] = 0.0

    total = sum(exact_counts.values())

    # ------------------------------------------------------------------
    # 4. Optional authoritative worker cross-check, half-size only.
    # ------------------------------------------------------------------
    worker_result: dict[int, dict[str, object]] | None = None
    if args.score_worker:
        worker_result = scoreworker_half(
            47,
            left_edges,
            (4, 8, 16, 32),
            witness_cap=args.witness_cap,
            node_budget=args.node_budget,
        )

        for length, pure_half_count in half_counts.items():
            if length == 64:
                continue
            result = worker_result[length]
            if not bool(result["complete"]):
                raise RuntimeError(
                    f"ScoreWorker C{length} incomplete after "
                    f"{result['nodes']} nodes; increase --node-budget"
                )
            if bool(result["saturated"]):
                raise RuntimeError(
                    f"ScoreWorker C{length} saturated at cap; "
                    f"increase --witness-cap"
                )
            if int(result["count"]) != pure_half_count:
                raise RuntimeError(
                    f"cycle counter disagreement C{length}: "
                    f"pure={pure_half_count}, worker={result['count']}"
                )

    # ------------------------------------------------------------------
    # 5. Compare literal reconstruction to claims printed in the paper.
    # ------------------------------------------------------------------
    # The paper says K has no C4/C8 and G1 has no C4/C8/C16.
    # It also calls the remaining power-of-two cycle unique.
    claim_checks = {
        "K_C4_is_zero": k_cycles[4] == 0,
        "K_C8_is_zero": k_cycles[8] == 0,
        "G1_C4_is_zero": exact_counts[4] == 0,
        "G1_C8_is_zero": exact_counts[8] == 0,
        "G1_C16_is_zero": exact_counts[16] == 0,
        "G1_exactly_one_power_of_two_cycle": total == 1,
    }
    all_claims_match = all(claim_checks.values())

    payload: dict[str, object] = {
        "schema_version": "heg.pirzada_g1_verification.v1",
        "source": {
            "paper": (
                "S. Pirzada, M. A. Shah, E. T. Baskoro, "
                "On 2-power unicyclic cubic graphs, EJGTA 10(1), 2022"
            ),
            "doi": "10.5614/ejgta.2022.10.1.24",
            "reconstruction_basis": [
                "Figure 2",
                "Figure 3",
                "Theorem 2.1 proof edge list",
                "Figure 4",
            ],
            "note": (
                "The proof text contains an apparent '84' typo; Figure 4, "
                "Table 1, and the 47+47 construction give order 94."
            ),
        },
        "base_graph": {
            "G_order": 8,
            "G_edges": [list(e) for e in sorted(g_edges)],
            "G_cycle_counts_4_to_8": {
                str(k): v for k, v in sorted(g_cycles.items())
            },
            "K_edges": [list(e) for e in sorted(k_edges)],
            "K_u": u,
            "K_v": v,
            "K_u_name": k_names[u],
            "K_v_name": k_names[v],
            "K_uv_distance": uv_distance,
            "K_cycle_counts": {
                str(k): v for k, v in sorted(k_cycles.items())
            },
        },
        "graph": {
            "order": order,
            "edge_count": len(edges),
            "edges": [list(e) for e in sorted(edges)],
            "vertex_names": list(b.names),
            "sha256": graph_hash(order, edges),
            "degree_sequence": list(degrees),
            "cubic": set(degrees) == {3},
            "bridge": list(bridge),
            "bridge_component_sizes": comp_sizes,
        },
        "exact_low_memory_counts": {
            "half_X1": {str(k): half_counts[k] for k in forbidden},
            "G1": {str(k): exact_counts[k] for k in forbidden},
            "total_power_of_two_cycles": total,
            "wall_seconds_by_half_length": {
                str(k): timings[k] for k in forbidden
            },
            "method": (
                "Enumerate one 47-vertex half exactly; double C4/C8/C16/C32; "
                "certify C64=0 from the 47+47 bridge decomposition."
            ),
        },
        "paper_claim_checks": claim_checks,
        "all_paper_claims_match_literal_reconstruction": all_claims_match,
        "score_worker_half_cross_check": worker_result,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print("Pirzada-Shah-Baskoro G1 literal verification")
    print(
        f"Fig.2 G: n=8 m=12 cubic=yes "
        f"cycles C4..C8={g_cycles}"
    )
    print(
        f"Fig.3 K=G-uv: m=11 deg(u)=deg(v)=2 "
        f"d_K(u,v)={uv_distance} C4={k_cycles[4]} C8={k_cycles[8]}"
    )
    print(
        f"G1: n={order} m={len(edges)} cubic=yes "
        f"bridge-components={comp_sizes}"
    )
    print(
        "EXACT "
        + " ".join(f"C{k}={exact_counts[k]}" for k in forbidden)
        + f" TOTAL={total}"
    )
    print(
        "half timings: "
        + ", ".join(
            f"C{k}={timings[k]:.3f}s" for k in (4, 8, 16, 32)
        )
    )

    if worker_result is not None:
        print("ScoreWorker half cross-check: MATCH")
        for length in (4, 8, 16, 32):
            r = worker_result[length]
            print(
                f"  C{length}: half={r['count']} "
                f"complete={r['complete']} nodes={r['nodes']} "
                f"wall={float(r['wall_seconds']):.3f}s"
            )

    if all_claims_match:
        print("PAPER CLAIM CHECK: MATCH")
    else:
        failed = [name for name, ok in claim_checks.items() if not ok]
        print("PAPER CLAIM CHECK: MISMATCH")
        for name in failed:
            print(f"  FAIL: {name}")

    print(f"saved: {args.output}")

    if args.fail_on_paper_claim_mismatch and not all_claims_match:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
