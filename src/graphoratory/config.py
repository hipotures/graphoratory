from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Any, cast

from graphoratory.errors import ConfigurationError

CONCRETE_GENERATORS = (
    "cycle_matching_stub_pairing",
    "random_regular",
    "erdos_renyi_rejection",
    "degree_sequence_rejection",
)
GENERATOR_NAMES = (*CONCRETE_GENERATORS, "mixed")


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path
    active: str | None


@dataclass(frozen=True, slots=True)
class DegreeRangeConfig:
    degree_min: int
    degree_max: int


@dataclass(frozen=True, slots=True)
class ErdosRenyiConfig:
    expected_degree_min: float
    expected_degree_max: float


@dataclass(frozen=True, slots=True)
class MixedGeneratorConfig:
    generators: tuple[str, ...]
    weights: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class GraphConfig:
    generator: str
    workspace_graph_count: int
    line_graph_count: int
    min_order: int
    max_order: int
    seed: int
    random_regular: DegreeRangeConfig
    erdos_renyi_rejection: ErdosRenyiConfig
    degree_sequence_rejection: DegreeRangeConfig
    mixed: MixedGeneratorConfig


@dataclass(frozen=True, slots=True)
class AppConfig:
    workspace: WorkspaceConfig
    graphs: GraphConfig
    source: Path


