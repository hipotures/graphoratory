from pathlib import Path

import pytest
from typer.testing import CliRunner

from graphoratory.application import create_workspace
from graphoratory.cli import app
from graphoratory.config import AppConfig
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
        ["line", "status", "--help"],
    ],
)
def test_help_works_everywhere(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "--help" in result.stdout
    assert "key=value syntax" not in result.stdout


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

    generated = runner.invoke(app, ["graph", "generate", f"config={config_file}"])
    assert generated.exit_code == 0
    assert "generated 6 graphs" in generated.stdout

    created_line = runner.invoke(app, ["line", "create", f"config={config_file}"])
    assert created_line.exit_code == 0
    line_id = created_line.stdout.strip()
    assert line_id.startswith("ln-")

    line_status = runner.invoke(
        app,
        ["line", "status", line_id, f"config={config_file}"],
    )
    assert line_status.exit_code == 0
    assert line_id in line_status.stdout
    assert workspace_id in line_status.stdout

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
            "graphs.count=4",
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
    assert "set active_workspace in experiment.toml" in result.stderr
    assert "workspace=<name-or-id>" in result.stderr


def test_unknown_override_fails_clearly(config_file: Path) -> None:
    result = runner.invoke(
        app,
        ["graph", "generate", "foo.bar=123", f"config={config_file}"],
    )
    assert result.exit_code == 2
    assert "unknown override key: foo.bar" in result.stderr


@pytest.mark.parametrize("name", ["../escape", "path/name", "ws-deadbeef"])
def test_workspace_init_rejects_unsafe_names(config_file: Path, name: str) -> None:
    result = runner.invoke(
        app,
        ["workspace", "init", name, f"config={config_file}"],
    )
    assert result.exit_code == 2
    assert "workspace name must be" in result.stderr


def test_line_status_requires_an_explicit_line() -> None:
    result = runner.invoke(app, ["line", "status"])
    assert result.exit_code == 2
    assert "Missing argument" in result.stderr
    assert "LINE" in result.stderr


def _set_active_workspace(path: Path, value: str) -> None:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("active_workspace =")
    ]
    path.write_text(
        f'active_workspace = "{value}"\n\n' + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
