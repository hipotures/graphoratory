from __future__ import annotations

import hashlib
import json
import resource
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import SystemRandom, token_bytes
from typing import Any

from graphoratory.artifacts import (
    DATABASE_NAME,
    EVALUATIONS_DIRECTORY,
    GRAPH_FILE,
    WorkspaceArtifact,
    discard_directory,
    ensure_workspace_alias,
    evaluation_manifest_hash,
    parse_utc_timestamp,
    publish_directory,
    resolve_workspace,
    temporary_directory,
    validate_workspace_name,
    workspace_artifacts,
)
from graphoratory.config import AppConfig
from graphoratory.database.core import (
    database_path,
    index_evaluation,
    index_graphs,
    index_line,
    index_workspace,
    make_engine,
    migrate,
    rebuild_database,
)
from graphoratory.database.queries import (
    latest_line,
    line_graph_hashes,
    workspace_projection,
)
from graphoratory.database.queries import list_lines as query_lines
from graphoratory.database.queries import resolve_line as query_line
from graphoratory.errors import ArtifactError, GraphoratoryError
from graphoratory.graphs import (
    Graph,
    generate_graphs,
    read_graphs_jsonl_gz,
    write_graphs_jsonl_gz,
)
from graphoratory.identifiers import Identifier, ObjectType
from graphoratory.jsonio import canonical_json_bytes, read_json, write_json_atomic
from graphoratory.science.baseline import (
    SUPPORTED_BASELINES,
    baseline_for_selector,
)
from graphoratory.science.evaluator import IndependentEvaluator, Policy, score_payload


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


@dataclass(frozen=True, slots=True)
class BaselineEvaluationResult:
    evaluation_hash: str
    baseline_selector: str
    baseline: str
    line: Identifier
    workspace: Identifier
    workspace_name: str | None
    graph_count: int
    score: dict[str, object]
    diagnostics: dict[str, object]
    wall_seconds: float
    graphs_per_second: float
    peak_rss_bytes: int
    database_state: str
    selected_latest: bool


def create_workspace(config: AppConfig, name: str) -> Identifier:
    validate_workspace_name(name)
    config.workspace.root.mkdir(parents=True, exist_ok=True)
    if any(workspace.name == name for workspace in workspace_artifacts(config.workspace.root)):
        raise ArtifactError(f"duplicate workspace name: {name}")

    created_at = _timestamp()
    identifier = Identifier.from_bytes(ObjectType.WORKSPACE, token_bytes(32))
    final_path = config.workspace.root / identifier.display
    if final_path.exists():
        raise ArtifactError(f"workspace path already exists: {final_path}")

    temporary = temporary_directory(config.workspace.root, "workspace")
    published = False
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
        database = temporary / DATABASE_NAME
        migrate(database)
        engine = make_engine(database)
        try:
            with engine.begin() as connection:
                index_workspace(connection, manifest)
        finally:
            engine.dispose()
        publish_directory(temporary, final_path)
        published = True
        ensure_workspace_alias(WorkspaceArtifact(identifier, name, created_at, final_path))
    except BaseException:
        if published:
            discard_directory(final_path)
        discard_directory(temporary)
        raise
    return identifier


def generate_workspace_graphs(
    config: AppConfig, workspace_value: str | None = None
) -> GraphResult:
    workspace = _selected_workspace(config, workspace_value)
    database = _workspace_database(workspace)
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
        engine = make_engine(database)
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
    database = _workspace_database(workspace)
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
        engine = make_engine(database)
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
    database = _workspace_database(workspace)
    workspace_path = workspace.path
    projection = workspace_projection(database, workspace.identifier.digest)
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
    database = _workspace_database(selection.workspace)
    manifest = read_json(selection.line_path / "manifest.json")
    graphs_manifest = read_json(workspace_path / "graphs" / "manifest.json")
    selected = list(manifest["graph_hashes"])
    if not set(selected).issubset(set(graphs_manifest["graph_hashes"])):
        raise ArtifactError("line manifest references graphs absent from its workspace")
    indexed_graph_hashes = line_graph_hashes(database, selection.identifier.digest)
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
    workspace = _selected_workspace(config, workspace_value)
    database = _workspace_database(workspace)

    if explicit_line is None:
        line = latest_line(database, workspace.identifier.digest)
        if line is None:
            label = workspace.name or workspace.identifier.display
            raise ArtifactError(
                f"workspace {label} has no lines; create one with `graphlab line create`"
            )
        selected_latest = True
    else:
        line = query_line(database, explicit_line)
        selected_latest = False

    if line.workspace != workspace.identifier:
        raise ArtifactError(
            "workspace index is missing or stale; run `graphlab workspace reindex`"
        )
    return CommandLineSelection(
        line.identifier,
        workspace.path / "lines" / line.identifier.display,
        workspace,
        selected_latest=selected_latest,
        indexed_created_at=line.created_at,
        indexed_graph_count=line.graph_count,
    )


