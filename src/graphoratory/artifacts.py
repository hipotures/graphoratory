from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from graphoratory.errors import ArtifactError, IdentifierError
from graphoratory.identifiers import Identifier, ObjectType
from graphoratory.jsonio import canonical_json_bytes, read_json

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


@dataclass(frozen=True, slots=True)
class LineArtifact:
    identifier: Identifier
    workspace: Identifier
    created_at: datetime
    graph_count: int
    path: Path


def scan_workspace_directories(root: Path) -> Iterator[Path]:
    """Enumerate workspace artifacts for explicit reindex and recovery operations."""
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if path.is_dir() and path.name.startswith("ws-") and (path / WORKSPACE_MANIFEST).is_file():
            yield path


def validate_workspace_name(value: str) -> None:
    if not _WORKSPACE_NAME.fullmatch(value) or _WORKSPACE_TYPED_NAME.fullmatch(value):
        raise ArtifactError(
            "workspace name must be 1 to 64 characters using letters, numbers, '-' or '_', "
            "must start with a letter or number, and must not look like a typed workspace ID"
        )


def ensure_workspace_alias(workspace: WorkspaceArtifact) -> Path | None:
    if workspace.name is None:
        return None
    alias = workspace.path.parent / workspace.name
    target = workspace.path.name
    if alias.is_symlink():
        if alias.readlink() != Path(target):
            raise ArtifactError(f"workspace alias points to the wrong target: {alias}")
        return alias
    if alias.exists():
        raise ArtifactError(f"workspace alias path already exists: {alias}")
    alias.symlink_to(target, target_is_directory=True)
    return alias


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


def workspace_artifact(path: Path) -> WorkspaceArtifact:
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


def scan_line_artifacts(workspace: WorkspaceArtifact) -> list[LineArtifact]:
    """Enumerate line artifacts for explicit reindex and recovery operations."""
    lines_path = workspace.path / "lines"
    if not lines_path.exists():
        return []
    artifacts: list[LineArtifact] = []
    for line_path in sorted(lines_path.iterdir()):
        manifest_path = line_path / WORKSPACE_MANIFEST
        if (
            not line_path.is_dir()
            or not line_path.name.startswith("ln-")
            or not manifest_path.is_file()
        ):
            continue
        try:
            manifest = read_json(manifest_path)
            identifier = Identifier(
                ObjectType.LINE,
                _manifest_string(manifest, "line_hash"),
            )
            workspace_identifier = Identifier(
                ObjectType.WORKSPACE,
                _manifest_string(manifest, "workspace_hash"),
            )
            if manifest.get("artifact_type") != "line":
                raise ValueError("artifact_type must be line")
            if identifier.display != line_path.name:
                raise ValueError("line directory does not match line hash")
            if workspace_identifier != workspace.identifier:
                raise ValueError("line belongs to another workspace")
            created_at = parse_utc_timestamp(_manifest_string(manifest, "created_at"))
            graph_hashes = manifest["graph_hashes"]
            if not isinstance(graph_hashes, list):
                raise TypeError("graph_hashes must be a list")
            for graph_hash in graph_hashes:
                if not isinstance(graph_hash, str):
                    raise TypeError("graph hash must be a string")
                Identifier(ObjectType.GRAPH, graph_hash)
            expected_identifier = Identifier.from_bytes(
                ObjectType.LINE,
                canonical_json_bytes(
                    {
                        "workspace_hash": workspace_identifier.digest,
                        "created_at": _manifest_string(manifest, "created_at"),
                        "graph_hashes": graph_hashes,
                    }
                ),
            )
            if identifier != expected_identifier:
                raise ValueError("line hash does not match its identity payload")
        except (OSError, ValueError, KeyError, TypeError, IdentifierError) as exc:
            raise ArtifactError(f"invalid line manifest: {manifest_path}") from exc
        artifacts.append(
            LineArtifact(
                identifier,
                workspace_identifier,
                created_at,
                len(graph_hashes),
                line_path,
            )
        )
    return sorted(
        artifacts,
        key=lambda line: (line.created_at, line.identifier.digest),
        reverse=True,
    )


def parse_utc_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("timestamp must use UTC Z notation")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed


def _manifest_string(manifest: dict[str, Any], field: str) -> str:
    value = manifest[field]
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value
