from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import SystemRandom, token_bytes
from typing import Any

from graphoratory.artifacts import (
    GRAPH_FILE,
    WorkspaceArtifact,
    discard_directory,
    ensure_workspace_alias,
    parse_utc_timestamp,
    publish_directory,
    temporary_directory,
    validate_workspace_name,
    workspace_artifact,
)
from graphoratory.config import AppConfig
from graphoratory.database.core import (
    database_path,
    index_graphs,
    index_line,
    index_workspace,
    make_engine,
    migrate,
    rebuild_database,
)
from graphoratory.database.queries import (
    WorkspaceRow,
    latest_line,
    line_graph_hashes,
    workspace_name_exists,
    workspace_projection,
)
from graphoratory.database.queries import (
    list_lines as query_lines,
)
from graphoratory.database.queries import (
    list_workspaces as query_workspaces,
)
from graphoratory.database.queries import (
    resolve_line as query_line,
)
from graphoratory.database.queries import (
    resolve_workspace as query_workspace,
)
from graphoratory.errors import ArtifactError, GraphoratoryError
from graphoratory.graphs import generate_graphs, write_graphs_jsonl_gz
from graphoratory.identifiers import Identifier, ObjectType
from graphoratory.jsonio import canonical_json_bytes, read_json, write_json_atomic


@dataclass(frozen=True, slots=True)
class GraphResult:
    workspace: Identifier
    graph_count: int
    attempts: int
    rejected: int
    duplicates: int
    accepted_by_generator: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    identifier: Identifier
    name: str | None
    created_at: str
    config_source: str
    generator: str | None
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
    workspace_name: str | None
    graph_count: int
    created_at: str
    phase: str
    database_state: str
    selected_latest: bool


@dataclass(frozen=True, slots=True)
class CommandLineSelection:
    identifier: Identifier
    line_path: Path
    workspace: WorkspaceArtifact
    selected_latest: bool
    indexed_created_at: str
    indexed_graph_count: int


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    identifier: Identifier
    name: str | None
    created_at: str
    active: bool


@dataclass(frozen=True, slots=True)
class LineSummary:
    identifier: Identifier
    created_at: datetime
    graph_count: int
    latest: bool


@dataclass(frozen=True, slots=True)
class LineListResult:
    workspace: Identifier
    workspace_name: str | None
    lines: tuple[LineSummary, ...]


def create_workspace(config: AppConfig, name: str) -> Identifier:
    validate_workspace_name(name)
    project_database = database_path(config.project_root)
    if not project_database.is_file():
        if config.workspace.root.exists():
            raise ArtifactError(
                "project index is missing or stale; run `graphlab workspace reindex`"
            )
        migrate(project_database)
    if workspace_name_exists(project_database, name):
        raise ArtifactError(f"duplicate workspace name: {name}")
    alias = config.workspace.root / name
    if alias.exists() or alias.is_symlink():
        raise ArtifactError(
            "project index is missing or stale; run `graphlab workspace reindex`"
        )
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
            "name": name,
            "workspace_hash": identifier.digest,
            "created_at": created_at,
            "creation_config": {
                "graphs": _graph_config_manifest(config),
            },
        }
        write_json_atomic(temporary / "manifest.json", manifest)
        publish_directory(temporary, final_path)
        ensure_workspace_alias(WorkspaceArtifact(identifier, name, created_at, final_path))
    except BaseException:
        discard_directory(temporary)
        raise
    try:
        engine = make_engine(project_database)
        try:
            with engine.begin() as connection:
                index_workspace(connection, manifest)
        finally:
            engine.dispose()
    except Exception as exc:
        raise ArtifactError(
            "workspace was published, but SQLite indexing failed; "
            "run `graphlab workspace reindex`"
        ) from exc
    return identifier


