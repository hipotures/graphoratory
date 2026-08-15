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
from graphoratory.config import AppConfig
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
    assert not any(path.rglob("state.json"))
    engine = create_engine(f"sqlite:///{path / DATABASE_NAME}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0002_workspace_name")
            row = connection.execute(
                text(
                    "SELECT workspace_name, workspace_hash, workspace_short "
                    "FROM workspaces"
                )
            ).one()
            assert row == ("testowy", workspace.digest, workspace.short)
    finally:
        engine.dispose()
    database = path / DATABASE_NAME
    before = database.read_bytes()
    assert get_workspace_status(app_config, workspace.display).database_state == "indexed"
    assert database.read_bytes() == before


def test_workspace_name_migration_upgrades_the_initial_schema(tmp_path: Path) -> None:
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
            ).scalar_one() == "0002_workspace_name"
            assert connection.execute(
                text("SELECT workspace_name FROM workspaces")
            ).scalar_one() is None
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

    assert generated.graph_count == app_config.graphs.count
    assert (graphs_path / GRAPH_FILE).is_file()
    graphs_manifest = read_json(graphs_path / "manifest.json")
    assert len(graphs_manifest["graph_hashes"]) == app_config.graphs.count
    assert len(set(graphs_manifest["graph_hashes"])) == app_config.graphs.count

    line_manifest = read_json(line_path / "manifest.json")
    selected = line_manifest["graph_hashes"]
    assert len(selected) == app_config.graphs.line_sample_size
    assert len(set(selected)) == app_config.graphs.line_sample_size
    assert set(selected).issubset(set(graphs_manifest["graph_hashes"]))
    persisted_status = get_line_status(app_config, line.display)
    assert persisted_status.graph_count == app_config.graphs.line_sample_size
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
    assert process.stdout.strip() == str(app_config.graphs.line_sample_size)

    database = workspace_path / DATABASE_NAME
    expected = projection_counts(database)
    assert expected == {
        "workspaces": 1,
        "graphs": app_config.graphs.count,
        "lines": 1,
        "line_graphs": app_config.graphs.line_sample_size,
    }
    delete_database(database)
    assert not database.exists()

    inspection = get_workspace_status(app_config, workspace.display)
    assert inspection.database_state == "needs reindex"
    assert not database.exists()

    reindex_workspace(app_config, workspace.display)
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
            line_row = connection.execute(
                text("SELECT line_hash, workspace_hash FROM lines")
            ).one()
            membership_count = connection.execute(
                text("SELECT COUNT(*) FROM line_graphs")
            ).scalar_one()
        assert workspace_row == ("testowy", workspace.digest, workspace.short)
        assert corpus_count == app_config.graphs.count
        assert line_row == (line.digest, workspace.digest)
        assert membership_count == app_config.graphs.line_sample_size
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


def test_graph_generation_does_not_overwrite_existing_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config, "testowy")
    generate_workspace_graphs(app_config, workspace.display)
    with pytest.raises(ArtifactError, match="already exist"):
        generate_workspace_graphs(app_config, workspace.display)
