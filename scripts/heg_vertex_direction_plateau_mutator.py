#!/usr/bin/env python3
"""
Directional vertex-distance adapter for the HEG compactness plateau mutator.

This script keeps the underlying mutation kernel, exact scorer, elite machinery,
and cycle enumeration unchanged. It adds two explicit direction labels over
exactly the existing vertex-mean geometry:

    vertex-near
        Minimize mean vertex-to-vertex distance between residual forbidden
        cycle pairs. This is identical to the historical ``vertex-mean`` arm.

    vertex-far
        Maximize that same mean distance. The implementation only negates the
        existing minimization energy; the geometry definition is otherwise
        identical.

Keeping the metric identical and changing only the sign makes near-vs-far a
clean directional experiment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence


ALIASES = {"vertex-near", "vertex-far"}


def _load_compactness_module() -> Any:
    path = Path(__file__).with_name("heg_compactness_plateau_mutator.py")
    if not path.is_file():
        raise RuntimeError(f"required sibling script is missing: {path}")
    spec = importlib.util.spec_from_file_location(
        "heg_compactness_plateau_mutator_direction_base", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _find_metric(argv: Sequence[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--compactness-metric":
            if index + 1 >= len(argv):
                return None
            return argv[index + 1]
        if arg.startswith("--compactness-metric="):
            return arg.split("=", 1)[1]
    return None


def _rewrite_metric(argv: Sequence[str], requested: str) -> list[str]:
    rewritten = list(argv)
    for index, arg in enumerate(rewritten):
        if arg == "--compactness-metric" and index + 1 < len(rewritten):
            rewritten[index + 1] = "vertex-mean"
            return rewritten
        if arg.startswith("--compactness-metric="):
            rewritten[index] = "--compactness-metric=vertex-mean"
            return rewritten
    raise RuntimeError(f"could not rewrite requested compactness metric {requested!r}")


def main() -> int:
    compact = _load_compactness_module()
    original_parse = compact._parse_adapter_args
    original_compute = compact._compute_geometry

    def parse_with_direction(argv: Sequence[str]):
        requested = _find_metric(argv)
        if requested not in ALIASES:
            return original_parse(argv)
        adapter, remaining = original_parse(_rewrite_metric(argv, requested))
        adapter.compactness_metric = requested
        return adapter, remaining

    def compute_with_direction(
        entry: Any,
        *,
        metric: str,
        node_budget: int,
    ):
        if metric not in ALIASES:
            return original_compute(entry, metric=metric, node_budget=node_budget)

        score = original_compute(entry, metric="vertex-mean", node_budget=node_budget)
        energy = score.energy if metric == "vertex-near" else -score.energy
        return compact.GeometryScore(
            metric=metric,
            energy=float(energy),
            cycle_count=score.cycle_count,
            pair_count=score.pair_count,
            enumeration_nodes=score.enumeration_nodes,
        )

    compact._parse_adapter_args = parse_with_direction
    compact._compute_geometry = compute_with_direction
    return int(compact.main())


if __name__ == "__main__":
    raise SystemExit(main())
