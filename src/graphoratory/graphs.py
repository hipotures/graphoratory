from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

import networkx as nx

from graphoratory.config import GraphConfig
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
    def graph_hash(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_payload())).hexdigest()

    def identity_payload(self) -> dict[str, Any]:
        return {"edges": [list(edge) for edge in self.edges], "order": self.order}

    def record(self) -> dict[str, Any]:
        return {"graph_hash": self.graph_hash, **self.identity_payload()}

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

    def validate_scientific_invariants(
        self,
        *,
        min_order: int | None = None,
        max_order: int | None = None,
    ) -> None:
        if min_order is not None and self.order < min_order:
            raise ValueError("generated graph order is below the configured minimum")
        if max_order is not None and self.order > max_order:
            raise ValueError("generated graph order is above the configured maximum")
        if not self.is_connected():
            raise ValueError("generated graph is disconnected")
        if min(self.degrees(), default=0) < 3:
            raise ValueError("generated graph has minimum degree below 3")


@dataclass(frozen=True, slots=True)
class GeneratedGraphs:
    graphs: tuple[Graph, ...]
    attempts: int
    rejected: int
    duplicates: int
    accepted_by_generator: tuple[tuple[str, int], ...]


class CandidateRejected(RuntimeError):
    """A sampled candidate could not satisfy its generator and common constraints."""


GeneratorFunction = Callable[[int, Random, GraphConfig], Graph]


def generate_graphs(
    config: GraphConfig,
) -> GeneratedGraphs:
    graphs: list[Graph] = []
    hashes: set[str] = set()
    attempts = 0
    rejected = 0
    duplicates = 0
    accepted = {name: 0 for name in GENERATORS}
    maximum_attempts = max(1_000, config.workspace_graph_count * 100)

    while len(graphs) < config.workspace_graph_count and attempts < maximum_attempts:
        rng = Random(_derived_seed(config.seed, attempts))
        attempts += 1
        order = rng.randint(config.min_order, config.max_order)
        generator_name = _select_generator(config, rng)
        try:
            graph = GENERATORS[generator_name](order, rng, config)
            graph.validate_scientific_invariants(
                min_order=config.min_order,
                max_order=config.max_order,
            )
        except (CandidateRejected, ValueError):
            rejected += 1
            continue
        if graph.graph_hash in hashes:
            duplicates += 1
            continue
        hashes.add(graph.graph_hash)
        graphs.append(graph)
        accepted[generator_name] += 1

    if len(graphs) != config.workspace_graph_count:
        raise RuntimeError(
            f"generated only {len(graphs)} distinct graphs after {attempts} attempts "
            f"({rejected} invalid candidates, {duplicates} duplicates)"
        )
    return GeneratedGraphs(
        graphs=tuple(graphs),
        attempts=attempts,
        rejected=rejected,
        duplicates=duplicates,
        accepted_by_generator=tuple(
            (name, accepted[name])
            for name in (
                config.mixed.generators
                if config.generator == "mixed"
                else (config.generator,)
            )
        ),
    )


def _generate_cycle_matching_stub_pairing(
    order: int,
    rng: Random,
    _config: GraphConfig,
) -> Graph:
    if order < 4:
        raise CandidateRejected("order must be at least 4")
    if order % 2:
        return _generate_stub_pairing(order, rng)
    return _generate_cycle_matching(order, rng)


def _generate_stub_pairing(order: int, rng: Random) -> Graph:
    if order < 5:
        raise CandidateRejected("stub-pairing graphs require order at least 5")
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
    raise CandidateRejected("failed to generate a stub-pairing graph within retry budget")


def _generate_cycle_matching(order: int, rng: Random) -> Graph:
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
    raise CandidateRejected("failed to generate a cycle-matching graph within retry budget")


def _generate_random_regular(order: int, rng: Random, config: GraphConfig) -> Graph:
    settings = config.random_regular
    feasible_degrees = [
        degree
        for degree in range(settings.degree_min, settings.degree_max + 1)
        if degree < order and (order * degree) % 2 == 0
    ]
    if not feasible_degrees:
        raise CandidateRejected("no feasible regular degree for sampled order")
    degree = rng.choice(feasible_degrees)
    try:
        generated = nx.random_regular_graph(degree, order, seed=rng)
    except nx.NetworkXError as exc:
        raise CandidateRejected("random regular construction failed") from exc
    return _from_networkx(generated)


def _generate_erdos_renyi_rejection(
    order: int,
    rng: Random,
    config: GraphConfig,
) -> Graph:
    settings = config.erdos_renyi_rejection
    maximum = min(settings.expected_degree_max, float(order - 1))
    if settings.expected_degree_min > maximum:
        raise CandidateRejected("no feasible expected degree for sampled order")
    expected_degree = rng.uniform(settings.expected_degree_min, maximum)
    probability = expected_degree / (order - 1)
    generated = nx.fast_gnp_random_graph(order, probability, seed=rng, directed=False)
    return _from_networkx(generated)


def _generate_degree_sequence_rejection(
    order: int,
    rng: Random,
    config: GraphConfig,
) -> Graph:
    settings = config.degree_sequence_rejection
    maximum = min(settings.degree_max, order - 1)
    if settings.degree_min > maximum:
        raise CandidateRejected("no feasible degree sequence for sampled order")
    sequence = [rng.randint(settings.degree_min, maximum) for _ in range(order)]
    if len(set(sequence)) < 2 or sum(sequence) % 2:
        raise CandidateRejected("sampled degree sequence is not heterogeneous with even sum")
    if not nx.is_graphical(sequence, method="eg"):
        raise CandidateRejected("sampled degree sequence is not graphical")
    generated = nx.havel_hakimi_graph(sequence)
    if not nx.is_connected(generated):
        raise CandidateRejected("degree-sequence realization is disconnected")
    swaps = max(1, generated.number_of_edges())
    try:
        nx.double_edge_swap(
            generated,
            nswap=swaps,
            max_tries=swaps * 20,
            seed=rng.getrandbits(64),
        )
    except nx.NetworkXAlgorithmError as exc:
        raise CandidateRejected("degree-sequence randomization failed") from exc
    return _from_networkx(generated)


GENERATORS: dict[str, GeneratorFunction] = {
    "cycle_matching_stub_pairing": _generate_cycle_matching_stub_pairing,
    "random_regular": _generate_random_regular,
    "erdos_renyi_rejection": _generate_erdos_renyi_rejection,
    "degree_sequence_rejection": _generate_degree_sequence_rejection,
}


def _select_generator(config: GraphConfig, rng: Random) -> str:
    if config.generator != "mixed":
        return config.generator
    return rng.choices(
        config.mixed.generators,
        weights=config.mixed.weights,
        k=1,
    )[0]


def _derived_seed(root_seed: int, attempt: int) -> int:
    digest = hashlib.sha256(f"{root_seed}:{attempt}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _from_networkx(graph: nx.Graph[Any]) -> Graph:
    return Graph.from_edges(
        graph.number_of_nodes(),
        ((int(u), int(v)) for u, v in graph.edges()),
    )


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
            if raw.get("graph_hash") != graph.graph_hash:
                raise ValueError(f"graph record {line_number} has an invalid hash")
            graph.validate_scientific_invariants()
            yield graph
