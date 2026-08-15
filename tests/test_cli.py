import sys
from pathlib import Path

import pytest

from graphoratory.cli import main


def test_line_status_requires_an_explicit_line(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["graphlab", "line", "status", f"config={config_file}"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "missing required parameter: line=..." in capsys.readouterr().err
