import shutil
import sqlite3
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import graphoratory.application as application
from graphoratory.application import (
    create_line,
    create_workspace,
    evaluate_baseline,
    generate_workspace_graphs,
    get_line_status,
    get_workspace_status,
    list_lines,
    list_workspaces,
    reindex_workspace,
    resolve_line_for_command,
)
from graphoratory.artifacts import DATABASE_NAME
from graphoratory.config import AppConfig, load_config
from graphoratory.database.core import (
    database_path,
    delete_database,
    index_line,
    make_engine,
    projection_counts,
)
from graphoratory.errors import ArtifactError, IdentifierError
from graphoratory.identifiers import Identifier, ObjectType
from graphoratory.jsonio import canonical_json_bytes, read_json, write_json_atomic
from graphoratory.science.evaluator import (
    EvaluationDiagnostics,
    EvaluationResult,
    RationalInterval,
)


def _workspace_path(config: AppConfig, workspace: Identifier) -> Path:
    return config.workspace.root / workspace.display


def _workspace_database(config: AppConfig, workspace: Identifier) -> Path:
    return database_path(_workspace_path(config, workspace))


def test_workspace_has_exactly_one_local_rebuildable_database(
    app_config: AppConfig,
) -> None:
    workspace = create_workspace(app_config, "testowy")
    workspace_path = _workspace_path(app_config, workspace)
    database = _workspace_database(app_config, workspace)

    assert database == workspace_path / DATABASE_NAME
    assert database.is_file()
    assert not (app_config.project_root / DATABASE_NAME).exists()
    assert {item.name for item in workspace_path.iterdir()} == {
        "manifest.json",
        DATABASE_NAME,
        "graphs",
        "lines",
    }
    assert (app_config.workspace.root / "testowy").is_symlink()

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT workspace_hash FROM workspaces")
            ).scalar_one() == workspace.digest
            assert connection.execute(text("SELECT COUNT(*) FROM workspaces")).scalar_one() == 1
    finally:
        engine.dispose()


def test_multiple_workspaces_have_isolated_indexes(app_config: AppConfig) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    line_a = create_line(app_config, workspace_a.display)
    line_b = create_line(app_config, workspace_b.display)

    database_a = _workspace_database(app_config, workspace_a)
    database_b = _workspace_database(app_config, workspace_b)
    expected = {
        "workspaces": 1,
        "graph_corpora": 1,
        "graphs": app_config.graphs.workspace_graph_count,
        "lines": 1,
        "line_graphs": app_config.graphs.line_graph_count,
        "evaluations": 0,
    }
    assert database_a != database_b
    assert projection_counts(database_a) == expected
    assert projection_counts(database_b) == expected
    assert not (app_config.project_root / DATABASE_NAME).exists()

    engine_a = create_engine(f"sqlite:///{database_a}")
    engine_b = create_engine(f"sqlite:///{database_b}")
    try:
        with engine_a.connect() as connection:
            indexed_line_a = connection.execute(
                text("SELECT line_hash FROM lines")
            ).scalar_one()
            assert indexed_line_a == line_a.digest
        with engine_b.connect() as connection:
            indexed_line_b = connection.execute(
                text("SELECT line_hash FROM lines")
            ).scalar_one()
            assert indexed_line_b == line_b.digest
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_line_list_and_resolution_use_workspace_sqlite_not_line_scan(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "indexed")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    lines_path = _workspace_path(app_config, workspace) / "lines"
    original_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == lines_path:
            raise AssertionError("ordinary line lookup enumerated lines/")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    listed = list_lines(app_config, workspace.display)
    selected = resolve_line_for_command(app_config, line.display, workspace.display)
    latest = resolve_line_for_command(app_config, None, workspace.display)

    assert [item.identifier for item in listed.lines] == [line]
    assert selected.identifier == line
    assert latest.identifier == line


def test_explicit_line_status_reads_only_requested_line_manifest(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "specific")
    generate_workspace_graphs(app_config, workspace.display)
    requested = create_line(app_config, workspace.display)
    other = create_line(app_config, workspace.display)
    workspace_path = _workspace_path(app_config, workspace)
    requested_manifest = workspace_path / "lines" / requested.display / "manifest.json"
    other_manifest = workspace_path / "lines" / other.display / "manifest.json"
    opened: list[Path] = []
    original_read_json = application.read_json

    def record_read(path: Path) -> dict[str, object]:
        opened.append(path)
        return original_read_json(path)

    monkeypatch.setattr(application, "read_json", record_read)
    status = get_line_status(app_config, requested.display, workspace.display)

    assert status.identifier == requested
    assert requested_manifest in opened
    assert other_manifest not in opened