def reindex_workspace(config: AppConfig, workspace_value: str | None = None) -> Identifier:
    workspace = _selected_workspace_for_reindex(config, workspace_value)
    try:
        rebuild_database(workspace)
    except GraphoratoryError:
        raise
    except Exception as exc:
        raise ArtifactError(f"workspace reindex failed: {exc}") from exc
    ensure_workspace_alias(workspace)
    return workspace.identifier


def list_workspaces(config: AppConfig) -> list[WorkspaceSummary]:
    active_hash: str | None = None
    if config.workspace.active is not None:
        with suppress(GraphoratoryError):
            active_hash = resolve_workspace(
                config.workspace.root, config.workspace.active
            ).identifier.digest
    return [
        WorkspaceSummary(
            identifier=workspace.identifier,
            name=workspace.name,
            created_at=workspace.created_at,
            active=workspace.identifier.digest == active_hash,
        )
        for workspace in workspace_artifacts(config.workspace.root)
    ]


def list_lines(
    config: AppConfig,
    workspace_value: str | None = None,
) -> LineListResult:
    workspace = _selected_workspace(config, workspace_value)
    database = _workspace_database(workspace)
    lines = tuple(
        LineSummary(
            identifier=line.identifier,
            created_at=parse_utc_timestamp(line.created_at),
            graph_count=line.graph_count,
            latest=index == 0,
        )
        for index, line in enumerate(
            query_lines(database, workspace.identifier.digest)
        )
    )
    return LineListResult(
        workspace=workspace.identifier,
        workspace_name=workspace.name,
        lines=lines,
    )


def evaluate_baseline(
    config: AppConfig,
    line_value: str | None = None,
    workspace_value: str | None = None,
    baseline_selector: str = "random",
) -> BaselineEvaluationResult:
    baseline = baseline_for_selector(baseline_selector)
    selection, database, graphs = _baseline_evaluation_input(
        config,
        line_value,
        workspace_value,
    )
    return _evaluate_loaded_baseline(selection, database, graphs, baseline)


def evaluate_baselines(
    config: AppConfig,
    line_value: str | None = None,
    workspace_value: str | None = None,
) -> tuple[BaselineEvaluationResult, ...]:
    selection, database, graphs = _baseline_evaluation_input(
        config,
        line_value,
        workspace_value,
    )
    return tuple(
        _evaluate_loaded_baseline(
            selection,
            database,
            graphs,
            baseline_for_selector(selector),
        )
        for selector in SUPPORTED_BASELINES
    )


def _baseline_evaluation_input(
    config: AppConfig,
    line_value: str | None,
    workspace_value: str | None,
) -> tuple[CommandLineSelection, Path, tuple[Graph, ...]]:
    selection = resolve_line_for_command(config, line_value, workspace_value)
    database = _workspace_database(selection.workspace)
    graph_hashes = line_graph_hashes(database, selection.identifier.digest)
    if len(graph_hashes) != selection.indexed_graph_count:
        raise ArtifactError("workspace index is missing or stale; run `graphlab workspace reindex`")
    line_manifest_path = selection.line_path / "manifest.json"
    try:
        line_manifest = read_json(line_manifest_path)
        artifact_graph_hashes = line_manifest["graph_hashes"]
        if not isinstance(artifact_graph_hashes, list) or any(
            not isinstance(graph_hash, str) for graph_hash in artifact_graph_hashes
        ):
            raise TypeError("graph_hashes must be a list of strings")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactError(f"invalid line artifact: {line_manifest_path}") from exc
    if (
        line_manifest.get("line_hash") != selection.identifier.digest
        or line_manifest.get("workspace_hash") != selection.workspace.identifier.digest
        or tuple(artifact_graph_hashes) != graph_hashes
    ):
        raise ArtifactError(
            "line artifact and workspace index disagree; run `graphlab workspace reindex`"
        )
    graphs = _line_graphs(selection.workspace, graph_hashes)
    return selection, database, graphs


