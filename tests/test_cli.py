import json
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

import graphoratory.application as application
from graphoratory.application import create_line, create_workspace, generate_workspace_graphs
from graphoratory.cli import app
from graphoratory.config import AppConfig
from graphoratory.database.core import database_path, delete_database
from graphoratory.jsonio import read_json
from graphoratory.science.evaluator import (
    EvaluationDiagnostics,
    EvaluationResult,
    RationalInterval,
)

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
        ["baseline", "--help"],
        ["baseline", "evaluate", "--help"],
    ],
)
def test_help_works_everywhere(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    output = Text.from_ansi(result.stdout).plain
    assert result.exit_code == 0
    assert "Usage:" in output
    assert "--help" in output


def test_json_output_covers_every_command(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_evaluator(monkeypatch)
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
    assert listed_workspaces.exit_code == 0
    workspace_rows = json.loads(listed_workspaces.stdout)["workspaces"]
    assert len(workspace_rows) == 1
    assert workspace_rows[0]["id"] == workspace["id"]
    assert workspace_rows[0]["active"] is True

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
        ["line", "list", "--json", f"config={config_file}"],
    )
    line_list_payload = json.loads(listed_lines.stdout)
    assert listed_lines.exit_code == 0
    assert line_list_payload["workspace"] == {**workspace, "name": "json-test"}
    assert len(line_list_payload["lines"]) == 1
    assert line_list_payload["lines"][0]["id"] == line["id"]
    assert line_list_payload["lines"][0]["latest"] is True

    line_status = runner.invoke(
        app,
        ["line", "status", "--json", f"config={config_file}"],
    )
    line_status_payload = json.loads(line_status.stdout)
    assert line_status.exit_code == 0
    assert line_status_payload["line"] == line
    assert line_status_payload["selected_latest"] is True

    evaluated = runner.invoke(
        app,
        ["baseline", "evaluate", "--json", f"config={config_file}"],
    )
    evaluation_payload = json.loads(evaluated.stdout)
    assert evaluated.exit_code == 0
    assert evaluation_payload["baseline"] == "heg_uniform_two_switch"
    assert evaluation_payload["line"] == line
    assert evaluation_payload["graphs"] == 2
    assert evaluation_payload["score"]["fitness"]["lower"] == {
        "numerator": 1,
        "denominator": 2,
    }
    assert evaluation_payload["database"] == "indexed"

    reindexed = runner.invoke(
        app,
        ["workspace", "reindex", "--json", f"config={config_file}"],
    )
    reindex_payload = json.loads(reindexed.stdout)
    assert reindexed.exit_code == 0
    assert reindex_payload["reindexed"] is True
    assert reindex_payload["workspace"] == {**workspace, "name": "json-test"}
    assert reindex_payload["database"] == "indexed"


@pytest.mark.parametrize(
    ("arguments", "error_type", "message"),
    [
        (["workspace", "init", "--json"], "MissingParameter", "Missing argument 'NAME'."),
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
    arguments: list[str], error_type: str, message: str
) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"] == {
        "message": message,
        "type": error_type,
    }
    assert "\x1b" not in result.stderr


def test_workspace_and_line_manual_flow(config_file: Path) -> None:
    initialized = runner.invoke(
        app,
        ["workspace", "init", "testowy", f"config={config_file}"],
    )
    assert initialized.exit_code == 0
    workspace_id = initialized.stdout.strip()
    workspace_path = config_file.parent / "workspaces" / workspace_id
    assert (workspace_path / "index.sqlite3").is_file()
    assert not (config_file.parent / "index.sqlite3").exists()

    _set_active_workspace(config_file, "testowy")
    generated = runner.invoke(app, ["graph", "generate", f"config={config_file}"])
    assert generated.exit_code == 0
    assert "generated 6 graphs" in generated.stdout

    created_line = runner.invoke(app, ["line", "create", f"config={config_file}"])
    assert created_line.exit_code == 0
    line_id = created_line.stdout.strip()

    listed = runner.invoke(app, ["line", "list", f"config={config_file}"])
    assert listed.exit_code == 0
    assert "Workspace: testowy" in listed.stdout
    assert line_id in listed.stdout
    assert listed.stdout.count("*") == 1

    explicit_status = runner.invoke(
        app,
        ["line", "status", line_id, f"config={config_file}"],
    )
    assert explicit_status.exit_code == 0
    assert line_id in explicit_status.stdout
    assert "latest in workspace" not in explicit_status.stdout

    implicit_status = runner.invoke(
        app,
        ["line", "status", f"config={config_file}"],
    )
    assert implicit_status.exit_code == 0
    assert f"{line_id} (latest in workspace testowy)" in implicit_status.stdout


def test_baseline_rich_and_json_outputs_share_semantics(
    app_config: AppConfig,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "baseline-output")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    _set_active_workspace(config_file, "baseline-output")
    _install_fake_evaluator(
        monkeypatch,
        RationalInterval(Fraction(1, 3), Fraction(1, 2)),
    )

    json_result = runner.invoke(
        app,
        ["baseline", "evaluate", line.display, "--json", f"config={config_file}"],
    )
    rich_result = runner.invoke(
        app,
        ["baseline", "evaluate", line.display, f"config={config_file}"],
    )

    payload = json.loads(json_result.stdout)
    rich_text = Text.from_ansi(rich_result.stdout).plain
    assert json_result.exit_code == rich_result.exit_code == 0
    assert payload["baseline"] in rich_text
    assert payload["line"]["id"] in rich_text
    assert str(payload["graphs"]) in rich_text
    assert payload["score"]["fitness"] == {
        "lower": {"numerator": 1, "denominator": 3},
        "upper": {"numerator": 1, "denominator": 2},
    }
    assert "[0.33333, 0.50000]" in rich_text
    assert "Score width" in rich_text
    assert "0.16667" in rich_text
    assert "Exact" in rich_text
    assert "no" in rich_text
    assert "1 / 2 (50.0%)" in rich_text
    assert "Episodes" in rich_text
    assert "Proposals" in rich_text
    assert "Score calls" in rich_text
    assert "Episode rate" in rich_text
    assert "graphs/s" in rich_text
    assert "Proposal rate" in rich_text
    assert "proposals/s" in rich_text
    assert "Score rate" in rich_text
    assert "calls/s" in rich_text
    assert "Throughput" not in rich_text
    assert "1/3" not in rich_text
    assert payload["database"] in rich_text


def test_workspace_reindex_only_repairs_selected_workspace(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    workspace_a = create_workspace(app_config, "workspace-a")
    workspace_b = create_workspace(app_config, "workspace-b")
    generate_workspace_graphs(app_config, workspace_a.display)
    generate_workspace_graphs(app_config, workspace_b.display)
    create_line(app_config, workspace_a.display)
    create_line(app_config, workspace_b.display)

    path_a = app_config.workspace.root / workspace_a.display
    path_b = app_config.workspace.root / workspace_b.display
    database_a = database_path(path_a)
    database_b = database_path(path_b)
    before_b = database_b.read_bytes()
    delete_database(database_a)

    result = runner.invoke(
        app,
        [
            "workspace",
            "reindex",
            f"workspace={workspace_a.display}",
            f"config={config_file}",
        ],
    )
    assert result.exit_code == 0
    assert database_a.is_file()
    assert database_b.read_bytes() == before_b
    assert not (app_config.project_root / "index.sqlite3").exists()


def test_missing_local_index_does_not_trigger_filesystem_fallback(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    workspace = create_workspace(app_config, "missing-local")
    generate_workspace_graphs(app_config, workspace.display)
    create_line(app_config, workspace.display)
    _set_active_workspace(config_file, "missing-local")
    delete_database(database_path(app_config.workspace.root / workspace.display))

    result = runner.invoke(app, ["line", "list", f"config={config_file}"])
    assert result.exit_code == 2
    assert "workspace index is missing or stale" in result.stderr

    # Workspace listing itself is top-level discovery and remains available.
    listed = runner.invoke(app, ["workspace", "list", f"config={config_file}"])
    assert listed.exit_code == 0
    assert "missing-local" in listed.stdout


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
    assert not (
        app_config.workspace.root / workspace_a.display / "graphs" / "manifest.json"
    ).exists()
    manifest = read_json(
        app_config.workspace.root / workspace_b.display / "graphs" / "manifest.json"
    )
    assert manifest["graph_count"] == 4


def test_no_active_workspace_fails_even_with_one_workspace(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    create_workspace(app_config, "only")
    result = runner.invoke(app, ["workspace", "status", f"config={config_file}"])
    assert result.exit_code == 2
    assert "no workspace selected" in result.stderr


def test_line_list_handles_an_empty_workspace(
    app_config: AppConfig,
    config_file: Path,
) -> None:
    create_workspace(app_config, "empty")
    _set_active_workspace(config_file, "empty")
    result = runner.invoke(app, ["line", "list", f"config={config_file}"])
    assert result.exit_code == 0
    assert "No lines in workspace empty." in result.stdout


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


def test_line_status_without_a_line_requires_a_workspace(config_file: Path) -> None:
    result = runner.invoke(app, ["line", "status", f"config={config_file}"])
    assert result.exit_code == 2
    assert "no workspace selected" in result.stderr


def _set_active_workspace(path: Path, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.startswith("active =")]
    workspace_section = lines.index("[workspace]")
    lines.insert(workspace_section + 2, f'active = "{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _install_fake_evaluator(
    monkeypatch: pytest.MonkeyPatch,
    score: RationalInterval | None = None,
) -> None:
    evaluation_score = score or RationalInterval(Fraction(1, 2), Fraction(1, 2))

    class FakeEvaluator:
        def evaluate(self, graphs, _policy):  # type: ignore[no-untyped-def]
            return EvaluationResult(
                evaluation_score,
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
