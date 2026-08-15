from pathlib import Path

import pytest

from graphoratory.config import AppConfig, load_config


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "experiment.toml"
    path.write_text(
        """
[workspace]
root = "workspaces"

[graphs]
generator = "mixed"
workspace_graph_count = 6
line_graph_count = 2
min_order = 10
max_order = 13
seed = 401

[graphs.random_regular]
degree_min = 3
degree_max = 5

[graphs.erdos_renyi_rejection]
expected_degree_min = 5.0
expected_degree_max = 8.0

[graphs.degree_sequence_rejection]
degree_min = 3
degree_max = 6

[graphs.mixed]
generators = [
    "cycle_matching_stub_pairing",
    "random_regular",
    "erdos_renyi_rejection",
    "degree_sequence_rejection",
]
weights = [1.0, 1.0, 1.0, 1.0]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app_config(config_file: Path) -> AppConfig:
    return load_config(config_file)
