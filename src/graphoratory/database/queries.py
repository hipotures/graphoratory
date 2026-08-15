from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from graphoratory.errors import ArtifactError, IdentifierError
from graphoratory.identifiers import Identifier, ObjectType, parse_typed

_REINDEX = "workspace index is missing or stale; run `graphlab workspace reindex`"


@dataclass(frozen=True, slots=True)
class LineRow:
    identifier: Identifier
    workspace: Identifier
    created_at: str
    graph_count: int


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    evaluation_hash: str
    workspace_hash: str
    line_hash: str
    created_at: str
    baseline_name: str
    graph_count: int


@dataclass(frozen=True, slots=True)
class WorkspaceProjection:
    graph_count: int
    line_count: int
    generator: str | None
    configuration_json: str | None


def resolve_line(path: Path, value: str) -> LineRow:
    _, hash_part = parse_typed(value, ObjectType.LINE)
    with _connection(path) as connection:
        rows = connection.execute(
            _LINE_SELECT + " WHERE line_hash LIKE ? ORDER BY line_hash LIMIT 2",
            (f"{hash_part}%",),
        ).fetchall()
    if not rows:
        raise IdentifierError(f"{value} does not resolve in the workspace index; {_REINDEX}")
    if len(rows) > 1:
        raise IdentifierError(f"{value} is ambiguous; {len(rows)} objects match")
    return _line_row(rows[0])


def list_lines(path: Path, workspace_hash: str) -> tuple[LineRow, ...]:
    with _connection(path) as connection:
        rows = connection.execute(
            _LINE_SELECT
            + " WHERE workspace_hash = ?"
            + " ORDER BY created_at DESC, line_hash DESC",
            (workspace_hash,),
        ).fetchall()
    return tuple(_line_row(row) for row in rows)


def latest_line(path: Path, workspace_hash: str) -> LineRow | None:
    with _connection(path) as connection:
        row = connection.execute(
            _LINE_SELECT
            + " WHERE workspace_hash = ?"
            + " ORDER BY created_at DESC, line_hash DESC LIMIT 1",
            (workspace_hash,),
        ).fetchone()
    return _line_row(row) if row is not None else None


def line_graph_hashes(path: Path, line_hash: str) -> tuple[str, ...]:
    with _connection(path) as connection:
        rows = connection.execute(
            "SELECT graph_hash FROM line_graphs "
            "WHERE line_hash = ? ORDER BY position",
            (line_hash,),
        ).fetchall()
    return tuple(str(row["graph_hash"]) for row in rows)


def latest_evaluation(
    path: Path,
    line_hash: str,
    baseline_name: str,
) -> EvaluationRow | None:
    with _connection(path) as connection:
        row = connection.execute(
            """
            SELECT evaluation_hash, workspace_hash, line_hash, created_at,
                   baseline_name, graph_count
            FROM evaluations
            WHERE line_hash = ? AND baseline_name = ?
            ORDER BY created_at DESC, evaluation_hash DESC
            LIMIT 1
            """,
            (line_hash, baseline_name),
        ).fetchone()
    if row is None:
        return None
    return EvaluationRow(
        evaluation_hash=str(row["evaluation_hash"]),
        workspace_hash=str(row["workspace_hash"]),
        line_hash=str(row["line_hash"]),
        created_at=str(row["created_at"]),
        baseline_name=str(row["baseline_name"]),
        graph_count=int(row["graph_count"]),
    )


def workspace_projection(path: Path, workspace_hash: str) -> WorkspaceProjection:
    with _connection(path) as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM graphs WHERE workspace_hash = :workspace_hash)
                    AS graph_count,
                (SELECT COUNT(*) FROM lines WHERE workspace_hash = :workspace_hash)
                    AS line_count,
                graph_corpora.generator,
                graph_corpora.configuration_json
            FROM workspaces
            LEFT JOIN graph_corpora USING (workspace_hash)
            WHERE workspaces.workspace_hash = :workspace_hash
            """,
            {"workspace_hash": workspace_hash},
        ).fetchone()
    if row is None:
        raise ArtifactError(_REINDEX)
    return WorkspaceProjection(
        graph_count=int(row["graph_count"]),
        line_count=int(row["line_count"]),
        generator=str(row["generator"]) if row["generator"] is not None else None,
        configuration_json=(
            str(row["configuration_json"])
            if row["configuration_json"] is not None
            else None
        ),
    )


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise ArtifactError(_REINDEX)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("SELECT 1 FROM workspaces LIMIT 1")
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise ArtifactError(_REINDEX) from exc
    try:
        yield connection
    except sqlite3.Error as exc:
        raise ArtifactError(_REINDEX) from exc
    finally:
        connection.close()


def _line_row(row: sqlite3.Row) -> LineRow:
    return LineRow(
        identifier=Identifier(ObjectType.LINE, str(row["line_hash"])),
        workspace=Identifier(ObjectType.WORKSPACE, str(row["workspace_hash"])),
        created_at=str(row["created_at"]),
        graph_count=int(row["graph_count"]),
    )


_LINE_SELECT = (
    "SELECT line_hash, workspace_hash, created_at, graph_count FROM lines"
)
