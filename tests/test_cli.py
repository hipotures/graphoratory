import json
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from graphoratory.application import (
    create_line,
    create_workspace,
    generate_workspace_graphs,
)
from graphoratory.artifacts import DATABASE_NAME
from graphoratory.cli import app
from graphoratory.config import AppConfig
from graphoratory.database.core import delete_database
from graphoratory.jsonio import read_json

runner = CliRunner()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["workspace", "--help"],
        ["workspace", "init", "--help"],
        ["workspace", "status", "--help"],
        ["workspace", "list", "--help"],
        ["workspace", "reindex", "--help"],
        ["graph", "--help"],
        ["graph", "generate", "--help"],
        ["line", "--help"],
        ["line", "create", "--help"],
        ["line", "list", "--help"],
        ["line", "status", "--help"],
    ],
)
def test_help_works_everywhere(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    output = Text.from_ansi(result.stdout).plain
    assert result.exit_code == 0
    assert "Usage:" in output
    assert "--help" in output
    assert "key=value syntax" not in output


def test_json_output_covers_every_command(config_file: Path) -> None:
    initialized = runner.invoke(
        app,
        ["workspace", "init", "json-test", "--json", f"config={config_file}"],
    )
    assert initialized.exit_code == 0
    workspace = json.loads(initialized.stdout)["workspace"]
    assert workspace["id"].startswith("ws-")
    assert len(workspace["hash"]) == 64
    assert "\x1b" not in initialized.stdout
    _set_active_workspace(config_file, "json-test")

    listed_workspaces = runner.invoke(
        app,
        ["workspace", "list", "--json", f"config={config_file}"],
    )
    workspace_rows = json.loads(listed_workspaces.stdout)["workspaces"]
    assert listed_workspaces.exit_code == 0
    assert workspace_rows == [
        {
            "active": True,
            "created_at": workspace_rows[0]["created_at"],
            "hash": workspace["hash"],
            "id": workspace["id"],
            "name": "json-test",
        }
    ]

    workspace_status = runner.invoke(
        app,
        ["workspace", "status", "--json", f"config={config_file}"],
    )
    status_payload = json.loads(workspace_status.stdout)
    assert workspace_status.exit_code == 0
    assert status_payload["workspace"] == {**workspace, "name": "json-test"}
    assert status_payload["graphs"] == 0
    assert status_payload["lines"] == 0
    assert isinstance(status_payload["disk_bytes"], int)

    generated = runner.invoke(
        app,
        ["graph", "generate", "--json", f"config={config_file}"],
    )
    generated_payload = json.loads(generated.stdout)
    assert generated.exit_code == 0
    assert generated_payload["workspace"] == workspace
    assert generated_payload["graph_count"] == 6
    assert sum(generated_payload["accepted_by_generator"].values()) == 6
    assert generated.stderr == ""

    created_line = runner.invoke(
        app,
        ["line", "create", "--json", f"config={config_file}"],
    )
    assert created_line.exit_code == 0
    line = json.loads(created_line.stdout)["line"]
    assert line["id"].startswith("ln-")
    assert len(line["hash"]) == 64

    listed_lines = runner.invoke(
        app,
        [
            "line",
            "list",
            "workspace=json-test",
            "--json",
            f"config={config_file}",
        ],
    )
    line_list_payload = json.loads(listed_lines.stdout)
    assert listed_lines.exit_code == 0
    assert line_list_payload["workspace"] == {**workspace, "name": "json-test"}
    assert line_list_payload["lines"] == [
        {
            "created_at": line_list_payload["lines"][0]["created_at"],
            "graphs": 2,
            "hash": line["hash"],
            "id": line["id"],
            "latest": True,
        }
    ]

    line_status = runner.invoke(
        app,
        ["line", "status", "--json", f"config={config_file}"],
    )
    line_status_payload = json.loads(line_status.stdout)
    assert line_status.exit_code == 0
    assert line_status_payload["line"] == line
    assert line_status_payload["workspace"] == {**workspace, "name": "json-test"}
    assert line_status_payload["graphs"] == 2
    assert line_status_payload["selected_latest"] is True

    reindexed = runner.invoke(
        app,
        ["workspace", "reindex", "--json", f"config={config_file}"],
    )
    reindex_payload = json.loads(reindexed.stdout)
    assert reindexed.exit_code == 0
    assert reindex_payload["reindexed"] is True
    assert reindex_payload["workspace"] == {**workspace, "name": "json-test"}
    assert reindex_payload["database"] == "indexed"


def test_json_output_formats_application_errors(config_file: Path) -> None:
    result = runner.invoke(
        app,
        ["line", "list", "--json", f"config={config_file}"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "ArtifactError"
    assert "no workspace selected" in error["message"]
    assert "\x1b" not in result.stderr


@pytest.mark.parametrize(
    ("arguments", "error_type", "message"),
    [
        (
            ["workspace", "init", "--json"],
            "MissingParameter",
            "Missing argument 'NAME'.",
        ),
        (
            ["graph", "generate", "--unknown-option", "--json"],
            "NoSuchOption",
            "No such option: --unknown-option",
        ),
        (
            ["unknown-command", "--json"],
            "UsageError",
            "No such command 'unknown-command'.",
        ),
    ],
)
def test_json_output_formats_typer_parser_errors(
    arguments: list[str],
    error_type: str,
    message: str,
) -> None:
    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error == {
        "message": message,
        "type": error_type,
    }
    assert "\x1b" not in result.stderr
    assert "Usage:" not in result.stderr


def test_parser_errors_without_json_remain_typer_rich() -> None:
    result = runner.invoke(app, ["workspace", "init"])

    assert result.exit_code == 2
    assert "Missing argument" in Text.from_ansi(result.stderr).plain
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stderr)


def test_workspace_commands_use_active_name_and_id(config_file: Path) -> None:
    initialized = runner.invoke(
        app,
        ["workspace", "init", "testowy", f"config={config_file}"],
    )
    assert initialized.exit_code == 0
    workspace_id = initialized.stdout.strip()
    assert workspace_id.startswith("ws-")
    workspace_path = config_file.parent / "workspaces" / workspace_id

    manifest = read_json(workspace_path / "manifest.json")
    assert manifest["name"] == "testowy"
    assert workspace_path.name == workspace_id
    duplicate = runner.invoke(
        app,
        ["workspace", "init", "testowy", f"config={config_file}"],
    )
    assert duplicate.exit_code == 2
    assert "duplicate workspace name" in duplicate.stderr

    _set_active_workspace(config_file, "testowy")
    listed = runner.invoke(app, ["workspace", "list", f"config={config_file}"])
    assert listed.exit_code == 0
    assert "testowy" in listed.stdout
    assert workspace_id in listed.stdout
    assert "*" in listed.stdout

    active_status = runner.invoke(app, ["workspace", "status", f"config={config_file}"])
    assert active_status.exit_code == 0
    assert "testowy" in active_status.stdout
    assert workspace_id in active_status.stdout
    assert str(config_file) not in active_status.stdout
    assert "$PROJECT/experiment.toml" in active_status.stdout

    generated = runner.invoke(app, ["graph", "generate", f"config={config_file}"])
    assert generated.exit_code == 0
    assert "generated 6 graphs" in generated.stdout

    created_line = runner.invoke(app, ["line", "create", f"config={config_file}"])
    assert created_line.exit_code == 0
    line_id = created_line.stdout.strip()
    assert line_id.startswith("ln-")

    listed_lines = runner.invoke(
        app,
        ["line", "list", f"config={config_file}"],
    )
    assert listed_lines.exit_code == 0
    assert "Workspace: testowy" in listed_lines.stdout
    assert workspace_id in listed_lines.stdout
    assert all(
        column in listed_lines.stdout
        for column in ("ID", "CREATED", "GRAPHS", "LATEST")
    )
    assert line_id in listed_lines.stdout
    assert listed_lines.stdout.count("*") == 1

    line_status = runner.invoke(
        app,
        ["line", "status", line_id, f"config={config_file}"],
    )
    assert line_status.exit_code == 0
    assert line_id in line_status.stdout
    assert workspace_id in line_status.stdout
    assert "latest in workspace" not in line_status.stdout

    latest_line_status = runner.invoke(
        app,
        ["line", "status", f"config={config_file}"],
    )
    assert latest_line_status.exit_code == 0
    assert f"{line_id} (latest in workspace testowy)" in latest_line_status.stdout
    assert workspace_id in latest_line_status.stdout

    latest_with_workspace_override = runner.invoke(
        app,
        ["line", "status", "workspace=testowy", f"config={config_file}"],
    )
    assert latest_with_workspace_override.exit_code == 0
    assert f"{line_id} (latest in workspace testowy)" in (
        latest_with_workspace_override.stdout
    )

    reindexed = runner.invoke(
        app,
        ["workspace", "reindex", f"config={config_file}"],
    )
    assert reindexed.exit_code == 0
    assert "Reindex complete" in reindexed.stdout
    assert "Name" in reindexed.stdout
    assert "testowy" in reindexed.stdout
    assert "Workspace" in reindexed.stdout
    assert workspace_id in reindexed.stdout
    assert "Generator" in reindexed.stdout
    assert "mixed" in reindexed.stdout
    assert "Graphs" in reindexed.stdout
    assert "Lines" in reindexed.stdout
    assert "Database" in reindexed.stdout
    assert "indexed" in reindexed.stdout
    assert "Disk usage" in reindexed.stdout
    assert str(config_file) not in reindexed.stdout
    assert "$PROJECT/experiment.toml" in reindexed.stdout

    by_name = runner.invoke(
        app,
        ["workspace", "status", "testowy", f"config={config_file}"],
    )
    by_id = runner.invoke(
        app,
        ["workspace", "status", workspace_id, f"config={config_file}"],
    )
    assert by_name.exit_code == 0
    assert by_id.exit_code == 0
    assert workspace_id in by_name.stdout
    assert workspace_id in by_id.stdout

    _set_active_workspace(config_file, workspace_id)
    active_by_id = runner.invoke(app, ["workspace", "status", f"config={config_file}"])
    assert active_by_id.exit_code == 0
    assert workspace_id in active_by_id.stdout


def test_explicit_workspace_override_wins(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    _set_active_workspace(config_file, "workspace-a")

    result = runner.invoke(
        app,
        [
            "graph",
            "generate",
            "workspace=workspace-b",
            "graphs.workspace_graph_count=4",
            f"config={config_file}",
        ],
    )
    assert result.exit_code == 0
    assert "generated 4 graphs" in result.stdout
    root = app_config.workspace.root
    assert not (root / workspace_a.display / "graphs" / "manifest.json").exists()
    manifest = read_json(root / workspace_b.display / "graphs" / "manifest.json")
    assert manifest["graph_count"] == 4


def test_no_active_workspace_fails_even_with_one_workspace(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    create_workspace(app_config, "only")
    result = runner.invoke(app, ["workspace", "status", f"config={config_file}"])
    assert result.exit_code == 2
    assert "no workspace selected" in result.stderr
    assert "set workspace.active in experiment.toml" in result.stderr
    assert "workspace=<name-or-id>" in result.stderr

    configured = runner.invoke(
        app,
        [
            "workspace",
            "status",
            "workspace.active=only",
            f"config={config_file}",
        ],
    )
    assert configured.exit_code == 0
    assert "only" in configured.stdout


def test_unknown_override_fails_clearly(config_file: Path) -> None:
    result = runner.invoke(
        app,
        ["graph", "generate", "foo.bar=123", f"config={config_file}"],
    )
    assert result.exit_code == 2
    assert "unknown override key: foo.bar" in result.stderr


def test_config_path_is_reported_relative_to_project_root(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = config_file.parent
    nested_config = project_root / "configs" / "test.toml"
    nested_config.parent.mkdir()
    nested_config.write_text(
        config_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.chdir(project_root)
    initialized = runner.invoke(
        app,
        ["workspace", "init", "nested", "config=configs/test.toml"],
    )
    assert initialized.exit_code == 0
    _set_active_workspace(nested_config, "nested")

    status = runner.invoke(
        app,
        ["workspace", "status", "config=configs/test.toml"],
    )

    assert status.exit_code == 0
    assert "$PROJECT/configs/test.toml" in status.stdout
    assert str(nested_config) not in status.stdout


@pytest.mark.parametrize("name", ["../escape", "path/name", "ws-deadbeef"])
def test_workspace_init_rejects_unsafe_names(config_file: Path, name: str) -> None:
    result = runner.invoke(
        app,
        ["workspace", "init", name, f"config={config_file}"],
    )
    assert result.exit_code == 2
    assert "workspace name must be" in result.stderr


def test_line_status_without_a_line_requires_a_workspace(config_file: Path) -> None:
    result = runner.invoke(app, ["line", "status", f"config={config_file}"])
    assert result.exit_code == 2
    assert "no workspace selected" in result.stderr


def test_line_list_is_workspace_scoped_and_sqlite_independent(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    line_a = create_line(app_config, workspace_a.display)
    line_b = create_line(app_config, workspace_b.display)
    _set_active_workspace(config_file, "workspace-a")
    delete_database(app_config.workspace.root / workspace_a.display / DATABASE_NAME)

    active = runner.invoke(app, ["line", "list", f"config={config_file}"])
    explicit = runner.invoke(
        app,
        [
            "line",
            "list",
            f"workspace={workspace_b.display}",
            f"config={config_file}",
        ],
    )

    assert active.exit_code == 0
    assert "Workspace: workspace-a" in active.stdout
    assert line_a.display in active.stdout
    assert line_b.display not in active.stdout
    assert active.stdout.count("*") == 1
    assert explicit.exit_code == 0
    assert "Workspace: workspace-b" in explicit.stdout
    assert line_b.display in explicit.stdout
    assert line_a.display not in explicit.stdout
    assert explicit.stdout.count("*") == 1


def test_line_list_handles_an_empty_workspace(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    create_workspace(app_config, "empty")
    _set_active_workspace(config_file, "empty")

    result = runner.invoke(app, ["line", "list", f"config={config_file}"])

    assert result.exit_code == 0
    assert "Workspace: empty" in result.stdout
    assert "No lines in workspace empty." in result.stdout


def _set_active_workspace(path: Path, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.startswith("active =")]
    workspace_section = lines.index("[workspace]")
    lines.insert(workspace_section + 2, f'active = "{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
