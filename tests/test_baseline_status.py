import json
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest
from typer.testing import CliRunner

import graphoratory.application as application
from graphoratory.application import create_line, create_workspace, generate_workspace_graphs
from graphoratory.cli import app
from graphoratory.config import AppConfig
from graphoratory.database.core import database_path, projection_counts
from graphoratory.science.evaluator import (
    EvaluationDiagnostics,
    EvaluationResult,
    RationalInterval,
)

runner = CliRunner()


def test_baseline_status_reads_latest_artifacts_without_recomputation(
    app_config: AppConfig,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "baseline-status")
    generate_workspace_graphs(app_config, workspace.display)
    line = create_line(app_config, workspace.display)
    _set_active_workspace(config_file, "baseline-status")
    _install_fake_evaluator(monkeypatch)

    evaluated = runner.invoke(
        app,
        ["baseline", "evaluate", "--json", f"config={config_file}"],
    )
    assert evaluated.exit_code == 0
    evaluated_payload = json.loads(evaluated.stdout)
    database = database_path(app_config.workspace.root / workspace.display)
    assert projection_counts(database)["evaluations"] == 2  # type: ignore[index]

    class ExplodingEvaluator:
        def evaluate(self, graphs, policy):  # type: ignore[no-untyped-def]
            raise AssertionError("baseline status must not run the evaluator")

    monkeypatch.setattr(application, "IndependentEvaluator", ExplodingEvaluator)
    status = runner.invoke(
        app,
        ["baseline", "status", "--json", f"config={config_file}"],
    )
    assert status.exit_code == 0
    assert json.loads(status.stdout) == evaluated_payload
    assert projection_counts(database)["evaluations"] == 2  # type: ignore[index]
    assert all(
        result["line"]["id"] == line.display
        for result in evaluated_payload["baselines"]
    )


def test_baseline_status_supports_selector_and_reports_missing_result(
    app_config: AppConfig,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_workspace(app_config, "baseline-status-selector")
    generate_workspace_graphs(app_config, workspace.display)
    first_line = create_line(app_config, workspace.display)
    _set_active_workspace(config_file, "baseline-status-selector")
    _install_fake_evaluator(monkeypatch)

    evaluated = runner.invoke(
        app,
        [
            "baseline",
            "evaluate",
            "--json",
            "baseline=random",
            f"line={first_line.display}",
            f"config={config_file}",
        ],
    )
    assert evaluated.exit_code == 0
    status = runner.invoke(
        app,
        [
            "baseline",
            "status",
            "--json",
            "baseline=random",
            f"line={first_line.display}",
            f"config={config_file}",
        ],
    )
    assert status.exit_code == 0
    assert json.loads(status.stdout) == json.loads(evaluated.stdout)

    second_line = create_line(app_config, workspace.display)
    missing = runner.invoke(
        app,
        [
            "baseline",
            "status",
            "--json",
            "baseline=random",
            f"line={second_line.display}",
            f"config={config_file}",
        ],
    )
    assert missing.exit_code == 2
    error = json.loads(missing.stderr)["error"]
    assert error["type"] == "ArtifactError"
    assert "no stored random baseline evaluation" in error["message"]


def _set_active_workspace(path: Path, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line for line in lines if not line.startswith("active =")]
    workspace_section = lines.index("[workspace]")
    lines.insert(workspace_section + 2, f'active = "{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _install_fake_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEvaluator:
        def evaluate(self, graphs, policy):  # type: ignore[no-untyped-def]
            return EvaluationResult(
                RationalInterval(Fraction(1, 3), Fraction(1, 2)),
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
