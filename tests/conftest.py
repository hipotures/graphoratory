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
mode = "unrestricted_min_degree_3"
count = 6
line_sample_size = 2
min_order = 10
max_order = 13
seed = 401
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app_config(config_file: Path) -> AppConfig:
    return load_config(config_file)
