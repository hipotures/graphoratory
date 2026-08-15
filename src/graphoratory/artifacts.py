from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
    identifiers = [_manifest_hash(path / WORKSPACE_MANIFEST, "workspace") for path in paths]
    resolved = resolve_typed(value, ObjectType.WORKSPACE, identifiers)
    for path, hash_full in zip(paths, identifiers, strict=True):
        if hash_full == resolved.hash_full:
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
                    (_manifest_hash(manifest_path, "line"), line_path, workspace_path)
                )
    resolved = resolve_typed(value, ObjectType.LINE, (hash_full for hash_full, _, _ in candidates))
    for hash_full, line_path, workspace_path in candidates:
        if hash_full == resolved.hash_full:
            return resolved, line_path, workspace_path
    raise AssertionError("resolved line has no path")


def corpus_directories(workspace_path: Path) -> Iterator[Path]:
    graphs_path = workspace_path / "graphs"
    if not graphs_path.exists():
        return
    for path in sorted(graphs_path.iterdir()):
        if (
            path.is_dir()
            and path.name.startswith("cp-")
            and (path / WORKSPACE_MANIFEST).is_file()
            and (path / GRAPH_FILE).is_file()
        ):
            yield path


def latest_corpus(workspace_path: Path) -> tuple[dict[str, Any], Path]:
    candidates = [
        (read_json(path / WORKSPACE_MANIFEST), path) for path in corpus_directories(workspace_path)
    ]
    if not candidates:
        raise ArtifactError("workspace has no completed graph corpus; run graph generate first")
    return max(candidates, key=lambda item: (str(item[0]["created_at"]), item[1].name))


def resolve_corpus(workspace_path: Path, value: str) -> tuple[dict[str, Any], Path]:
    candidates = [
        (read_json(path / WORKSPACE_MANIFEST), path) for path in corpus_directories(workspace_path)
    ]
    resolved = resolve_typed(
        value,
        ObjectType.CORPUS,
        (str(manifest["hash_full"]) for manifest, _ in candidates),
    )
    for manifest, path in candidates:
        if manifest["hash_full"] == resolved.hash_full:
            return manifest, path
    raise AssertionError("resolved corpus has no path")


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


def _manifest_hash(path: Path, kind: str) -> str:
    try:
        value = read_json(path)["hash_full"]
    except (OSError, ValueError, KeyError) as exc:
        raise ArtifactError(f"invalid {kind} manifest: {path}") from exc
    if not isinstance(value, str):
        raise ArtifactError(f"invalid {kind} hash in {path}")
    return value
