from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from graphoratory.errors import ConfigurationError

GRAPH_MODE = "unrestricted_min_degree_3"


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path
    active: str | None


@dataclass(frozen=True, slots=True)
class GraphConfig:
    mode: str
    workspace_graph_count: int
    line_graph_count: int
    min_order: int
    max_order: int
    seed: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    workspace: WorkspaceConfig
    graphs: GraphConfig
    source: Path


_OVERRIDE_TYPES: dict[str, type[str] | type[int]] = {
    "workspace.root": str,
    "workspace.active": str,
    "graphs.mode": str,
    "graphs.workspace_graph_count": int,
    "graphs.line_graph_count": int,
    "graphs.min_order": int,
    "graphs.max_order": int,
    "graphs.seed": int,
}


def load_config(path: Path, overrides: list[str] | None = None) -> AppConfig:
    source = path.resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot load configuration {source}: {exc}") from exc

    _validate_sections(raw)
    try:
        workspace_raw = raw["workspace"]
        graphs_raw = raw["graphs"]
        config = AppConfig(
            workspace=WorkspaceConfig(
                root=_root_path(source, str(workspace_raw["root"])),
                active=_optional_string(workspace_raw.get("active"), "workspace.active"),
            ),
            graphs=GraphConfig(
                mode=str(graphs_raw["mode"]),
                workspace_graph_count=_strict_int(
                    graphs_raw["workspace_graph_count"],
                    "graphs.workspace_graph_count",
                ),
                line_graph_count=_strict_int(
                    graphs_raw["line_graph_count"], "graphs.line_graph_count"
                ),
                min_order=_strict_int(graphs_raw["min_order"], "graphs.min_order"),
                max_order=_strict_int(graphs_raw["max_order"], "graphs.max_order"),
                seed=_strict_int(graphs_raw["seed"], "graphs.seed"),
            ),
            source=source,
        )
    except KeyError as exc:
        raise ConfigurationError(f"missing configuration key: {exc.args[0]}") from exc

    for override in overrides or []:
        config = _apply_override(config, override)
    _validate(config)
    return config


def _validate_sections(raw: dict[str, Any]) -> None:
    expected = {"workspace", "graphs"}
    unknown = set(raw) - expected
    if unknown:
        raise ConfigurationError(f"unknown configuration section: {sorted(unknown)[0]}")
    for section, keys in {
        "workspace": {"root", "active"},
        "graphs": {
            "mode",
            "workspace_graph_count",
            "line_graph_count",
            "min_order",
            "max_order",
            "seed",
        },
    }.items():
        value = raw.get(section)
        if not isinstance(value, dict):
            raise ConfigurationError(f"configuration section {section!r} must be a table")
        unknown_keys = set(value) - keys
        if unknown_keys:
            key = sorted(unknown_keys)[0]
            raise ConfigurationError(f"unknown configuration key: {section}.{key}")


def _apply_override(config: AppConfig, override: str) -> AppConfig:
    if "=" not in override:
        raise ConfigurationError(f"override must use key=value syntax: {override!r}")
    key, text = override.split("=", 1)
    value_type = _OVERRIDE_TYPES.get(key)
    if value_type is None:
        raise ConfigurationError(f"unknown override key: {key}")
    if key == "workspace.root":
        return replace(
            config,
            workspace=replace(config.workspace, root=_root_path(config.source, text)),
        )
    if key == "workspace.active":
        return replace(config, workspace=replace(config.workspace, active=text))
    if key == "graphs.mode":
        return replace(config, graphs=replace(config.graphs, mode=text))
    try:
        value = int(text)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer") from exc

    if key == "graphs.workspace_graph_count":
        return replace(config, graphs=replace(config.graphs, workspace_graph_count=value))
    if key == "graphs.line_graph_count":
        return replace(config, graphs=replace(config.graphs, line_graph_count=value))
    if key == "graphs.min_order":
        return replace(config, graphs=replace(config.graphs, min_order=value))
    if key == "graphs.max_order":
        return replace(config, graphs=replace(config.graphs, max_order=value))
    if key == "graphs.seed":
        return replace(config, graphs=replace(config.graphs, seed=value))
    raise AssertionError(f"unhandled override key: {key}")


def _root_path(source: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (source.parent / path).resolve()


def _strict_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{key} must be an integer")
    return cast(int, value)


def _optional_string(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be a string")
    return value


def _validate(config: AppConfig) -> None:
    graphs = config.graphs
    if config.workspace.active is not None and not config.workspace.active.strip():
        raise ConfigurationError("workspace.active must not be empty")
    if not str(config.workspace.root):
        raise ConfigurationError("workspace.root must not be empty")
    if graphs.mode != GRAPH_MODE:
        raise ConfigurationError(f"graphs.mode must be {GRAPH_MODE!r}")
    if graphs.workspace_graph_count < 1:
        raise ConfigurationError("graphs.workspace_graph_count must be positive")
    if graphs.line_graph_count < 1:
        raise ConfigurationError("graphs.line_graph_count must be positive")
    if graphs.min_order < 4:
        raise ConfigurationError("graphs.min_order must be at least 4")
    if graphs.max_order < graphs.min_order:
        raise ConfigurationError("graphs.max_order must be at least graphs.min_order")
    if graphs.seed < 0:
        raise ConfigurationError("graphs.seed must be non-negative")
