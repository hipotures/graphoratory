import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from graphoratory.application import (
    create_line,
    create_workspace,
    generate_workspace_graphs,
    get_line_status,
    get_workspace_status,
    reindex_workspace,
)
from graphoratory.artifacts import DATABASE_NAME, GRAPH_FILE
from graphoratory.config import AppConfig, load_config
from graphoratory.database.core import delete_database, projection_counts
from graphoratory.errors import ArtifactError
from graphoratory.jsonio import read_json, write_json_atomic


def test_workspace_is_minimal_and_migrated(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    path = app_config.workspace.root / workspace.display
    assert {item.name for item in path.iterdir()} == {
        "manifest.json",
        DATABASE_NAME,
        "graphs",
        "lines",
    }
    manifest = read_json(path / "manifest.json")
    assert manifest["name"] == "testowy"
    assert manifest["workspace_hash"] == workspace.digest
    assert "config_source" not in manifest
    assert set(manifest["creation_config"]) == {"graphs"}
    alias = app_config.workspace.root / "testowy"
    assert alias.is_symlink()
    assert alias.readlink() == Path(workspace.display)
    assert alias.resolve() == path
    assert not any(path.rglob("state.json"))
    engine = create_engine(f"sqlite:///{path / DATABASE_NAME}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0004_graph_corpus_generator")
            row = connection.execute(
                text(
                    "SELECT workspace_name, workspace_hash, workspace_short "
                    "FROM workspaces"
                )
            ).one()
            assert row == ("testowy", workspace.digest, workspace.short)
            workspace_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(workspaces)"))
            }
            line_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(lines)"))
            }
            assert "manifest_path" not in workspace_columns
            assert "manifest_path" not in line_columns
    finally:
        engine.dispose()
    database = path / DATABASE_NAME
    before = database.read_bytes()
    assert get_workspace_status(app_config, workspace.display).database_state == "indexed"
    assert database.read_bytes() == before


def test_portable_persistence_migration_upgrades_the_initial_schema(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    alembic_config = Config()
    alembic_config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "src" / "graphoratory" / "database" / "migrations"),
    )
    alembic_config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(alembic_config, "0001_initial")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces "
                    "(workspace_hash, workspace_short, created_at, manifest_path) "
                    "VALUES (:workspace_hash, :workspace_short, :created_at, :manifest_path)"
                ),
                {
                    "workspace_hash": "a" * 64,
                    "workspace_short": "a" * 8,
                    "created_at": "2026-01-01T00:00:00Z",
                    "manifest_path": "/tmp/manifest.json",
                },
            )
    finally:
        engine.dispose()

    command.upgrade(alembic_config, "head")

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0004_graph_corpus_generator"
            assert connection.execute(
                text("SELECT workspace_name FROM workspaces")
            ).scalar_one() is None
            workspace_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(workspaces)"))
            }
            line_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(lines)"))
            }
            assert "manifest_path" not in workspace_columns
            assert "manifest_path" not in line_columns
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'graph_corpora'"
                )
            ).scalar_one() == 1
    finally:
        engine.dispose()


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


