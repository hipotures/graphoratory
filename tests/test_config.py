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
    assert config.active_workspace == "testowy"
    assert config.graphs.count == 1000
    assert config.graphs.min_order == 22
    assert config.graphs.max_order == 63


def test_nested_overrides_are_applied(config_file: Path) -> None:
    config = load_config(
        config_file,
        [
            "active_workspace=heg-test",
            "graphs.count=9",
            "graphs.min_order=12",
            "workspace.root=elsewhere",
        ],
    )
    assert config.active_workspace == "heg-test"
    assert config.graphs.count == 9
    assert config.graphs.min_order == 12
    assert config.workspace.root == (config_file.parent / "elsewhere").resolve()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("graphs.missing=1", "unknown override key"),
        ("active_workspace=", "active_workspace must not be empty"),
        ("graphs.count=0", "graphs.count must be positive"),
        ("graphs.min_order=3", "graphs.min_order must be at least 4"),
        ("graphs.min_order=14", "graphs.max_order must be at least"),
    ],
)
def test_invalid_overrides_fail(config_file: Path, override: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config(config_file, [override])
