from __future__ import annotations

import hashlib
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sglab.score_worker import PersistentScoreWorker  # type: ignore[import-untyped]

from graphoratory.errors import EvaluationFailure

SCORE_PROTOCOL = "heg_bounded_cycle_count"
INITIAL_NODE_BUDGET = 50_000
EXPANDED_NODE_BUDGET = 200_000
INITIAL_TIMEOUT_SECONDS = 5.0
EXPANDED_TIMEOUT_SECONDS = 20.0
_BUILD_FLAGS = ("-O3", "-std=c++17", "-Wall", "-Wextra", "-Wpedantic")


class ScoreTimeoutWithoutPartial(EvaluationFailure):
    """A bounded HEG request timed out without sound partial evidence."""


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    source_sha256: str
    binary_sha256: str
    compiler: str
    build_flags: tuple[str, ...]
    platform: str
    architecture: str

    def payload(self) -> dict[str, object]:
        return {
            "score_protocol": SCORE_PROTOCOL,
            "source_sha256": self.source_sha256,
            "binary_sha256": self.binary_sha256,
            "compiler": self.compiler,
            "build_flags": list(self.build_flags),
            "platform": self.platform,
            "architecture": self.architecture,
        }


class ScoreWorker:
    def __init__(self) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._worker: Any | None = None
        self.identity: WorkerIdentity | None = None

    def __enter__(self) -> ScoreWorker:
        source = Path(__file__).with_name("sglab_score_worker.cpp")
        if not source.is_file():
            raise EvaluationFailure(f"bundled HEG score-worker source is missing: {source}")
        self._temporary = tempfile.TemporaryDirectory(prefix="graphoratory-score-worker-")
        binary = Path(self._temporary.name) / "sglab-score-worker"
        try:
            compiler = subprocess.run(
                ["c++", "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.splitlines()[0]
            subprocess.run(
                ["c++", *_BUILD_FLAGS, str(source), "-o", str(binary)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise EvaluationFailure(
                f"could not build the mandatory HEG score worker: {exc}"
            ) from exc
        self.identity = WorkerIdentity(
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            compiler=compiler,
            build_flags=_BUILD_FLAGS,
            platform=platform.platform(),
            architecture=platform.machine(),
        )
        self._worker = PersistentScoreWorker(
            binary=binary,
            timeout_seconds=INITIAL_TIMEOUT_SECONDS,
            memory_limit_bytes=64 * 1024 * 1024,
            cutoff_longest_first=True,
            prepared_request_cache_enabled=True,
        )
        return self

    def score(
        self,
        graph: Any,
        *,
        lengths: tuple[int, ...],
        witness_cap: int,
        node_budget: int,
    ) -> Any:
        if self._worker is None:
            raise EvaluationFailure("score worker is not active")
        self._worker.timeout_seconds = (
            INITIAL_TIMEOUT_SECONDS
            if node_budget == INITIAL_NODE_BUDGET
            else EXPANDED_TIMEOUT_SECONDS
        )
        try:
            response = self._worker.score(
                graph,
                lengths=lengths,
                limit=witness_cap + 1,
                node_budget=node_budget,
                cutoff=None,
                profile_timing=True,
            )
        except Exception as exc:
            if _is_timeout(exc):
                raise ScoreTimeoutWithoutPartial(str(exc)) from exc
            raise EvaluationFailure(f"mandatory HEG score worker failed: {exc}") from exc
        if response.dominated:
            raise EvaluationFailure("uncut HEG score request returned dominated")
        return response

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()
            self._worker = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def _is_timeout(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if "timeout" in str(current).lower() or "timed out" in str(current).lower():
            return True
        current = current.__cause__
    return False