def test_full_workflow_and_reindex_from_artifacts(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    generated = generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    workspace_path = app_config.workspace.root / workspace.display
    graphs_path = workspace_path / "graphs"
    line_path = workspace_path / "lines" / line.display

    assert generated.graph_count == app_config.graphs.workspace_graph_count
    assert (graphs_path / GRAPH_FILE).is_file()
    graphs_manifest = read_json(graphs_path / "manifest.json")
    assert len(graphs_manifest["graph_hashes"]) == app_config.graphs.workspace_graph_count
    assert len(set(graphs_manifest["graph_hashes"])) == app_config.graphs.workspace_graph_count
    assert graphs_manifest["generation"]["generator"] == app_config.graphs.generator
    assert graphs_manifest["accepted_distinct_graphs"] == generated.graph_count
    assert sum(graphs_manifest["accepted_by_generator"].values()) == generated.graph_count
    assert graphs_manifest["attempted_candidates"] == generated.attempts
    assert graphs_manifest["rejected_invalid_candidates"] == generated.rejected
    assert graphs_manifest["duplicate_candidates"] == generated.duplicates

    line_manifest = read_json(line_path / "manifest.json")
    selected = line_manifest["graph_hashes"]
    assert len(selected) == app_config.graphs.line_graph_count
    assert len(set(selected)) == app_config.graphs.line_graph_count
    assert set(selected).issubset(set(graphs_manifest["graph_hashes"]))
    persisted_status = get_line_status(app_config, line.display)
    assert persisted_status.graph_count == app_config.graphs.line_graph_count
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from graphoratory.application import get_line_status; "
                "from graphoratory.config import load_config; "
                "import sys; "
                "print(get_line_status(load_config(Path(sys.argv[1])), sys.argv[2]).graph_count)"
            ),
            str(app_config.source),
            line.display,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    assert process.stdout.strip() == str(app_config.graphs.line_graph_count)

    database = workspace_path / DATABASE_NAME
    expected = projection_counts(database)
    assert expected == {
        "workspaces": 1,
        "graph_corpora": 1,
        "graphs": app_config.graphs.workspace_graph_count,
        "lines": 1,
        "line_graphs": app_config.graphs.line_graph_count,
    }
    delete_database(database)
    assert not database.exists()
    alias = app_config.workspace.root / "testowy"
    alias.unlink()
    assert not alias.exists()

    inspection = get_workspace_status(app_config, workspace.display)
    assert inspection.database_state == "needs reindex"
    assert not database.exists()

    reindex_workspace(app_config, workspace.display)
    assert alias.is_symlink()
    assert alias.readlink() == Path(workspace.display)
    assert projection_counts(database) == expected
    assert get_line_status(app_config, line.display).database_state == "indexed"
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            workspace_row = connection.execute(
                text(
                    "SELECT workspace_name, workspace_hash, workspace_short "
                    "FROM workspaces"
                )
            ).one()
            corpus_count = connection.execute(text("SELECT COUNT(*) FROM graphs")).scalar_one()
            corpus_row = connection.execute(
                text(
                    "SELECT generator, requested_graph_count, actual_graph_count "
                    "FROM graph_corpora"
                )
            ).one()
            line_row = connection.execute(
                text("SELECT line_hash, workspace_hash FROM lines")
            ).one()
            membership_count = connection.execute(
                text("SELECT COUNT(*) FROM line_graphs")
            ).scalar_one()
        assert workspace_row == ("testowy", workspace.digest, workspace.short)
        assert corpus_count == app_config.graphs.workspace_graph_count
        assert corpus_row == (
            app_config.graphs.generator,
            app_config.graphs.workspace_graph_count,
            app_config.graphs.workspace_graph_count,
        )
        assert line_row == (line.digest, workspace.digest)
        assert membership_count == app_config.graphs.line_graph_count
    finally:
        engine.dispose()


def test_reindex_preserves_a_workspace_created_before_names(
    app_config: AppConfig,
) -> None:
    workspace = create_workspace(app_config, "temporary-name")
    workspace_path = app_config.workspace.root / workspace.display
    manifest_path = workspace_path / "manifest.json"
    manifest = read_json(manifest_path)
    del manifest["name"]
    write_json_atomic(manifest_path, manifest)
    (app_config.workspace.root / "temporary-name").unlink()
    database = workspace_path / DATABASE_NAME
    delete_database(database)

    reindex_workspace(app_config, workspace.display)

    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT workspace_name FROM workspaces")
            ).scalar_one() is None
    finally:
        engine.dispose()


