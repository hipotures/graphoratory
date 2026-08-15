from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphoratory.errors import ArtifactError
from graphoratory.identifiers import Identifier, ObjectType, resolve_typed
from graphoratory.jsonio import read_json

WORKSPACE_MANIFEST = "manifest.json"
DATABASE_NAME = "index.sqlite3"
GRAPH_FILE = "graphs.jsonl.gz"
_WORKSPACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WORKSPACE_TYPED_NAME = re.compile(r"^ws-(?:[0-9a-f]{8}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class WorkspaceArtifact:
    identifier: Identifier
    name: str | None
    created_at: str
    path: Path


def workspace_directories(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if path.is_dir() and path.name.startswith("ws-") and (path / WORKSPACE_MANIFEST).is_file():
            yield path


def workspace_artifacts(root: Path) -> list[WorkspaceArtifact]:
    return [_workspace_artifact(path) for path in workspace_directories(root)]


def resolve_workspace(root: Path, value: str) -> WorkspaceArtifact:
    candidates = workspace_artifacts(root)
    if value.startswith(f"{ObjectType.WORKSPACE.value}-"):
        resolved = resolve_typed(
            value,
            ObjectType.WORKSPACE,
            (candidate.identifier.digest for candidate in candidates),
        )
        return next(candidate for candidate in candidates if candidate.identifier == resolved)

    validate_workspace_name(value)
    matches = [candidate for candidate in candidates if candidate.name == value]
    if not matches:
        raise ArtifactError(f"workspace not found: {value}")
    if len(matches) > 1:
        raise ArtifactError(f"duplicate workspace name: {value}")
    return matches[0]


def validate_workspace_name(value: str) -> None:
    if not _WORKSPACE_NAME.fullmatch(value) or _WORKSPACE_TYPED_NAME.fullmatch(value):
        raise ArtifactError(
            "workspace name must be 1 to 64 characters using letters, numbers, '-' or '_', "
            "must start with a letter or number, and must not look like a typed workspace ID"
        )


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


def _workspace_artifact(path: Path) -> WorkspaceArtifact:
    manifest_path = path / WORKSPACE_MANIFEST
    try:
        manifest = read_json(manifest_path)
        workspace_hash = _manifest_string(manifest, "workspace_hash")
        created_at = _manifest_string(manifest, "created_at")
        raw_name = manifest.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise TypeError("name must be a string")
        if raw_name is not None:
            validate_workspace_name(raw_name)
        identifier = Identifier(ObjectType.WORKSPACE, workspace_hash)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactError(f"invalid workspace manifest: {manifest_path}") from exc
    return WorkspaceArtifact(identifier, raw_name, created_at, path)


def _manifest_string(manifest: dict[str, Any], field: str) -> str:
    value = manifest[field]
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value