def _evaluate_loaded_baseline(
    selection: CommandLineSelection,
    database: Path,
    graphs: tuple[Graph, ...],
    baseline: Policy,
) -> BaselineEvaluationResult:
    started = time.perf_counter()
    result = IndependentEvaluator().evaluate(graphs, baseline)
    wall_seconds = time.perf_counter() - started
    created_at = _timestamp()
    manifest: dict[str, Any] = {
        "artifact_type": "baseline_evaluation",
        "workspace_hash": selection.workspace.identifier.digest,
        "line_hash": selection.identifier.digest,
        "created_at": created_at,
        "baseline": {
            **baseline.provenance(),
            "graphoratory_source_sha256": _source_sha256(
                Path(__file__).parent / "science" / "baseline.py"
            ),
        },
        "evaluator": {
            "forbidden_lengths": "powers_of_two_from_4_through_graph_order",
            "component_weight": "max(1, 64 // forbidden_length)",
            "energy": "mixed_radix_total_witnesses_weighted_penalty_edge_count",
            "strict_improvement": "candidate_upper_below_incumbent_lower",
            "aggregation": "best_so_far_episode_auc_order_balanced_mean",
            "graphoratory_source_sha256": _source_sha256(
                Path(__file__).parent / "science" / "evaluator.py"
            ),
            "worker": result.worker,
        },
        "score": score_payload(result.score),
        "diagnostics": result.diagnostics.payload(),
        "graph_count": len(graphs),
        "resources": {
            "wall_seconds": wall_seconds,
            "graphs_per_second": len(graphs) / wall_seconds if wall_seconds else 0.0,
            "peak_rss_bytes": _peak_rss_bytes(),
        },
    }
    manifest["evaluation_hash"] = evaluation_manifest_hash(manifest)
    evaluation_hash = str(manifest["evaluation_hash"])
    directory = selection.line_path / EVALUATIONS_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / f"{evaluation_hash}.json"
    if artifact_path.exists():
        raise ArtifactError(f"evaluation artifact already exists: {artifact_path}")
    write_json_atomic(artifact_path, manifest)
    try:
        engine = make_engine(database)
        try:
            with engine.begin() as connection:
                index_evaluation(connection, manifest)
        finally:
            engine.dispose()
    except Exception as exc:
        raise ArtifactError(
            f"evaluation {evaluation_hash} was published, but SQLite indexing failed; "
            f"run `graphlab workspace reindex {selection.workspace.identifier.display}`"
        ) from exc
    resources = manifest["resources"]
    assert isinstance(resources, dict)
    return BaselineEvaluationResult(
        evaluation_hash=evaluation_hash,
        baseline_selector=str(baseline.provenance()["selector"]),
        baseline=baseline.name,
        line=selection.identifier,
        workspace=selection.workspace.identifier,
        workspace_name=selection.workspace.name,
        graph_count=len(graphs),
        score=manifest["score"],
        diagnostics=manifest["diagnostics"],
        wall_seconds=wall_seconds,
        graphs_per_second=float(resources["graphs_per_second"]),
        peak_rss_bytes=int(resources["peak_rss_bytes"]),
        database_state="indexed",
        selected_latest=selection.selected_latest,
    )


def _line_graphs(
    workspace: WorkspaceArtifact,
    selected_hashes: tuple[str, ...],
) -> tuple[Graph, ...]:
    graphs_path = workspace.path / "graphs"
    manifest_path = graphs_path / "manifest.json"
    graph_path = graphs_path / GRAPH_FILE
    if not manifest_path.is_file() or not graph_path.is_file():
        raise ArtifactError(
            "workspace graph artifact is missing or incomplete; run `graphlab workspace reindex`"
        )
    manifest = read_json(manifest_path)
    if manifest.get("workspace_hash") != workspace.identifier.digest:
        raise ArtifactError("workspace graph artifact belongs to another workspace")
    try:
        workspace_graphs = tuple(read_graphs_jsonl_gz(graph_path))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactError(f"invalid workspace graph artifact: {graph_path}") from exc
    file_hashes = tuple(graph.graph_hash for graph in workspace_graphs)
    if list(file_hashes) != manifest.get("graph_hashes"):
        raise ArtifactError(
            "workspace graph artifact and manifest disagree; run `graphlab workspace reindex`"
        )
    by_hash = {graph.graph_hash: graph for graph in workspace_graphs}
    if len(by_hash) != len(workspace_graphs):
        raise ArtifactError("workspace graph artifact contains duplicate graph hashes")
    missing = [graph_hash for graph_hash in selected_hashes if graph_hash not in by_hash]
    if missing:
        raise ArtifactError(
            "line graph membership is absent from the workspace graph artifact; "
            "run `graphlab workspace reindex`"
        )
    return tuple(by_hash[graph_hash] for graph_hash in selected_hashes)


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
    return resolve_workspace(config.workspace.root, value)


def _selected_workspace_for_reindex(
    config: AppConfig, explicit: str | None
) -> WorkspaceArtifact:
    value = explicit if explicit is not None else config.workspace.active
    if value is None:
        raise ArtifactError(
            "no workspace selected; set workspace.active in experiment.toml "
            "or pass workspace=<name-or-id>"
        )
    try:
        return resolve_workspace(config.workspace.root, value)
    except GraphoratoryError:
        if value.startswith("ws-"):
            raise
        matches = [
            workspace
            for workspace in workspace_artifacts(config.workspace.root)
            if workspace.name == value
        ]
        if len(matches) == 1:
            return matches[0]
        raise


def _workspace_database(workspace: WorkspaceArtifact) -> Path:
    database = database_path(workspace.path)
    if not database.is_file():
        raise ArtifactError(
            "workspace index is missing or stale; run `graphlab workspace reindex`"
        )
    return database


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _peak_rss_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
