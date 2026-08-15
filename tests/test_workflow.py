import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import graphoratory.application as application
from graphoratory.application import (
    create_line,
    create_workspace,
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
    migrate,
    projection_counts,
)
from graphoratory.errors import ArtifactError
from graphoratory.identifiers import Identifier, ObjectType
from graphoratory.jsonio import canonical_json_bytes, read_json, write_json_atomic


def test_workspace_uses_one_project_database(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    workspace_path = app_config.workspace.root / workspace.display
    project_database = database_path(app_config.project_root)

    assert project_database == app_config.project_root / DATABASE_NAME
    assert project_database.is_file()
    assert not (workspace_path / DATABASE_NAME).exists()
    assert {item.name for item in workspace_path.iterdir()} == {
        "manifest.json",
        "graphs",
        "lines",
    }
    assert (app_config.workspace.root / "testowy").is_symlink()
    assert not any(workspace_path.rglob("state.json"))

    engine = create_engine(f"sqlite:///{project_database}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0001_initial"
            assert connection.execute(
                text(
                    "SELECT workspace_name, workspace_hash, workspace_short "
                    "FROM workspaces"
                )
            ).one() == ("testowy", workspace.digest, workspace.short)
    finally:
        engine.dispose()


def test_workspace_artifact_survives_indexing_failure(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_index(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected indexing failure")

    monkeypatch.setattr(application, "index_workspace", fail_index)
    with pytest.raises(ArtifactError, match="workspace was published"):
        create_workspace(app_config, "recoverable")

    manifests = list(app_config.workspace.root.glob("ws-*/manifest.json"))
    assert len(manifests) == 1
    assert read_json(manifests[0])["name"] == "recoverable"


def test_multiple_workspaces_share_one_index_and_duplicate_graph_hashes(
    app_config: AppConfig,
) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    line_a = create_line(app_config, workspace_a.display)
    line_b = create_line(app_config, workspace_b.display)
    database = database_path(app_config.project_root)

    assert projection_counts(database) == {
        "workspaces": 2,
        "graph_corpora": 2,
        "graphs": 2 * app_config.graphs.workspace_graph_count,
        "lines": 2,
        "line_graphs": 2 * app_config.graphs.line_graph_count,
    }
    assert not list(app_config.workspace.root.rglob(DATABASE_NAME))
    assert get_line_status(app_config, line_a.display).workspace == workspace_a
    assert get_line_status(app_config, line_b.display).workspace == workspace_b

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            repeated = connection.execute(
                text(
                    "SELECT graph_hash, COUNT(*) FROM graphs "
                    "GROUP BY graph_hash HAVING COUNT(*) = 2"
                )
            ).all()
    finally:
        engine.dispose()
    assert len(repeated) == app_config.graphs.workspace_graph_count


def test_workspace_and_line_lists_do_not_enumerate_artifacts(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "indexed")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)

    def reject_enumeration(_path: Path) -> None:
        raise AssertionError("ordinary lookup enumerated the filesystem")

    monkeypatch.setattr(Path, "iterdir", reject_enumeration)

    assert [item.identifier for item in list_workspaces(app_config)] == [workspace]
    listed = list_lines(app_config, workspace.display)
    assert [item.identifier for item in listed.lines] == [line]
    assert resolve_line_for_command(
        app_config, line.display, workspace.display
    ).identifier == line


def test_explicit_line_status_reads_only_derived_artifacts(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "specific")
    generate_workspace_graphs(app_config, workspace.display)
    requested = create_line(app_config, workspace.display)
    other = create_line(app_config, workspace.display)
    requested_manifest = (
        app_config.workspace.root
        / workspace.display
        / "lines"
        / requested.display
        / "manifest.json"
    )
    other_manifest = (
        app_config.workspace.root
        / workspace.display
        / "lines"
        / other.display
        / "manifest.json"
    )
    opened: list[Path] = []
    original_read_json = application.read_json

    def record_read(path: Path) -> dict[str, object]:
        opened.append(path)
        return original_read_json(path)

    monkeypatch.setattr(application, "read_json", record_read)
    status = get_line_status(app_config, requested.display)

    assert status.identifier == requested
    assert requested_manifest in opened
    assert other_manifest not in opened


def test_latest_line_uses_sql_timestamp_and_hash_ordering(
    app_config: AppConfig,
) -> None:
    workspace = create_workspace(app_config, "latest")
    generate_workspace_graphs(app_config, workspace.display)
    graph_hashes = tuple(
        read_json(
            app_config.workspace.root
            / workspace.display
            / "graphs"
            / "manifest.json"
        )["graph_hashes"][:2]
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


def test_normal_queries_require_the_project_index(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "missing-index")
    delete_database(database_path(app_config.project_root))

    def reject_enumeration(_path: Path) -> None:
        raise AssertionError("missing-index fallback enumerated the filesystem")

    monkeypatch.setattr(Path, "iterdir", reject_enumeration)
    with pytest.raises(ArtifactError, match="project index is missing or stale"):
        list_workspaces(app_config)
    with pytest.raises(ArtifactError, match="project index is missing or stale"):
        list_lines(app_config, workspace.display)


def test_full_reindex_restores_all_project_entities(app_config: AppConfig) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    line_a = create_line(app_config, workspace_a.display)
    line_b = create_line(app_config, workspace_b.display)
    database = database_path(app_config.project_root)
    expected = projection_counts(database)
    obsolete_database = (
        app_config.workspace.root / workspace_b.display / DATABASE_NAME
    )
    migrate(obsolete_database)

    delete_database(database)
    (app_config.workspace.root / "workspace-a").unlink()
    (app_config.workspace.root / "workspace-b").unlink()
    reindex_workspace(app_config, workspace_a.display)

    assert projection_counts(database) == expected
    assert (app_config.workspace.root / "workspace-a").is_symlink()
    assert (app_config.workspace.root / "workspace-b").is_symlink()
    assert not obsolete_database.exists()
    assert get_line_status(app_config, line_a.display).database_state == "indexed"
    assert get_line_status(app_config, line_b.display).database_state == "indexed"


def test_reindex_rejects_invalid_completed_line(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "invalid-line")
    line_path = app_config.workspace.root / workspace.display / "lines" / "ln-deadbeef"
    line_path.mkdir()
    write_json_atomic(line_path / "manifest.json", {"artifact_type": "line"})

    with pytest.raises(ArtifactError, match="invalid line manifest"):
        reindex_workspace(app_config, workspace.display)


def test_semantic_line_mismatch_is_not_reported_as_indexed(
    app_config: AppConfig,
) -> None:
    workspace = create_workspace(app_config, "mismatch")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    database = database_path(app_config.project_root)
    before = projection_counts(database)
    engine = make_engine(database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE lines SET created_at = :created_at WHERE line_hash = :line_hash"),
                {
                    "created_at": "1999-01-01T00:00:00Z",
                    "line_hash": line.digest,
                },
            )
    finally:
        engine.dispose()

    assert projection_counts(database) == before
    assert get_line_status(app_config, line.display).database_state == "needs reindex"


def test_project_survives_relocation_with_one_database(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    workspace = create_workspace(app_config, "portable")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    source_root = app_config.project_root
    source_database = database_path(source_root)

    for manifest_path in app_config.workspace.root.rglob("*.json"):
        assert str(source_root) not in manifest_path.read_text(encoding="utf-8")
    source_connection = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
    try:
        assert str(source_root) not in "\n".join(source_connection.iterdump())
    finally:
        source_connection.close()

    relocated_root = tmp_path / "relocated"
    relocated_root.mkdir()
    shutil.copy2(app_config.source, relocated_root / "experiment.toml")
    shutil.copytree(
        app_config.workspace.root,
        relocated_root / "workspaces",
        symlinks=True,
    )
    relocated_database = relocated_root / DATABASE_NAME
    _backup_database(source_database, relocated_database)
    relocated_config = load_config(relocated_root / "experiment.toml")

    assert get_workspace_status(relocated_config, "portable").identifier == workspace
    assert get_line_status(relocated_config, line.display).workspace == workspace
    relocated_connection = sqlite3.connect(
        f"file:{relocated_database}?mode=ro",
        uri=True,
    )
    try:
        assert str(source_root) not in "\n".join(relocated_connection.iterdump())
    finally:
        relocated_connection.close()

    delete_database(relocated_database)
    reindex_workspace(relocated_config, "portable")
    assert get_workspace_status(relocated_config, "portable").database_state == "indexed"


def test_graph_generation_does_not_overwrite_existing_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    generate_workspace_graphs(app_config, workspace.display)
    with pytest.raises(ArtifactError, match="already exist"):
        generate_workspace_graphs(app_config, workspace.display)


def test_line_requires_generated_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    with pytest.raises(ArtifactError, match="no completed graphs"):
        create_line(app_config, workspace.display)


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
        r"path\name",
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
    identifier = Identifier.from_bytes(
        ObjectType.LINE,
        canonical_json_bytes(identity_payload),
    )
    manifest = {
        "artifact_type": "line",
        "line_hash": identifier.digest,
        "workspace_hash": workspace.digest,
        "created_at": created_at,
        "graph_hashes": list(graph_hashes),
    }
    line_path = config.workspace.root / workspace.display / "lines" / identifier.display
    line_path.mkdir()
    write_json_atomic(line_path / "manifest.json", manifest)
    engine = make_engine(database_path(config.project_root))
    try:
        with engine.begin() as connection:
            index_line(connection, manifest)
    finally:
        engine.dispose()
    return identifier


def _backup_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
