from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from graphoratory.errors import ArtifactError
from graphoratory.identifiers import Identifier, ObjectType, resolve_typed
from graphoratory.jsonio import read_json, write_json_atomic

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
    path: Path


def workspace_directories(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        if path.is_dir() and path.name.startswith("ws-") and (path / WORKSPACE_MANIFEST).is_file():
            yield path


def workspace_artifacts(root: Path) -> list[WorkspaceArtifact]:
    return [workspace_artifact(path) for path in workspace_directories(root)]


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


def normalize_workspace_manifests(workspace_path: Path) -> None:
    manifest_path = workspace_path / WORKSPACE_MANIFEST
    manifest = read_json(manifest_path)
    changed = "config_source" in manifest
    manifest.pop("config_source", None)
    creation_config = manifest.get("creation_config")
    if isinstance(creation_config, dict) and creation_config.pop("workspace", None) is not None:
        changed = True
    if isinstance(creation_config, dict):
        graphs = creation_config.get("graphs")
        if isinstance(graphs, dict):
            changed = _normalize_graph_config(graphs) or changed
    if changed:
        write_json_atomic(manifest_path, manifest)

    graphs_manifest_path = workspace_path / "graphs" / WORKSPACE_MANIFEST
    if graphs_manifest_path.is_file():
        graphs_manifest = read_json(graphs_manifest_path)
        generation = graphs_manifest.get("generation")
        manifest_changed = False
        if isinstance(generation, dict):
            manifest_changed = _normalize_graph_config(generation)
            generator = generation.get("generator")
            if "generation_attempts" in graphs_manifest:
                graphs_manifest["attempted_candidates"] = graphs_manifest.pop(
                    "generation_attempts"
                )
                manifest_changed = True
            if "duplicate_attempts" in graphs_manifest:
                graphs_manifest["duplicate_candidates"] = graphs_manifest.pop(
                    "duplicate_attempts"
                )
                manifest_changed = True
            if "rejected_invalid_candidates" not in graphs_manifest:
                graphs_manifest["rejected_invalid_candidates"] = 0
                manifest_changed = True
            if "accepted_distinct_graphs" not in graphs_manifest:
                graphs_manifest["accepted_distinct_graphs"] = graphs_manifest.get(
                    "graph_count", 0
                )
                manifest_changed = True
            if (
                "accepted_by_generator" not in graphs_manifest
                and isinstance(generator, str)
            ):
                graphs_manifest["accepted_by_generator"] = {
                    generator: graphs_manifest.get("graph_count", 0)
                }
                manifest_changed = True
        if manifest_changed:
            write_json_atomic(graphs_manifest_path, graphs_manifest)


def _normalize_graph_config(values: dict[str, object]) -> bool:
    changed = False
    for old, new in (
        ("count", "workspace_graph_count"),
        ("line_sample_size", "line_graph_count"),
    ):
        if old in values:
            values[new] = values.pop(old)
            changed = True
    if values.get("mode") == "unrestricted_min_degree_3":
        values.pop("mode")
        values["generator"] = "cycle_matching_stub_pairing"
        changed = True
    if values.get("order_distribution") == "round_robin":
        values["order_distribution"] = "accepted_round_robin"
        changed = True
    return changed


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


def resolve_latest_line(workspace: WorkspaceArtifact) -> LineArtifact:
    candidates = _line_artifacts(workspace)
    if not candidates:
        label = workspace.name or workspace.identifier.display
        raise ArtifactError(
            f"workspace {label} has no lines; create one with `graphlab line create`"
        )
    return max(candidates, key=lambda line: (line.created_at, line.identifier.digest))


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


def _line_artifacts(workspace: WorkspaceArtifact) -> list[LineArtifact]:
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
            if workspace_identifier != workspace.identifier:
                raise ValueError("line belongs to another workspace")
            created_at = _parse_utc_timestamp(_manifest_string(manifest, "created_at"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ArtifactError(f"invalid line manifest: {manifest_path}") from exc
        artifacts.append(LineArtifact(identifier, workspace_identifier, created_at, line_path))
    return artifacts


def _parse_utc_timestamp(value: str) -> datetime:
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
