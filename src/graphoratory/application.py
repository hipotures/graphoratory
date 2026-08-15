from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import SystemRandom, token_bytes
from typing import Any

from graphoratory.artifacts import (
    DATABASE_NAME,
    GRAPH_FILE,
    corpus_directories,
    discard_directory,
    latest_corpus,
    publish_directory,
    resolve_corpus,
    resolve_line,
    resolve_workspace,
    temporary_directory,
)
from graphoratory.config import AppConfig
from graphoratory.database.core import (
    database_path,
    index_corpus,
    index_line,
    index_workspace,
    make_engine,
    migrate,
    projection_counts,
    rebuild_database,
)
from graphoratory.errors import ArtifactError
from graphoratory.graphs import generate_corpus, read_graphs_jsonl_gz, write_graphs_jsonl_gz
from graphoratory.identifiers import Identifier, ObjectType
from graphoratory.jsonio import canonical_json_bytes, read_json, write_json_atomic


@dataclass(frozen=True, slots=True)
class CorpusResult:
    identifier: Identifier
    graph_count: int
    attempts: int
    duplicates: int


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    identifier: Identifier
    created_at: str
    config_source: str
    corpus_count: int
    graph_count: int
    min_order: int | None
    max_order: int | None
    line_count: int
    database_state: str
    disk_bytes: int


@dataclass(frozen=True, slots=True)
class LineStatus:
    identifier: Identifier
    workspace: Identifier
    corpus: Identifier
    graph_count: int
    created_at: str
    phase: str
    database_state: str


def create_workspace(config: AppConfig) -> Identifier:
    config.workspace.root.mkdir(parents=True, exist_ok=True)
    created_at = _timestamp()
    identifier = Identifier.from_bytes(ObjectType.WORKSPACE, token_bytes(32))
    final_path = config.workspace.root / identifier.display
    if final_path.exists():
        raise ArtifactError(f"workspace path already exists: {final_path}")
    temporary = temporary_directory(config.workspace.root, "workspace")
    try:
        (temporary / "graphs").mkdir()
        (temporary / "lines").mkdir()
        manifest = {
            "artifact_type": "workspace",
            "hash_full": identifier.hash_full,
            "created_at": created_at,
            "config_source": str(config.source),
            "creation_config": {
                "workspace": {"root": str(config.workspace.root)},
                "graphs": _graph_config_manifest(config),
            },
        }
        write_json_atomic(temporary / "manifest.json", manifest)
        migrate(temporary / DATABASE_NAME)
        engine = make_engine(temporary / DATABASE_NAME)
        try:
            with engine.begin() as connection:
                index_workspace(connection, manifest, final_path)
        finally:
            engine.dispose()
        publish_directory(temporary, final_path)
    except BaseException:
        discard_directory(temporary)
        raise
    return identifier


def generate_graph_corpus(config: AppConfig, workspace_value: str) -> CorpusResult:
    workspace, workspace_path = resolve_workspace(config.workspace.root, workspace_value)
    generated = generate_corpus(
        count=config.graphs.count,
        min_order=config.graphs.min_order,
        max_order=config.graphs.max_order,
        seed=config.graphs.seed,
    )
    created_at = _timestamp()
    identity_payload = {
        "workspace_hash": workspace.hash_full,
        "created_at": created_at,
        "generation": _graph_config_manifest(config),
        "graph_hashes": [graph.hash_full for graph in generated.graphs],
    }
    identifier = Identifier.from_bytes(ObjectType.CORPUS, canonical_json_bytes(identity_payload))
    final_path = workspace_path / "graphs" / identifier.display
    temporary = temporary_directory(workspace_path / "graphs", "corpus")
    manifest = {
        "artifact_type": "graph_corpus",
        "hash_full": identifier.hash_full,
        **identity_payload,
        "graph_count": len(generated.graphs),
        "generation_attempts": generated.attempts,
        "duplicate_attempts": generated.duplicates,
    }
    try:
        write_graphs_jsonl_gz(temporary / GRAPH_FILE, generated.graphs)
        write_json_atomic(temporary / "manifest.json", manifest)
        publish_directory(temporary, final_path)
    except BaseException:
        discard_directory(temporary)
        raise

    try:
        engine = make_engine(database_path(workspace_path))
        try:
            with engine.begin() as connection:
                index_corpus(connection, manifest, final_path, generated.graphs)
        finally:
            engine.dispose()
    except Exception as exc:
        raise ArtifactError(
            f"corpus {identifier.display} was published, but SQLite indexing failed; "
            f"run workspace reindex workspace={workspace.display}"
        ) from exc
    return CorpusResult(identifier, len(generated.graphs), generated.attempts, generated.duplicates)


