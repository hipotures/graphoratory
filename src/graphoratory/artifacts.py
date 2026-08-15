from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from graphoratory.errors import ArtifactError
from graphoratory.identifiers import Identifier, ObjectType, resolve_typed
from graphoratory.jsonio import read_json

WORKSPACE_MANIFEST = "manifest.json"
DATABASE_NAME = "index.sqlite3"
GRAPH_FILE = "graphs.jsonl.gz"


def workspace_directories(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if path.is_dir() and path.name.startswith("ws-") and (path / WORKSPACE_MANIFEST).is_file():
            yield path


def resolve_workspace(root: Path, value: str) -> tuple[Identifier, Path]:
    paths = list(workspace_directories(root))
    identifiers = [
        _manifest_hash(path / WORKSPACE_MANIFEST, "workspace_hash", "workspace") for path in paths
    ]
    resolved = resolve_typed(value, ObjectType.WORKSPACE, identifiers)
    for path, workspace_hash in zip(paths, identifiers, strict=True):
        if workspace_hash == resolved.digest:
            return resolved, path
    raise AssertionError("resolved workspace has no path")


def resolve_line(root: Path, value: str) -> tuple[Identifier, Path, Path]:
    candidates: list[tuple[str, Path, Path]] = []
    for workspace_path in workspace_directories(root):
        lines_path = workspace_path / "lines"
        if not lines_path.exists():
            continue
        for line_path in sorted(lines_path.iterdir()):
            manifest_path = line_path / WORKSPACE_MANIFEST
            if line_path.is_dir() and line_path.name.startswith("ln-") and manifest_path.is_file():
                candidates.append(
                    (
                        _manifest_hash(manifest_path, "line_hash", "line"),
                        line_path,
                        workspace_path,
                    )
                )
    resolved = resolve_typed(value, ObjectType.LINE, (line_hash for line_hash, _, _ in candidates))
    for line_hash, line_path, workspace_path in candidates:
        if line_hash == resolved.digest:
            return resolved, line_path, workspace_path
    raise AssertionError("resolved line has no path")


def temporary_directory(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{prefix}.", dir=parent))


def publish_directory(temporary: Path, final: Path) -> None:
    if final.exists():
        raise ArtifactError(f"artifact already exists: {final}")
    temporary.replace(final)


def discard_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _manifest_hash(path: Path, field: str, kind: str) -> str:
    try:
        value = read_json(path)[field]
    except (OSError, ValueError, KeyError) as exc:
        raise ArtifactError(f"invalid {kind} manifest: {path}") from exc
    if not isinstance(value, str):
        raise ArtifactError(f"invalid {kind} hash in {path}")
    return value
