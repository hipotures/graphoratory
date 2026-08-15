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
    assert config.graphs.generator == "mixed"
    assert config.graphs.workspace_graph_count == 1000
    assert config.graphs.min_order == 22
    assert config.graphs.max_order == 63


def test_missing_config_error_uses_project_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "configs" / "missing.toml"
    with pytest.raises(ConfigurationError) as error:
        load_config(Path("configs/missing.toml"))
    assert "$PROJECT/configs/missing.toml" in str(error.value)
    assert str(missing) not in str(error.value)


def test_nested_overrides_are_applied(config_file: Path) -> None:
    config = load_config(
        config_file,
        [
            "workspace.active=heg-test",
            "graphs.workspace_graph_count=9",
            "graphs.min_order=12",
            "graphs.generator=random_regular",
            "graphs.random_regular.degree_max=4",
            "graphs.erdos_renyi_rejection.expected_degree_min=7.5",
            "workspace.root=elsewhere",
        ],
    )
    assert config.workspace.active == "heg-test"
    assert config.graphs.workspace_graph_count == 9
    assert config.graphs.min_order == 12
    assert config.graphs.generator == "random_regular"
    assert config.graphs.random_regular.degree_max == 4
    assert config.graphs.erdos_renyi_rejection.expected_degree_min == 7.5
    assert config.workspace.root == (config_file.parent / "elsewhere").resolve()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("graphs.missing=1", "unknown override key"),
        ("graphs.count=1", "unknown override key"),
        ("graphs.line_sample_size=1", "unknown override key"),
        ("graphs.mode=unrestricted_min_degree_3", "unknown override key"),
        ("active_workspace=heg-test", "unknown override key"),
        ("workspace.active=", "workspace.active must not be empty"),
        (
            "graphs.workspace_graph_count=0",
            "graphs.workspace_graph_count must be positive",
        ),
        ("graphs.min_order=3", "graphs.min_order must be at least 4"),
        ("graphs.min_order=14", "graphs.max_order must be at least"),
        ("graphs.generator=unknown", "valid generators"),
        (
            "graphs.random_regular.degree_min=2",
            "degree_min must be at least 3",
        ),
        (
            "graphs.erdos_renyi_rejection.expected_degree_min=0",
            "expected_degree_min must be positive",
        ),
    ],
)
def test_invalid_overrides_fail(config_file: Path, override: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_config(config_file, [override])


def test_mixed_generator_configuration_is_validated(config_file: Path) -> None:
    text = config_file.read_text(encoding="utf-8")
    config_file.write_text(
        text.replace(
            "weights = [1.0, 1.0, 1.0, 1.0]",
            "weights = [1.0, 1.0]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must have the same length"):
        load_config(config_file)

    config_file.write_text(
        text.replace(
            '"degree_sequence_rejection",',
            '"not_registered",',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown mixed generator"):
        load_config(config_file)

    config_file.write_text(
        text.replace(
            "weights = [1.0, 1.0, 1.0, 1.0]",
            "weights = [1.0, 0.0, 1.0, 1.0]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="weights must all be positive"):
        load_config(config_file)

    config_file.write_text(
        text.replace(
            "weights = [1.0, 1.0, 1.0, 1.0]",
            "weights = [1e308, 1e308, 1e308, 1e308]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="finite total"):
        load_config(config_file)


def test_degree_sequence_requires_a_heterogeneous_feasible_range(
    config_file: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="no feasible heterogeneous degree range"):
        load_config(
            config_file,
            [
                "graphs.generator=degree_sequence_rejection",
                "graphs.min_order=4",
                "graphs.max_order=4",
                "graphs.erdos_renyi_rejection.expected_degree_min=3",
                "graphs.erdos_renyi_rejection.expected_degree_max=3",
                "graphs.degree_sequence_rejection.degree_min=3",
                "graphs.degree_sequence_rejection.degree_max=4",
            ],
        )
