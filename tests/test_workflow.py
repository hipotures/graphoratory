import subprocess
import sys

import pytest
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
from graphoratory.jsonio import read_json


def test_workspace_is_minimal_and_migrated(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config)
    path = app_config.workspace.root / workspace.display
    assert {item.name for item in path.iterdir()} == {
        "manifest.json",
        DATABASE_NAME,
        "graphs",
        "lines",
    }
    manifest = read_json(path / "manifest.json")
    assert manifest["workspace_hash"] == workspace.digest
    assert not any(path.rglob("state.json"))
    engine = create_engine(f"sqlite:///{path / DATABASE_NAME}")
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0001_initial")
    finally:
        engine.dispose()


def test_line_requires_generated_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config)
    with pytest.raises(ArtifactError, match="no completed graphs"):
        create_line(app_config, workspace.display)


def test_full_workflow_and_reindex_from_artifacts(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config)
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


def test_graph_generation_does_not_overwrite_existing_graphs(app_config: AppConfig) -> None:
    workspace = create_workspace(app_config)
    generate_workspace_graphs(app_config, workspace.display)
    with pytest.raises(ArtifactError, match="already exist"):
        generate_workspace_graphs(app_config, workspace.display)
