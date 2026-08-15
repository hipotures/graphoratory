import tomllib
from pathlib import Path

import pytest

from graphoratory.config import load_config
from graphoratory.errors import ConfigurationError


def test_project_targets_python_312() -> None:
    project_root = Path(__file__).parents[1]
    assert (project_root / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    with (project_root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    assert project["project"]["requires-python"] == ">=3.12,<3.13"


def test_root_default_config_loads() -> None:
    config = load_config(Path(__file__).parents[1] / "experiment.toml")
    assert config.workspace.active
    assert config.graphs.workspace_graph_count == 1000
    assert config.graphs.min_order == 22
    assert config.graphs.max_order == 63


def test_nested_overrides_are_applied(config_file: Path) -> None:
    config = load_config(
        config_file,
        [
            "workspace.active=heg-test",
            "graphs.workspace_graph_count=9",
            "graphs.min_order=12",
            "workspace.root=elsewhere",
        ],
    )
    assert config.workspace.active == "heg-test"
    assert config.graphs.workspace_graph_count == 9
    assert config.graphs.min_order == 12
    assert config.workspace.root == (config_file.parent / "elsewhere").resolve()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("graphs.missing=1", "unknown override key"),
        ("graphs.count=1", "unknown override key"),
        ("graphs.line_sample_size=1", "unknown override key"),
        ("active_workspace=heg-test", "unknown override key"),
        ("workspace.active=", "workspace.active must not be empty"),
        (
            "graphs.workspace_graph_count=0",
            "graphs.workspace_graph_count must be positive",
        ),
        ("graphs.min_order=3", "graphs.min_order must be at least 4"),
        ("graphs.min_order=14", "graphs.max_order must be at least"),
    ],
)
def test_invalid_overrides_fail(config_file: Path, override: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config(config_file, [override])