def create_line(
    config: AppConfig,
    workspace_value: str,
    corpus_value: str | None = None,
) -> Identifier:
    workspace, workspace_path = resolve_workspace(config.workspace.root, workspace_value)
    corpus_manifest, corpus_path = (
        resolve_corpus(workspace_path, corpus_value)
        if corpus_value is not None
        else latest_corpus(workspace_path)
    )
    graph_hashes = list(corpus_manifest["graph_hashes"])
    sample_size = config.graphs.line_sample_size
    if sample_size > len(graph_hashes):
        raise ArtifactError(
            f"graphs.line_sample_size={sample_size} exceeds corpus size {len(graph_hashes)}"
        )
    selected = SystemRandom().sample(graph_hashes, sample_size)
    created_at = _timestamp()
    identity_payload = {
        "workspace_hash": workspace.hash_full,
        "corpus_hash": corpus_manifest["hash_full"],
        "created_at": created_at,
        "graph_hashes": selected,
    }
    identifier = Identifier.from_bytes(ObjectType.LINE, canonical_json_bytes(identity_payload))
    final_path = workspace_path / "lines" / identifier.display
    temporary = temporary_directory(workspace_path / "lines", "line")
    manifest = {"artifact_type": "line", "hash_full": identifier.hash_full, **identity_payload}
    try:
        write_json_atomic(temporary / "manifest.json", manifest)
        publish_directory(temporary, final_path)
    except BaseException:
        discard_directory(temporary)
        raise

    try:
        engine = make_engine(database_path(workspace_path))
        try:
            with engine.begin() as connection:
                index_line(connection, manifest, final_path)
        finally:
            engine.dispose()
    except Exception as exc:
        raise ArtifactError(
            f"line {identifier.display} was published, but SQLite indexing failed; "
            f"run workspace reindex workspace={workspace.display}"
        ) from exc
    return identifier


def get_workspace_status(config: AppConfig, workspace_value: str) -> WorkspaceStatus:
    identifier, workspace_path = resolve_workspace(config.workspace.root, workspace_value)
    manifest = read_json(workspace_path / "manifest.json")
    corpus_manifests = [
        read_json(path / "manifest.json") for path in corpus_directories(workspace_path)
    ]
    line_manifests = _line_manifests(workspace_path)
    graph_count = sum(int(corpus["graph_count"]) for corpus in corpus_manifests)
    minimums = [int(corpus["generation"]["min_order"]) for corpus in corpus_manifests]
    maximums = [int(corpus["generation"]["max_order"]) for corpus in corpus_manifests]
    expected = {
        "workspaces": 1,
        "corpora": len(corpus_manifests),
        "graphs": graph_count,
        "lines": len(line_manifests),
        "line_graphs": sum(len(line["graph_hashes"]) for line in line_manifests),
    }
    actual = projection_counts(database_path(workspace_path))
    database_state = "indexed" if actual == expected else "needs reindex"
    return WorkspaceStatus(
        identifier=identifier,
        created_at=str(manifest["created_at"]),
        config_source=str(config.source),
        corpus_count=len(corpus_manifests),
        graph_count=graph_count,
        min_order=min(minimums) if minimums else None,
        max_order=max(maximums) if maximums else None,
        line_count=len(line_manifests),
        database_state=database_state,
        disk_bytes=_directory_size(workspace_path),
    )


def get_line_status(config: AppConfig, line_value: str) -> LineStatus:
    identifier, line_path, workspace_path = resolve_line(config.workspace.root, line_value)
    manifest = read_json(line_path / "manifest.json")
    workspace_manifest = read_json(workspace_path / "manifest.json")
    corpus_path = (
        workspace_path
        / "graphs"
        / Identifier(ObjectType.CORPUS, str(manifest["corpus_hash"])).display
    )
    corpus_manifest = read_json(corpus_path / "manifest.json")
    selected = list(manifest["graph_hashes"])
    if not set(selected).issubset(set(corpus_manifest["graph_hashes"])):
        raise ArtifactError("line manifest references graphs absent from its corpus")
    workspace = Identifier(ObjectType.WORKSPACE, str(workspace_manifest["hash_full"]))
    corpus = Identifier(ObjectType.CORPUS, str(corpus_manifest["hash_full"]))
    counts = projection_counts(database_path(workspace_path))
    database_state = (
        "indexed"
        if counts is not None and counts["lines"] >= 1 and counts["line_graphs"] >= len(selected)
        else "needs reindex"
    )
    return LineStatus(
        identifier=identifier,
        workspace=workspace,
        corpus=corpus,
        graph_count=len(selected),
        created_at=str(manifest["created_at"]),
        phase="ready for policy generation",
        database_state=database_state,
    )


def reindex_workspace(config: AppConfig, workspace_value: str) -> Identifier:
    identifier, workspace_path = resolve_workspace(config.workspace.root, workspace_value)
    rebuild_database(workspace_path)
    return identifier


def load_corpus_graph_hashes(corpus_path: Path) -> tuple[str, ...]:
    return tuple(graph.hash_full for graph in read_graphs_jsonl_gz(corpus_path / GRAPH_FILE))


def _graph_config_manifest(config: AppConfig) -> dict[str, Any]:
    return {
        "mode": config.graphs.mode,
        "count": config.graphs.count,
        "line_sample_size": config.graphs.line_sample_size,
        "min_order": config.graphs.min_order,
        "max_order": config.graphs.max_order,
        "seed": config.graphs.seed,
        "order_distribution": "round_robin",
    }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _line_manifests(workspace_path: Path) -> list[dict[str, Any]]:
    lines_path = workspace_path / "lines"
    if not lines_path.exists():
        return []
    return [
        read_json(path / "manifest.json")
        for path in sorted(lines_path.iterdir())
        if path.is_dir() and path.name.startswith("ln-") and (path / "manifest.json").is_file()
    ]


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