def generate_workspace_graphs(
    config: AppConfig, workspace_value: str | None = None
) -> GraphResult:
    workspace = _selected_workspace(config, workspace_value)
    workspace_path = workspace.path
    graphs_path = workspace_path / "graphs"
    manifest_path = graphs_path / "manifest.json"
    if manifest_path.exists():
        raise ArtifactError("workspace graphs already exist and cannot be overwritten")
    generated = generate_graphs(config.graphs)
    created_at = _timestamp()
    manifest = {
        "artifact_type": "graphs",
        "workspace_hash": workspace.identifier.digest,
        "created_at": created_at,
        "generation": _graph_config_manifest(config),
        "graph_hashes": [graph.graph_hash for graph in generated.graphs],
        "graph_count": len(generated.graphs),
        "attempted_candidates": generated.attempts,
        "rejected_invalid_candidates": generated.rejected,
        "duplicate_candidates": generated.duplicates,
        "accepted_distinct_graphs": len(generated.graphs),
        "accepted_by_generator": dict(generated.accepted_by_generator),
    }
    temporary = temporary_directory(graphs_path, "generation")
    try:
        write_graphs_jsonl_gz(temporary / GRAPH_FILE, generated.graphs)
        write_json_atomic(temporary / "manifest.json", manifest)
        (temporary / GRAPH_FILE).replace(graphs_path / GRAPH_FILE)
        (temporary / "manifest.json").replace(manifest_path)
    except BaseException:
        discard_directory(temporary)
        raise
    discard_directory(temporary)

    try:
        engine = make_engine(database_path(config.project_root))
        try:
            with engine.begin() as connection:
                index_graphs(connection, manifest, generated.graphs)
        finally:
            engine.dispose()
    except Exception as exc:
        raise ArtifactError(
            "graphs were published, but SQLite indexing failed; "
            "run `graphlab workspace reindex`"
        ) from exc
    return GraphResult(
        workspace=workspace.identifier,
        graph_count=len(generated.graphs),
        attempts=generated.attempts,
        rejected=generated.rejected,
        duplicates=generated.duplicates,
        accepted_by_generator=generated.accepted_by_generator,
    )


def create_line(
    config: AppConfig,
    workspace_value: str | None = None,
) -> Identifier:
    workspace = _selected_workspace(config, workspace_value)
    workspace_path = workspace.path
    graphs_manifest_path = workspace_path / "graphs" / "manifest.json"
    if not graphs_manifest_path.is_file():
        raise ArtifactError("workspace has no completed graphs; run graph generate first")
    graphs_manifest = read_json(graphs_manifest_path)
    graph_hashes = list(graphs_manifest["graph_hashes"])
    sample_size = config.graphs.line_graph_count
    if sample_size > len(graph_hashes):
        raise ArtifactError(
            f"graphs.line_graph_count={sample_size} exceeds graph count {len(graph_hashes)}"
        )
    selected = SystemRandom().sample(graph_hashes, sample_size)
    created_at = _timestamp()
    identity_payload = {
        "workspace_hash": workspace.identifier.digest,
        "created_at": created_at,
        "graph_hashes": selected,
    }
    identifier = Identifier.from_bytes(ObjectType.LINE, canonical_json_bytes(identity_payload))
    final_path = workspace_path / "lines" / identifier.display
    temporary = temporary_directory(workspace_path / "lines", "line")
    manifest = {"artifact_type": "line", "line_hash": identifier.digest, **identity_payload}
    try:
        write_json_atomic(temporary / "manifest.json", manifest)
        publish_directory(temporary, final_path)
    except BaseException:
        discard_directory(temporary)
        raise

    try:
        engine = make_engine(database_path(config.project_root))
        try:
            with engine.begin() as connection:
                index_line(connection, manifest)
        finally:
            engine.dispose()
    except Exception as exc:
        raise ArtifactError(
            f"line {identifier.display} was published, but SQLite indexing failed; "
            "run `graphlab workspace reindex`"
        ) from exc
    return identifier