def test_reindex_normalizes_legacy_manifest_metadata(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "legacy-metadata")
    generate_workspace_graphs(app_config, workspace.display)
    workspace_path = app_config.workspace.root / workspace.display
    workspace_manifest_path = workspace_path / "manifest.json"
    workspace_manifest = read_json(workspace_manifest_path)
    workspace_graphs = workspace_manifest["creation_config"]["graphs"]
    workspace_graphs["count"] = workspace_graphs.pop("workspace_graph_count")
    workspace_graphs["line_sample_size"] = workspace_graphs.pop("line_graph_count")
    workspace_graphs["mode"] = "unrestricted_min_degree_3"
    del workspace_graphs["generator"]
    workspace_graphs["order_distribution"] = "round_robin"
    workspace_manifest["config_source"] = str(app_config.source)
    workspace_manifest["creation_config"]["workspace"] = {
        "root": str(app_config.workspace.root)
    }
    write_json_atomic(workspace_manifest_path, workspace_manifest)

    graphs_manifest_path = workspace_path / "graphs" / "manifest.json"
    graphs_manifest = read_json(graphs_manifest_path)
    generation = graphs_manifest["generation"]
    generation["count"] = generation.pop("workspace_graph_count")
    generation["line_sample_size"] = generation.pop("line_graph_count")
    generation["mode"] = "unrestricted_min_degree_3"
    del generation["generator"]
    generation["order_distribution"] = "round_robin"
    graphs_manifest["generation_attempts"] = graphs_manifest.pop("attempted_candidates")
    graphs_manifest["duplicate_attempts"] = graphs_manifest.pop("duplicate_candidates")
    del graphs_manifest["rejected_invalid_candidates"]
    del graphs_manifest["accepted_distinct_graphs"]
    del graphs_manifest["accepted_by_generator"]
    write_json_atomic(graphs_manifest_path, graphs_manifest)

    reindex_workspace(app_config, workspace.display)

    workspace_manifest = read_json(workspace_manifest_path)
    graphs_manifest = read_json(graphs_manifest_path)
    assert "config_source" not in workspace_manifest
    assert "workspace" not in workspace_manifest["creation_config"]
    assert set(workspace_manifest["creation_config"]["graphs"]) >= {
        "workspace_graph_count",
        "line_graph_count",
    }
    assert set(graphs_manifest["generation"]) >= {
        "workspace_graph_count",
        "line_graph_count",
    }
    assert "count" not in workspace_manifest["creation_config"]["graphs"]
    assert "line_sample_size" not in workspace_manifest["creation_config"]["graphs"]
    assert "count" not in graphs_manifest["generation"]
    assert "line_sample_size" not in graphs_manifest["generation"]
    assert workspace_manifest["creation_config"]["graphs"]["generator"] == (
        "cycle_matching_stub_pairing"
    )
    assert graphs_manifest["generation"]["generator"] == "cycle_matching_stub_pairing"
    assert graphs_manifest["generation"]["order_distribution"] == "accepted_round_robin"
    assert graphs_manifest["accepted_distinct_graphs"] == graphs_manifest["graph_count"]
    assert graphs_manifest["rejected_invalid_candidates"] == 0
    assert graphs_manifest["accepted_by_generator"] == {
        "cycle_matching_stub_pairing": graphs_manifest["graph_count"]
    }


def test_workspace_survives_relocation_and_reindex(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    workspace = create_workspace(app_config, "portable")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    source_root = app_config.source.parent
    source_workspace = app_config.workspace.root / workspace.display
    source_database = source_workspace / DATABASE_NAME

    for manifest_path in source_workspace.rglob("*.json"):
        assert str(source_root) not in manifest_path.read_text(encoding="utf-8")
    source_connection = sqlite3.connect(f"file:{source_database}?mode=ro", uri=True)
    try:
        assert str(source_root) not in "\n".join(source_connection.iterdump())
    finally:
        source_connection.close()

    relocated_root = tmp_path / "relocated"
    relocated_root.mkdir()
    shutil.copy2(app_config.source, relocated_root / "experiment.toml")
    relocated_workspaces = relocated_root / "workspaces"
    shutil.copytree(
        app_config.workspace.root,
        relocated_workspaces,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            DATABASE_NAME,
            f"{DATABASE_NAME}-wal",
            f"{DATABASE_NAME}-shm",
        ),
    )
    relocated_database = relocated_workspaces / workspace.display / DATABASE_NAME
    _backup_database(source_database, relocated_database)

    relocated_config = load_config(relocated_root / "experiment.toml")
    assert get_workspace_status(relocated_config, "portable").identifier == workspace
    assert get_line_status(relocated_config, line.display).workspace == workspace

    delete_database(relocated_database)
    reindex_workspace(relocated_config, "portable")
    assert get_workspace_status(relocated_config, "portable").database_state == "indexed"
    relocated_connection = sqlite3.connect(
        f"file:{relocated_database}?mode=ro",
        uri=True,
    )
    try:
        assert str(source_root) not in "\n".join(relocated_connection.iterdump())
    finally:
        relocated_connection.close()


def test_graph_generation_does_not_overwrite_existing_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    generate_workspace_graphs(app_config, workspace.display)
    with pytest.raises(ArtifactError, match="already exist"):
        generate_workspace_graphs(app_config, workspace.display)


def _backup_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