_OVERRIDE_TYPES: dict[str, type[str] | type[int] | type[float]] = {
    "workspace.root": str,
    "workspace.active": str,
    "graphs.generator": str,
    "graphs.workspace_graph_count": int,
    "graphs.line_graph_count": int,
    "graphs.min_order": int,
    "graphs.max_order": int,
    "graphs.seed": int,
    "graphs.random_regular.degree_min": int,
    "graphs.random_regular.degree_max": int,
    "graphs.erdos_renyi_rejection.expected_degree_min": float,
    "graphs.erdos_renyi_rejection.expected_degree_max": float,
    "graphs.degree_sequence_rejection.degree_min": int,
    "graphs.degree_sequence_rejection.degree_max": int,
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
        regular_raw = graphs_raw["random_regular"]
        erdos_renyi_raw = graphs_raw["erdos_renyi_rejection"]
        degree_sequence_raw = graphs_raw["degree_sequence_rejection"]
        mixed_raw = graphs_raw["mixed"]
        config = AppConfig(
            workspace=WorkspaceConfig(
                root=_root_path(source, str(workspace_raw["root"])),
                active=_optional_string(workspace_raw.get("active"), "workspace.active"),
            ),
            graphs=GraphConfig(
                generator=str(graphs_raw["generator"]),
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
                random_regular=DegreeRangeConfig(
                    degree_min=_strict_int(
                        regular_raw["degree_min"],
                        "graphs.random_regular.degree_min",
                    ),
                    degree_max=_strict_int(
                        regular_raw["degree_max"],
                        "graphs.random_regular.degree_max",
                    ),
                ),
                erdos_renyi_rejection=ErdosRenyiConfig(
                    expected_degree_min=_strict_float(
                        erdos_renyi_raw["expected_degree_min"],
                        "graphs.erdos_renyi_rejection.expected_degree_min",
                    ),
                    expected_degree_max=_strict_float(
                        erdos_renyi_raw["expected_degree_max"],
                        "graphs.erdos_renyi_rejection.expected_degree_max",
                    ),
                ),
                degree_sequence_rejection=DegreeRangeConfig(
                    degree_min=_strict_int(
                        degree_sequence_raw["degree_min"],
                        "graphs.degree_sequence_rejection.degree_min",
                    ),
                    degree_max=_strict_int(
                        degree_sequence_raw["degree_max"],
                        "graphs.degree_sequence_rejection.degree_max",
                    ),
                ),
                mixed=MixedGeneratorConfig(
                    generators=_strict_string_tuple(
                        mixed_raw["generators"], "graphs.mixed.generators"
                    ),
                    weights=_strict_float_tuple(
                        mixed_raw["weights"], "graphs.mixed.weights"
                    ),
                ),
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
            "generator",
            "workspace_graph_count",
            "line_graph_count",
            "min_order",
            "max_order",
            "seed",
            "random_regular",
            "erdos_renyi_rejection",
            "degree_sequence_rejection",
            "mixed",
        },
    }.items():
        value = raw.get(section)
        if not isinstance(value, dict):
            raise ConfigurationError(f"configuration section {section!r} must be a table")
        unknown_keys = set(value) - keys
        if unknown_keys:
            key = sorted(unknown_keys)[0]
            raise ConfigurationError(f"unknown configuration key: {section}.{key}")
    graphs = cast(dict[str, Any], raw["graphs"])
    for section, keys in {
        "random_regular": {"degree_min", "degree_max"},
        "erdos_renyi_rejection": {
            "expected_degree_min",
            "expected_degree_max",
        },
        "degree_sequence_rejection": {"degree_min", "degree_max"},
        "mixed": {"generators", "weights"},
    }.items():
        value = graphs.get(section)
        if not isinstance(value, dict):
            raise ConfigurationError(f"configuration section 'graphs.{section}' must be a table")
        unknown_keys = set(value) - keys
        if unknown_keys:
            key = sorted(unknown_keys)[0]
            raise ConfigurationError(f"unknown configuration key: graphs.{section}.{key}")


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
    if key == "graphs.generator":
        return replace(config, graphs=replace(config.graphs, generator=text))
    try:
        value: int | float = int(text) if value_type is int else float(text)
    except ValueError as exc:
        kind = "an integer" if value_type is int else "a number"
        raise ConfigurationError(f"{key} must be {kind}") from exc

    if key == "graphs.workspace_graph_count":
        return replace(
            config,
            graphs=replace(config.graphs, workspace_graph_count=cast(int, value)),
        )
    if key == "graphs.line_graph_count":
        return replace(config, graphs=replace(config.graphs, line_graph_count=cast(int, value)))
    if key == "graphs.min_order":
        return replace(config, graphs=replace(config.graphs, min_order=cast(int, value)))
    if key == "graphs.max_order":
        return replace(config, graphs=replace(config.graphs, max_order=cast(int, value)))
    if key == "graphs.seed":
        return replace(config, graphs=replace(config.graphs, seed=cast(int, value)))
    if key.startswith("graphs.random_regular."):
        field = key.rsplit(".", 1)[1]
        return replace(
            config,
            graphs=replace(
                config.graphs,
                random_regular=replace(
                    config.graphs.random_regular,
                    **{field: cast(int, value)},
                ),
            ),
        )
    if key.startswith("graphs.erdos_renyi_rejection."):
        field = key.rsplit(".", 1)[1]
        return replace(
            config,
            graphs=replace(
                config.graphs,
                erdos_renyi_rejection=replace(
                    config.graphs.erdos_renyi_rejection,
                    **{field: cast(float, value)},
                ),
            ),
        )
    if key.startswith("graphs.degree_sequence_rejection."):
        field = key.rsplit(".", 1)[1]
        return replace(
            config,
            graphs=replace(
                config.graphs,
                degree_sequence_rejection=replace(
                    config.graphs.degree_sequence_rejection,
                    **{field: cast(int, value)},
                ),
            ),
        )
    raise AssertionError(f"unhandled override key: {key}")


def _root_path(source: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (source.parent / path).resolve()


def _strict_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{key} must be an integer")
    return cast(int, value)


def _strict_float(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{key} must be a number")
    result = float(value)
    if not isfinite(result):
        raise ConfigurationError(f"{key} must be finite")
    return result


def _strict_string_tuple(value: Any, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be an array of strings")
    return tuple(value)


def _strict_float_tuple(value: Any, key: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{key} must be an array of numbers")
    return tuple(_strict_float(item, key) for item in value)


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
    if graphs.generator not in GENERATOR_NAMES:
        valid = ", ".join(GENERATOR_NAMES)
        raise ConfigurationError(
            f"unknown graphs.generator {graphs.generator!r}; valid generators: {valid}"
        )
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
    _validate_degree_range(
        graphs.random_regular,
        "graphs.random_regular",
    )
    if not any(
        degree < order and (order * degree) % 2 == 0
        for order in range(graphs.min_order, graphs.max_order + 1)
        for degree in range(
            graphs.random_regular.degree_min,
            graphs.random_regular.degree_max + 1,
        )
    ):
        raise ConfigurationError(
            "graphs.random_regular has no feasible (order, degree) pair in the order range"
        )
    erdos_renyi = graphs.erdos_renyi_rejection
    if erdos_renyi.expected_degree_min <= 0:
        raise ConfigurationError(
            "graphs.erdos_renyi_rejection.expected_degree_min must be positive"
        )
    if erdos_renyi.expected_degree_max < erdos_renyi.expected_degree_min:
        raise ConfigurationError(
            "graphs.erdos_renyi_rejection.expected_degree_max must be at least "
            "expected_degree_min"
        )
    if erdos_renyi.expected_degree_min > graphs.max_order - 1:
        raise ConfigurationError(
            "graphs.erdos_renyi_rejection has no feasible expected degree in the order range"
        )
    _validate_degree_range(
        graphs.degree_sequence_rejection,
        "graphs.degree_sequence_rejection",
    )
    if not any(
        graphs.degree_sequence_rejection.degree_min
        < min(graphs.degree_sequence_rejection.degree_max, order - 1)
        for order in range(graphs.min_order, graphs.max_order + 1)
    ):
        raise ConfigurationError(
            "graphs.degree_sequence_rejection has no feasible heterogeneous degree range"
        )
    mixed = graphs.mixed
    if not mixed.generators:
        raise ConfigurationError("graphs.mixed.generators must not be empty")
    if len(mixed.generators) != len(mixed.weights):
        raise ConfigurationError(
            "graphs.mixed.generators and graphs.mixed.weights must have the same length"
        )
    invalid = [name for name in mixed.generators if name not in CONCRETE_GENERATORS]
    if invalid:
        valid = ", ".join(CONCRETE_GENERATORS)
        raise ConfigurationError(
            f"unknown mixed generator {invalid[0]!r}; valid generators: {valid}"
        )
    if len(set(mixed.generators)) != len(mixed.generators):
        raise ConfigurationError("graphs.mixed.generators must not contain duplicates")
    if any(weight <= 0 for weight in mixed.weights):
        raise ConfigurationError("graphs.mixed.weights must all be positive")
    if not isfinite(sum(mixed.weights)):
        raise ConfigurationError("graphs.mixed.weights must have a finite total")


def _validate_degree_range(config: DegreeRangeConfig, key: str) -> None:
    if config.degree_min < 3:
        raise ConfigurationError(f"{key}.degree_min must be at least 3")
    if config.degree_max < config.degree_min:
        raise ConfigurationError(f"{key}.degree_max must be at least degree_min")
