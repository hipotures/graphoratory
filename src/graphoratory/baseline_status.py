from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from graphoratory.application import (
    BaselineEvaluationResult,
    CommandLineSelection,
    resolve_line_for_command,
)
from graphoratory.artifacts import EVALUATIONS_DIRECTORY, evaluation_manifest_hash
from graphoratory.config import AppConfig
from graphoratory.database.core import database_path
from graphoratory.database.queries import EvaluationRow, latest_evaluation
from graphoratory.errors import ArtifactError
from graphoratory.jsonio import read_json
from graphoratory.science.baseline import SUPPORTED_BASELINES, baseline_for_selector


def get_baseline_status(
    config: AppConfig,
    line_value: str | None = None,
    workspace_value: str | None = None,
    baseline_selector: str = "random",
) -> BaselineEvaluationResult:
    selection = resolve_line_for_command(config, line_value, workspace_value)
    database = database_path(selection.workspace.path)
    return _stored_result(selection, database, baseline_selector)


def get_baselines_status(
    config: AppConfig,
    line_value: str | None = None,
    workspace_value: str | None = None,
) -> tuple[BaselineEvaluationResult, ...]:
    selection = resolve_line_for_command(config, line_value, workspace_value)
    database = database_path(selection.workspace.path)
    return tuple(
        _stored_result(selection, database, selector)
        for selector in SUPPORTED_BASELINES
    )


def _stored_result(
    selection: CommandLineSelection,
    database: Path,
    selector: str,
) -> BaselineEvaluationResult:
    baseline = baseline_for_selector(selector)
    row = latest_evaluation(database, selection.identifier.digest, baseline.name)
    if row is None:
        raise ArtifactError(
            f"no stored {selector} baseline evaluation for {selection.identifier.display}; "
            f"run `graphlab baseline evaluate baseline={selector}`"
        )
    if (
        row.workspace_hash != selection.workspace.identifier.digest
        or row.line_hash != selection.identifier.digest
    ):
        raise ArtifactError(
            "workspace index is missing or stale; run `graphlab workspace reindex`"
        )
    manifest = _read_exact_artifact(selection, row, selector, baseline.name)
    score = _dict_field(manifest, "score")
    diagnostics = _dict_field(manifest, "diagnostics")
    resources = _dict_field(manifest, "resources")
    return BaselineEvaluationResult(
        evaluation_hash=row.evaluation_hash,
        baseline_selector=selector,
        baseline=baseline.name,
        line=selection.identifier,
        workspace=selection.workspace.identifier,
        workspace_name=selection.workspace.name,
        graph_count=row.graph_count,
        score=score,
        diagnostics=diagnostics,
        wall_seconds=_float_field(resources, "wall_seconds"),
        graphs_per_second=_float_field(resources, "graphs_per_second"),
        peak_rss_bytes=_int_field(resources, "peak_rss_bytes"),
        database_state="indexed",
        selected_latest=selection.selected_latest,
    )


def _read_exact_artifact(
    selection: CommandLineSelection,
    row: EvaluationRow,
    selector: str,
    baseline_name: str,
) -> dict[str, Any]:
    path = (
        selection.line_path
        / EVALUATIONS_DIRECTORY
        / f"{row.evaluation_hash}.json"
    )
    if not path.is_file():
        raise ArtifactError(
            "stored evaluation artifact is missing; run `graphlab workspace reindex`"
        )
    try:
        manifest = read_json(path)
        baseline = _dict_field(manifest, "baseline")
        if manifest.get("artifact_type") != "baseline_evaluation":
            raise ValueError("artifact_type must be baseline_evaluation")
        if manifest.get("evaluation_hash") != row.evaluation_hash:
            raise ValueError("evaluation hash disagrees with SQLite")
        if evaluation_manifest_hash(manifest) != row.evaluation_hash:
            raise ValueError("evaluation hash does not match its payload")
        if manifest.get("workspace_hash") != selection.workspace.identifier.digest:
            raise ValueError("evaluation belongs to another workspace")
        if manifest.get("line_hash") != selection.identifier.digest:
            raise ValueError("evaluation belongs to another line")
        if baseline.get("selector") != selector or baseline.get("name") != baseline_name:
            raise ValueError("evaluation baseline disagrees with SQLite selection")
        if manifest.get("graph_count") != row.graph_count:
            raise ValueError("evaluation graph count disagrees with SQLite")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactError(f"invalid evaluation artifact: {path}") from exc
    return manifest


def _dict_field(manifest: dict[str, Any], field: str) -> dict[str, object]:
    value = manifest[field]
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _float_field(payload: dict[str, object], field: str) -> float:
    value = payload[field]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field} must be numeric")
    return float(value)


def _int_field(payload: dict[str, object], field: str) -> int:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    return value
