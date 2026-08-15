import gzip
from pathlib import Path

from graphoratory.graphs import (
    generate_corpus,
    read_graphs_jsonl_gz,
    write_graphs_jsonl_gz,
)


def test_generation_is_deterministic_and_structurally_valid(tmp_path: Path) -> None:
    first = generate_corpus(count=12, min_order=10, max_order=15, seed=401)
    second = generate_corpus(count=12, min_order=10, max_order=15, seed=401)
    assert [graph.hash_full for graph in first.graphs] == [
        graph.hash_full for graph in second.graphs
    ]
    assert len({graph.hash_full for graph in first.graphs}) == 12
    for graph in first.graphs:
        assert 10 <= graph.order <= 15
        assert graph.is_connected()
        assert min(graph.degrees()) >= 3
        assert len(graph.edges) == len(set(graph.edges))
        assert all(u < v for u, v in graph.edges)

    path = tmp_path / "graphs.jsonl.gz"
    write_graphs_jsonl_gz(path, first.graphs)
    assert path.read_bytes().startswith(b"\x1f\x8b")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        assert len(handle.readlines()) == 12
    assert tuple(read_graphs_jsonl_gz(path)) == first.graphs