def get_workspace_status(
    config: AppConfig, workspace_value: str | None = None
) -> WorkspaceStatus:
    workspace = _selected_workspace(config, workspace_value)
    workspace_path = workspace.path
    projection = workspace_projection(
        database_path(config.project_root),
        workspace.identifier.digest,
    )
    graphs_manifest_path = workspace_path / "graphs" / "manifest.json"
    graphs_manifest = read_json(graphs_manifest_path) if graphs_manifest_path.is_file() else None
    configuration = (
        json.loads(projection.configuration_json)
        if projection.configuration_json is not None
        else None
    )
    database_state = "indexed"
    if graphs_manifest is None:
        if projection.graph_count != 0 or projection.generator is not None:
            database_state = "needs reindex"
    elif (
        graphs_manifest.get("workspace_hash") != workspace.identifier.digest
        or graphs_manifest.get("graph_count") != projection.graph_count
        or graphs_manifest.get("generation", {}).get("generator")
        != projection.generator
    ):
        database_state = "needs reindex"
    return WorkspaceStatus(
        identifier=workspace.identifier,
        name=workspace.name,
        created_at=workspace.created_at,
        config_source=(
            f"$PROJECT/{config.source.relative_to(config.project_root).as_posix()}"
        ),
        generator=projection.generator,
        graph_count=projection.graph_count,
        min_order=int(configuration["min_order"]) if configuration else None,
        max_order=int(configuration["max_order"]) if configuration else None,
        line_count=projection.line_count,
        database_state=database_state,
        disk_bytes=_directory_size(workspace_path),
    )


def get_line_status(
    config: AppConfig,
    line_value: str | None = None,
    workspace_value: str | None = None,
) -> LineStatus:
    selection = resolve_line_for_command(config, line_value, workspace_value)
    workspace_path = selection.workspace.path
    manifest = read_json(selection.line_path / "manifest.json")
    graphs_manifest = read_json(workspace_path / "graphs" / "manifest.json")
    selected = list(manifest["graph_hashes"])
    if not set(selected).issubset(set(graphs_manifest["graph_hashes"])):
        raise ArtifactError("line manifest references graphs absent from its workspace")
    indexed_graph_hashes = line_graph_hashes(
        database_path(config.project_root),
        selection.identifier.digest,
    )
    database_state = "indexed"
    if (
        manifest.get("line_hash") != selection.identifier.digest
        or manifest.get("workspace_hash") != selection.workspace.identifier.digest
        or manifest.get("created_at") != selection.indexed_created_at
        or len(selected) != selection.indexed_graph_count
        or tuple(selected) != indexed_graph_hashes
    ):
        database_state = "needs reindex"
    return LineStatus(
        identifier=selection.identifier,
        workspace=selection.workspace.identifier,
        workspace_name=selection.workspace.name,
        graph_count=len(selected),
        created_at=str(manifest["created_at"]),
        phase="ready for policy generation",
        database_state=database_state,
        selected_latest=selection.selected_latest,
    )


def resolve_line_for_command(
    config: AppConfig,
    explicit_line: str | None,
    workspace_value: str | None = None,
) -> CommandLineSelection:
    if explicit_line is None:
        workspace = _selected_workspace(config, workspace_value)
        line = latest_line(
            database_path(config.project_root),
            workspace.identifier.digest,
        )
        if line is None:
            label = workspace.name or workspace.identifier.display
            raise ArtifactError(
                f"workspace {label} has no lines; create one with `graphlab line create`"
            )
        return CommandLineSelection(
            line.identifier,
            workspace.path / "lines" / line.identifier.display,
            workspace,
            selected_latest=True,
            indexed_created_at=line.created_at,
            indexed_graph_count=line.graph_count,
        )

    line = query_line(
        database_path(config.project_root),
        explicit_line,
    )
    workspace = _workspace_from_row(
        config,
        query_workspace(
            database_path(config.project_root),
            f"ws-{line.workspace.digest}",
        ),
    )
    workspace_context = (
        workspace_value if workspace_value is not None else config.workspace.active
    )
    if workspace_context is not None:
        selected_workspace = _selected_workspace(config, workspace_value)
        if workspace.identifier != selected_workspace.identifier:
            label = selected_workspace.name or selected_workspace.identifier.display
            raise ArtifactError(
                f"line {line.identifier.display} does not belong to workspace {label}"
            )
        workspace = selected_workspace
    return CommandLineSelection(
        line.identifier,
        workspace.path / "lines" / line.identifier.display,
        workspace,
        selected_latest=False,
        indexed_created_at=line.created_at,
        indexed_graph_count=line.graph_count,
    )