def test_latest_line_uses_sql_timestamp_and_hash_ordering(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "latest")
    generate_workspace_graphs(app_config, workspace.display)
    graph_hashes = tuple(
        read_json(_workspace_path(app_config, workspace) / "graphs" / "manifest.json")[
            "graph_hashes"
        ][:2]
    )
    older = _publish_indexed_line(
        app_config,
        workspace,
        "2026-08-15T19:10:00.000000Z",
        graph_hashes[:1],
    )
    tied_a = _publish_indexed_line(
        app_config,
        workspace,
        "2026-08-15T20:45:03.100000Z",
        graph_hashes[:1],
    )
    tied_b = _publish_indexed_line(
        app_config,
        workspace,
        "2026-08-15T20:45:03.100000Z",
        graph_hashes[1:],
    )

    listed = list_lines(app_config, workspace.display)
    selected = resolve_line_for_command(app_config, None, workspace.display)
    expected_latest = max((tied_a, tied_b), key=lambda item: item.digest)

    assert [line.identifier for line in listed.lines] == [
        expected_latest,
        min((tied_a, tied_b), key=lambda item: item.digest),
        older,
    ]
    assert selected.identifier == expected_latest
    assert selected.selected_latest is True


def test_missing_workspace_index_does_not_fall_back_to_line_scan(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "missing-index")
    generate_workspace_graphs(app_config, workspace.display)
    create_line(app_config, workspace.display)
    delete_database(_workspace_database(app_config, workspace))
    lines_path = _workspace_path(app_config, workspace) / "lines"
    original_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == lines_path:
            raise AssertionError("missing-index fallback enumerated lines/")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    with pytest.raises(ArtifactError, match="workspace index is missing or stale"):
        list_lines(app_config, workspace.display)
    with pytest.raises(ArtifactError, match="workspace index is missing or stale"):
        resolve_line_for_command(app_config, None, workspace.display)

    # Project-level workspace discovery remains available without a global SQLite database.
    assert [item.identifier for item in list_workspaces(app_config)] == [workspace]


def test_reindex_rebuilds_only_selected_workspace(app_config: AppConfig) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    line_a = create_line(app_config, workspace_a.display)
    line_b = create_line(app_config, workspace_b.display)

    database_a = _workspace_database(app_config, workspace_a)
    database_b = _workspace_database(app_config, workspace_b)
    expected_a = projection_counts(database_a)
    before_b = database_b.read_bytes()

    delete_database(database_a)
    (app_config.workspace.root / "workspace-a").unlink()
    reindex_workspace(app_config, workspace_a.display)

    assert projection_counts(database_a) == expected_a
    assert database_b.read_bytes() == before_b
    assert (app_config.workspace.root / "workspace-a").is_symlink()
    status_a = get_line_status(app_config, line_a.display, workspace_a.display)
    status_b = get_line_status(app_config, line_b.display, workspace_b.display)
    assert status_a.database_state == "indexed"
    assert status_b.database_state == "indexed"
    assert not (app_config.project_root / DATABASE_NAME).exists()


def test_reindex_by_name_can_recover_missing_alias(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "recover-name")
    generate_workspace_graphs(app_config, workspace.display)
    delete_database(_workspace_database(app_config, workspace))
    (app_config.workspace.root / "recover-name").unlink()

    reindex_workspace(app_config, "recover-name")

    assert _workspace_database(app_config, workspace).is_file()
    assert (app_config.workspace.root / "recover-name").is_symlink()


def test_reindex_rejects_invalid_completed_line(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "invalid-line")
    line_path = _workspace_path(app_config, workspace) / "lines" / "ln-deadbeef"
    line_path.mkdir()
    write_json_atomic(line_path / "manifest.json", {"artifact_type": "line"})

    with pytest.raises(ArtifactError, match="invalid line manifest"):
        reindex_workspace(app_config, workspace.display)


