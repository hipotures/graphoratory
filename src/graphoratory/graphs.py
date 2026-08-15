from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from graphoratory.jsonio import canonical_json_bytes

Edge = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Graph:
    order: int
    edges: tuple[Edge, ...]

    def __post_init__(self) -> None:
        normalized = tuple(sorted((min(u, v), max(u, v)) for u, v in self.edges))
        if self.order < 1:
            raise ValueError("graph order must be positive")
        if normalized != self.edges:
            raise ValueError("edges must be normalized and sorted")
        if len(set(self.edges)) != len(self.edges):
            raise ValueError("duplicate edges are not allowed")
        if any(u == v or u < 0 or v >= self.order for u, v in self.edges):
            raise ValueError("edge is a loop or has an endpoint outside the graph")

    @classmethod
    def from_edges(cls, order: int, edges: Iterable[Edge]) -> Graph:
        return cls(order, tuple(sorted((min(u, v), max(u, v)) for u, v in edges)))

    @property
    def hash_full(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_payload())).hexdigest()

    def identity_payload(self) -> dict[str, Any]:
        return {"edges": [list(edge) for edge in self.edges], "order": self.order}

    def record(self) -> dict[str, Any]:
        return {"hash_full": self.hash_full, **self.identity_payload()}

    def degrees(self) -> tuple[int, ...]:
        values = [0] * self.order
        for u, v in self.edges:
            values[u] += 1
            values[v] += 1
        return tuple(values)

    def is_connected(self) -> bool:
        adjacency = [set[int]() for _ in range(self.order)]
        for u, v in self.edges:
            adjacency[u].add(v)
            adjacency[v].add(u)
        seen = {0}
        queue: deque[int] = deque([0])
        while queue:
            for neighbour in adjacency[queue.popleft()]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        return len(seen) == self.order

    def validate_scientific_invariants(self) -> None:
        if not self.is_connected():
            raise ValueError("generated graph is disconnected")
        if min(self.degrees(), default=0) < 3:
            raise ValueError("generated graph has minimum degree below 3")


@dataclass(frozen=True, slots=True)
class GeneratedCorpus:
    graphs: tuple[Graph, ...]
    attempts: int
    duplicates: int


def generate_corpus(
    *,
    count: int,
    min_order: int,
    max_order: int,
    seed: int,
) -> GeneratedCorpus:
    graphs: list[Graph] = []
    hashes: set[str] = set()
    attempts = 0
    duplicates = 0
    maximum_attempts = max(1_000, count * 100)
    order_count = max_order - min_order + 1

    while len(graphs) < count and attempts < maximum_attempts:
        order = min_order + (len(graphs) % order_count)
        derived_seed = _derived_seed(seed, attempts, order)
        attempts += 1
        try:
            graph = generate_seed_graph(order, Random(derived_seed))
        except RuntimeError:
            continue
        graph.validate_scientific_invariants()
        if graph.hash_full in hashes:
            duplicates += 1
            continue
        hashes.add(graph.hash_full)
        graphs.append(graph)

    if len(graphs) != count:
        raise RuntimeError(
            f"generated only {len(graphs)} distinct graphs after {attempts} attempts"
        )
    return GeneratedCorpus(tuple(graphs), attempts, duplicates)


def generate_seed_graph(order: int, rng: Random) -> Graph:
    if order < 4:
        raise ValueError("order must be at least 4")
    if order % 2:
        return _generate_mixed_degree(order, rng)
    return _generate_cubic(order, rng)


def _generate_mixed_degree(order: int, rng: Random) -> Graph:
    if order < 5:
        raise ValueError("mixed-degree graphs require order at least 5")
    high_count = min(1, max(1, math.floor(3 * order / 7)))
    degrees = [4] * high_count + [3] * (order - high_count)
    if sum(degrees) % 2:
        high_count += 1
        degrees = [4] * high_count + [3] * (order - high_count)
    high = set(range(high_count))

    for _ in range(2_000):
        stubs = [vertex for vertex, degree in enumerate(degrees) for _ in range(degree)]
        rng.shuffle(stubs)
        edges: set[Edge] = set()
        valid = True
        while stubs:
            u = stubs.pop()
            choices = [
                index
                for index, v in enumerate(stubs)
                if u != v and (min(u, v), max(u, v)) not in edges and not (u in high and v in high)
            ]
            if not choices:
                valid = False
                break
            v = stubs.pop(rng.choice(choices))
            edges.add((min(u, v), max(u, v)))
        if valid:
            graph = Graph.from_edges(order, edges)
            degrees_actual = graph.degrees()
            adjacency = _adjacency(graph)
            if graph.is_connected() and all(
                any(degrees_actual[neighbour] == 3 for neighbour in adjacency[vertex])
                for vertex in range(order)
            ):
                return graph
    raise RuntimeError("failed to generate a mixed-degree graph within retry budget")


def _generate_cubic(order: int, rng: Random) -> Graph:
    cycle = {(min(u, (u + 1) % order), max(u, (u + 1) % order)) for u in range(order)}
    for _ in range(200):
        vertices = list(range(order))
        rng.shuffle(vertices)
        matching: set[Edge] = set()
        while vertices:
            u = vertices.pop()
            choices = [
                index for index, v in enumerate(vertices) if (min(u, v), max(u, v)) not in cycle
            ]
            if not choices:
                break
            v = vertices.pop(rng.choice(choices))
            matching.add((min(u, v), max(u, v)))
        if not vertices and len(matching) == order // 2:
            return Graph.from_edges(order, cycle | matching)
    raise RuntimeError("failed to generate a cubic graph within retry budget")


def _derived_seed(root_seed: int, attempt: int, order: int) -> int:
    digest = hashlib.sha256(f"{root_seed}:{attempt}:{order}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _adjacency(graph: Graph) -> tuple[frozenset[int], ...]:
    values = [set[int]() for _ in range(graph.order)]
    for u, v in graph.edges:
        values[u].add(v)
        values[v].add(u)
    return tuple(frozenset(value) for value in values)


def write_graphs_jsonl_gz(path: Path, graphs: Iterable[Graph]) -> None:
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
    ):
        for graph in graphs:
            compressed.write(canonical_json_bytes(graph.record()))


def read_graphs_jsonl_gz(path: Path) -> Iterator[Graph]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"graph record {line_number} must be an object")
            graph = Graph.from_edges(
                int(raw["order"]),
                ((int(edge[0]), int(edge[1])) for edge in raw["edges"]),
            )
            if raw.get("hash_full") != graph.hash_full:
                raise ValueError(f"graph record {line_number} has an invalid hash")
            graph.validate_scientific_invariants()
            yield graph