def reindex_workspace(config: AppConfig, workspace_value: str | None = None) -> Identifier:
    try:
        workspaces = rebuild_database(config.project_root, config.workspace.root)
    except GraphoratoryError:
        raise
    except Exception as exc:
        raise ArtifactError(f"project reindex failed: {exc}") from exc
    for scanned_workspace in workspaces:
        ensure_workspace_alias(scanned_workspace)
    workspace = _selected_workspace(config, workspace_value)
    return workspace.identifier


def list_workspaces(config: AppConfig) -> list[WorkspaceSummary]:
    active_hash: str | None = None
    if config.workspace.active is not None:
        with suppress(GraphoratoryError):
            active_hash = query_workspace(
                database_path(config.project_root), config.workspace.active
            ).identifier.digest
    return [
        WorkspaceSummary(
            identifier=row.identifier,
            name=row.name,
            created_at=row.created_at,
            active=row.identifier.digest == active_hash,
        )
        for row in query_workspaces(database_path(config.project_root))
    ]


def list_lines(
    config: AppConfig,
    workspace_value: str | None = None,
) -> LineListResult:
    workspace = _selected_workspace(config, workspace_value)
    lines = tuple(
        LineSummary(
            identifier=line.identifier,
            created_at=parse_utc_timestamp(line.created_at),
            graph_count=line.graph_count,
            latest=index == 0,
        )
        for index, line in enumerate(
            query_lines(
                database_path(config.project_root),
                workspace.identifier.digest,
            )
        )
    )
    return LineListResult(
        workspace=workspace.identifier,
        workspace_name=workspace.name,
        lines=lines,
    )


def _graph_config_manifest(config: AppConfig) -> dict[str, Any]:
    return {
        "generator": config.graphs.generator,
        "workspace_graph_count": config.graphs.workspace_graph_count,
        "line_graph_count": config.graphs.line_graph_count,
        "min_order": config.graphs.min_order,
        "max_order": config.graphs.max_order,
        "seed": config.graphs.seed,
        "order_distribution": "uniform_candidate_orders",
        "random_regular": {
            "degree_min": config.graphs.random_regular.degree_min,
            "degree_max": config.graphs.random_regular.degree_max,
        },
        "erdos_renyi_rejection": {
            "expected_degree_min": (
                config.graphs.erdos_renyi_rejection.expected_degree_min
            ),
            "expected_degree_max": (
                config.graphs.erdos_renyi_rejection.expected_degree_max
            ),
        },
        "degree_sequence_rejection": {
            "degree_min": config.graphs.degree_sequence_rejection.degree_min,
            "degree_max": config.graphs.degree_sequence_rejection.degree_max,
        },
        "mixed": {
            "generators": list(config.graphs.mixed.generators),
            "weights": list(config.graphs.mixed.weights),
        },
    }


def _selected_workspace(config: AppConfig, explicit: str | None) -> WorkspaceArtifact:
    value = explicit if explicit is not None else config.workspace.active
    if value is None:
        raise ArtifactError(
            "no workspace selected; set workspace.active in experiment.toml "
            "or pass workspace=<name-or-id>"
        )
    row = query_workspace(database_path(config.project_root), value)
    return _workspace_from_row(config, row)


def _workspace_from_row(config: AppConfig, row: WorkspaceRow) -> WorkspaceArtifact:
    path = config.workspace.root / row.identifier.display
    try:
        workspace = workspace_artifact(path)
    except GraphoratoryError as exc:
        raise ArtifactError(
            "project index is missing or stale; run `graphlab workspace reindex`"
        ) from exc
    if (
        workspace.identifier != row.identifier
        or workspace.name != row.name
        or workspace.created_at != row.created_at
    ):
        raise ArtifactError(
            "project index is missing or stale; run `graphlab workspace reindex`"
        )
    return workspace


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
