import gzip
from dataclasses import replace
from pathlib import Path

import pytest

from graphoratory.config import CONCRETE_GENERATORS, AppConfig, GraphConfig
from graphoratory.graphs import (
    GENERATORS,
    Graph,
    generate_graphs,
    read_graphs_jsonl_gz,
    write_graphs_jsonl_gz,
)


@pytest.mark.parametrize("generator", (*CONCRETE_GENERATORS, "mixed"))
def test_generators_are_deterministic_and_structurally_valid(
    app_config: AppConfig,
    generator: str,
) -> None:
    config = _graph_config(app_config, generator=generator, count=12)
    first = generate_graphs(config)
    second = generate_graphs(config)

    assert [graph.graph_hash for graph in first.graphs] == [
        graph.graph_hash for graph in second.graphs
    ]
    assert len({graph.graph_hash for graph in first.graphs}) == 12
    assert sum(dict(first.accepted_by_generator).values()) == 12
    for graph in first.graphs:
        assert config.min_order <= graph.order <= config.max_order
        assert graph.is_connected()
        assert min(graph.degrees()) >= 3
        assert len(graph.edges) == len(set(graph.edges))
        assert all(0 <= u < v < graph.order for u, v in graph.edges)


@pytest.mark.parametrize("generator", (*CONCRETE_GENERATORS, "mixed"))
def test_different_seeds_normally_change_the_corpus(
    app_config: AppConfig,
    generator: str,
) -> None:
    first = generate_graphs(_graph_config(app_config, generator=generator, seed=401))
    second = generate_graphs(_graph_config(app_config, generator=generator, seed=402))
    assert [graph.graph_hash for graph in first.graphs] != [
        graph.graph_hash for graph in second.graphs
    ]


def test_random_regular_graphs_are_regular_within_degree_bounds(
    app_config: AppConfig,
) -> None:
    config = _graph_config(app_config, generator="random_regular", count=16)
    generated = generate_graphs(config)
    for graph in generated.graphs:
        degrees = graph.degrees()
        assert len(set(degrees)) == 1
        assert config.random_regular.degree_min <= degrees[0]
        assert degrees[0] <= config.random_regular.degree_max


def test_random_regular_resamples_infeasible_order_degree_pairs(
    app_config: AppConfig,
) -> None:
    config = replace(
        _graph_config(app_config, generator="random_regular", count=4),
        min_order=5,
        max_order=6,
        random_regular=replace(
            app_config.graphs.random_regular,
            degree_min=3,
            degree_max=3,
        ),
    )
    generated = generate_graphs(config)
    assert generated.rejected > 0
    assert {graph.order for graph in generated.graphs} == {6}


def test_erdos_renyi_rejects_invalid_candidates(app_config: AppConfig) -> None:
    config = _graph_config(app_config, generator="erdos_renyi_rejection", count=20)
    generated = generate_graphs(config)
    assert generated.rejected > 0
    assert len(generated.graphs) == 20


def test_degree_sequence_graphs_have_heterogeneous_degrees(
    app_config: AppConfig,
) -> None:
    config = _graph_config(app_config, generator="degree_sequence_rejection", count=16)
    generated = generate_graphs(config)
    assert all(len(set(graph.degrees())) >= 2 for graph in generated.graphs)


def test_mixed_generator_is_deterministic_and_uses_every_component(
    app_config: AppConfig,
) -> None:
    config = _graph_config(app_config, generator="mixed", count=48)
    generated = generate_graphs(config)
    assert set(dict(generated.accepted_by_generator)) == set(CONCRETE_GENERATORS)
    assert sum(dict(generated.accepted_by_generator).values()) == 48


def test_mixed_provenance_includes_zero_contribution_components(
    app_config: AppConfig,
) -> None:
    config = replace(
        _graph_config(app_config, generator="mixed", count=1),
        mixed=replace(
            app_config.graphs.mixed,
            generators=("random_regular", "erdos_renyi_rejection"),
            weights=(1e100, 1e-100),
        ),
    )
    generated = generate_graphs(config)
    assert dict(generated.accepted_by_generator) == {
        "random_regular": 1,
        "erdos_renyi_rejection": 0,
    }


def test_registry_contains_every_concrete_generator() -> None:
    assert set(GENERATORS) == set(CONCRETE_GENERATORS)


def test_duplicate_candidates_do_not_count_toward_corpus_size(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prism = Graph.from_edges(
        6,
        (
            (0, 1),
            (0, 2),
            (0, 3),
            (1, 2),
            (1, 4),
            (2, 5),
            (3, 4),
            (3, 5),
            (4, 5),
        ),
    )
    bipartite = Graph.from_edges(
        6,
        ((u, v) for u in range(3) for v in range(3, 6)),
    )
    candidates = iter((prism, prism, bipartite))

    def controlled_generator(
        _order: int,
        _rng: object,
        _config: GraphConfig,
    ) -> Graph:
        return next(candidates)

    monkeypatch.setitem(GENERATORS, "random_regular", controlled_generator)
    config = replace(
        _graph_config(app_config, generator="random_regular", count=2),
        min_order=6,
        max_order=6,
    )
    generated = generate_graphs(config)
    assert generated.graphs == (prism, bipartite)
    assert generated.attempts == 3
    assert generated.duplicates == 1


def test_graph_jsonl_round_trip(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    generated = generate_graphs(
        _graph_config(app_config, generator="cycle_matching_stub_pairing")
    )
    path = tmp_path / "graphs.jsonl.gz"
    write_graphs_jsonl_gz(path, generated.graphs)

    assert path.read_bytes().startswith(b"\x1f\x8b")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert len(handle.readlines()) == len(generated.graphs)
    assert tuple(read_graphs_jsonl_gz(path)) == generated.graphs


def _graph_config(
    app_config: AppConfig,
    *,
    generator: str,
    count: int = 8,
    seed: int = 401,
) -> GraphConfig:
    return replace(
        app_config.graphs,
        generator=generator,
        workspace_graph_count=count,
        min_order=10,
        max_order=15,
        seed=seed,
    )