def test_semantic_line_mismatch_is_not_reported_as_indexed(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "mismatch")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    database = _workspace_database(app_config, workspace)
    before = projection_counts(database)
    engine = make_engine(database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE lines SET created_at = :created_at WHERE line_hash = :line_hash"),
                {"created_at": "1999-01-01T00:00:00Z", "line_hash": line.digest},
            )
    finally:
        engine.dispose()

    assert projection_counts(database) == before
    assert (
        get_line_status(app_config, line.display, workspace.display).database_state
        == "needs reindex"
    )


def test_workspace_local_index_survives_project_relocation(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    workspace = create_workspace(app_config, "portable")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    source_root = app_config.project_root
    source_database = _workspace_database(app_config, workspace)

    source_connection = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
    try:
        assert str(source_root) not in "\n".join(source_connection.iterdump())
    finally:
        source_connection.close()

    relocated_root = tmp_path / "relocated"
    relocated_root.mkdir()
    shutil.copy2(app_config.source, relocated_root / "experiment.toml")
    shutil.copytree(app_config.workspace.root, relocated_root / "workspaces", symlinks=True)
    relocated_config = load_config(relocated_root / "experiment.toml")

    assert get_workspace_status(relocated_config, "portable").identifier == workspace
    assert get_line_status(relocated_config, line.display, "portable").workspace == workspace

    relocated_database = relocated_root / "workspaces" / workspace.display / DATABASE_NAME
    delete_database(relocated_database)
    reindex_workspace(relocated_config, "portable")
    assert get_workspace_status(relocated_config, "portable").database_state == "indexed"


def test_baseline_evaluation_uses_exact_sql_ordered_line_membership(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "evaluation-membership")
    generate_workspace_graphs(app_config, workspace.display)
    requested = create_line(app_config, workspace.display)
    create_line(app_config, workspace.display)
    database = _workspace_database(app_config, workspace)
    expected = application.line_graph_hashes(database, requested.digest)
    observed: list[tuple[str, ...]] = []
    _install_fake_evaluator(monkeypatch, observed)

    result = evaluate_baseline(app_config, requested.display, workspace.display)

    assert observed == [expected]
    assert result.line == requested
    assert result.graph_count == len(expected)


def test_baseline_evaluation_does_not_scan_line_or_evaluation_directories(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "evaluation-sql")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    workspace_path = _workspace_path(app_config, workspace)
    guarded = {
        workspace_path / "lines",
        workspace_path / "lines" / line.display / "evaluations",
    }
    original_iterdir = Path.iterdir

    def guarded_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path in guarded:
            raise AssertionError("ordinary evaluation enumerated an artifact directory")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    _install_fake_evaluator(monkeypatch)

    result = evaluate_baseline(app_config, line.display, workspace.display)

    assert result.database_state == "indexed"


def test_reindex_reconstructs_evaluation_without_touching_other_workspace(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_a = create_workspace(app_config, "evaluation-a")
    workspace_b = create_workspace(app_config, "evaluation-b")
    for workspace in (workspace_a, workspace_b):
        generate_workspace_graphs(app_config, workspace.display)
    line_a = create_line(app_config, workspace_a.display)
    line_b = create_line(app_config, workspace_b.display)
    _install_fake_evaluator(monkeypatch)
    score_a = evaluate_baseline(app_config, line_a.display, workspace_a.display).score
    evaluate_baseline(app_config, line_b.display, workspace_b.display)
    database_a = _workspace_database(app_config, workspace_a)
    database_b = _workspace_database(app_config, workspace_b)
    before_b = database_b.read_bytes()

    delete_database(database_a)
    reindex_workspace(app_config, workspace_a.display)

    assert projection_counts(database_a)["evaluations"] == 1  # type: ignore[index]
    assert database_b.read_bytes() == before_b
    with sqlite3.connect(database_a) as connection:
        row = connection.execute(
            "SELECT score_lower_numerator, score_lower_denominator FROM evaluations"
        ).fetchone()
    lower = score_a["fitness"]["lower"]  # type: ignore[index]
    assert row == (str(lower["numerator"]), str(lower["denominator"]))  # type: ignore[index]
    assert not (app_config.project_root / DATABASE_NAME).exists()


def test_published_evaluation_survives_sqlite_indexing_failure(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "evaluation-index-failure")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    _install_fake_evaluator(monkeypatch)

    def fail_index(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("injected indexing failure")

    monkeypatch.setattr(application, "index_evaluation", fail_index)
    with pytest.raises(ArtifactError, match="was published.*reindex"):
        evaluate_baseline(app_config, line.display, workspace.display)

    evaluation_directory = (
        _workspace_path(app_config, workspace) / "lines" / line.display / "evaluations"
    )
    artifacts = list(evaluation_directory.iterdir())
    assert len(artifacts) == 1
    assert read_json(artifacts[0])["artifact_type"] == "baseline_evaluation"
    assert projection_counts(_workspace_database(app_config, workspace))["evaluations"] == 0  # type: ignore[index]
    reindex_workspace(app_config, workspace.display)
    assert projection_counts(_workspace_database(app_config, workspace))["evaluations"] == 1  # type: ignore[index]


def _install_fake_evaluator(
    monkeypatch: pytest.MonkeyPatch,
    observed: list[tuple[str, ...]] | None = None,
) -> None:
    class FakeEvaluator:
        def evaluate(self, graphs, _policy):  # type: ignore[no-untyped-def]
            if observed is not None:
                observed.append(tuple(graph.graph_hash for graph in graphs))
            return EvaluationResult(
                RationalInterval(Fraction(1, 2), Fraction(1, 2)),
                EvaluationDiagnostics(
                    episodes=len(graphs),
                    graphs_by_order=tuple(
                        sorted(Counter(graph.order for graph in graphs).items())
                    ),
                    proposals=len(graphs),
                    no_proposals=0,
                    accepted_rewrites=1,
                    score_attempts=len(graphs) * 2,
                    unique_graph_scores=len(graphs) * 2,
                    expanded_score_attempts=0,
                    unsafe_score_timeouts=0,
                    component_statuses=(("EXACT", len(graphs)),),
                ),
                {"binary_sha256": "fixture"},
            )

    monkeypatch.setattr(application, "IndependentEvaluator", FakeEvaluator)


def test_graph_generation_does_not_overwrite_existing_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    generate_workspace_graphs(app_config, workspace.display)
    with pytest.raises(ArtifactError, match="already exist"):
        generate_workspace_graphs(app_config, workspace.display)


def test_line_requires_generated_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    with pytest.raises(ArtifactError, match="no completed graphs"):
        create_line(app_config, workspace.display)


def test_explicit_line_is_scoped_to_selected_workspace(app_config: AppConfig) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    line_b = create_line(app_config, workspace_b.display)

    with pytest.raises(IdentifierError, match="does not resolve in the workspace index"):
        resolve_line_for_command(app_config, line_b.display, workspace_a.display)
    assert (
        resolve_line_for_command(app_config, line_b.display, workspace_b.display).identifier
        == line_b
    )


def test_workspace_names_are_unique_and_safe(app_config: AppConfig) -> None:
    create_workspace(app_config, "testowy")
    with pytest.raises(ArtifactError, match="duplicate workspace name"):
        create_workspace(app_config, "testowy")

    for invalid in (
        "",
        ".",
        "..",
        "../escape",
        "path/name",
        r"path\\name",
        "-leading",
        "ws-deadbeef",
    ):
        with pytest.raises(ArtifactError, match="workspace name must be"):
            create_workspace(app_config, invalid)


def _publish_indexed_line(
    config: AppConfig,
    workspace: Identifier,
    created_at: str,
    graph_hashes: tuple[str, ...],
) -> Identifier:
    identity_payload: dict[str, object] = {
        "workspace_hash": workspace.digest,
        "created_at": created_at,
        "graph_hashes": list(graph_hashes),
    }
    identifier = Identifier.from_bytes(ObjectType.LINE, canonical_json_bytes(identity_payload))
    manifest = {
        "artifact_type": "line",
        "line_hash": identifier.digest,
        **identity_payload,
    }
    line_path = _workspace_path(config, workspace) / "lines" / identifier.display
    line_path.mkdir()
    write_json_atomic(line_path / "manifest.json", manifest)
    engine = make_engine(_workspace_database(config, workspace))
    try:
        with engine.begin() as connection:
            index_line(connection, manifest)
    finally:
        engine.dispose()
    return identifier
