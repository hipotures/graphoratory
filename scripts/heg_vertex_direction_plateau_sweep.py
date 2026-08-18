#!/usr/bin/env python3
"""
Paired HEG plateau sweep for the direction of vertex-distance selection.

Measured arms:

    baseline
    vertex-near   minimize the existing vertex-mean cycle geometry
    vertex-far    maximize the exact same geometry

Defaults deliberately use the harder conditioned plateaus that exposed opposite
behavior of vertex-near across orders:

    n=10: F=8 -> 4
    n=11: F=6 -> 2

Every repeat uses the existing conditioned-plateau benchmark machinery: one
common warm-up graph per repeat, the same search seed for every arm, and
ELITE-only measured parent selection. Only the direction of the vertex metric
changes between vertex-near and vertex-far.

Example:

    uv run python scripts/heg_vertex_direction_plateau_sweep.py \
      --repeats 20 --seconds-per-run 10 --workers 16 \
      --output-dir results/sweeps/vertex_direction_20x
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


DIRECTION_METRICS = ("baseline", "vertex-near", "vertex-far")
DIRECTION_CASES = ((10, 8, 4), (11, 6, 2))


def _load_sweep_module() -> Any:
    path = Path(__file__).with_name("heg_compactness_plateau_sweep.py")
    if not path.is_file():
        raise RuntimeError(f"required sibling script is missing: {path}")
    spec = importlib.util.spec_from_file_location(
        "heg_compactness_plateau_sweep_direction_base", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _has_option(name: str) -> bool:
    return any(arg == name or arg.startswith(name + "=") for arg in sys.argv[1:])


def main() -> int:
    sweep = _load_sweep_module()

    # parse_metrics() and main() consult these globals at runtime, so the
    # existing paired benchmark can be reused without changing its mechanics.
    sweep.METRICS = DIRECTION_METRICS
    sweep.DEFAULT_CASES = DIRECTION_CASES

    if not _has_option("--child-script"):
        child = Path(__file__).with_name("heg_vertex_direction_plateau_mutator.py").resolve()
        sys.argv.extend(["--child-script", str(child)])

    return int(sweep.main())


if __name__ == "__main__":
    raise SystemExit(main())
