from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError

from graphoratory.artifacts import DATABASE_NAME, GRAPH_FILE
from graphoratory.database.schema import graphs, line_graphs, lines, workspaces
from graphoratory.graphs import Graph, read_graphs_jsonl_gz
from graphoratory.jsonio import read_json


def database_path(workspace_path: Path) -> Path:
    return workspace_path / DATABASE_NAME


def make_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")


def index_workspace(connection: Connection, manifest: dict[str, Any]) -> None:
    connection.execute(
        workspaces.insert().values(
            workspace_hash=manifest["workspace_hash"],
            workspace_short=str(manifest["workspace_hash"])[:8],
            workspace_name=manifest.get("name"),
            created_at=manifest["created_at"],
        )
    )


def index_graphs(
    connection: Connection,
    manifest: dict[str, Any],
    workspace_graphs: Iterable[Graph],
) -> None:
    connection.execute(
        graphs.insert(),
        [
            {
                "workspace_hash": manifest["workspace_hash"],
                "graph_hash": graph.graph_hash,
                "graph_short": graph.graph_hash[:8],
                "graph_order": graph.order,
            }
            for graph in workspace_graphs
        ],
    )


def index_line(connection: Connection, manifest: dict[str, Any]) -> None:
    connection.execute(
        lines.insert().values(
            line_hash=manifest["line_hash"],
            line_short=str(manifest["line_hash"])[:8],
            workspace_hash=manifest["workspace_hash"],
            created_at=manifest["created_at"],
            graph_count=len(manifest["graph_hashes"]),
        )
    )
    connection.execute(
        line_graphs.insert(),
        [
            {
                "line_hash": manifest["line_hash"],
                "graph_hash": graph_hash,
                "position": position,
            }
            for position, graph_hash in enumerate(manifest["graph_hashes"])
        ],
    )


def rebuild_database(workspace_path: Path) -> None:
    destination = database_path(workspace_path)
    temporary = destination.with_name(f".{destination.name}.reindex")
    delete_database(temporary)
    try:
        migrate(temporary)
        engine = make_engine(temporary)
        try:
            with engine.begin() as connection:
                workspace_manifest = read_json(workspace_path / "manifest.json")
                index_workspace(connection, workspace_manifest)
                graphs_path = workspace_path / "graphs"
                graphs_manifest_path = graphs_path / "manifest.json"
                if graphs_manifest_path.is_file():
                    graphs_manifest = read_json(graphs_manifest_path)
                    workspace_graphs = tuple(read_graphs_jsonl_gz(graphs_path / GRAPH_FILE))
                    _validate_graphs_manifest(graphs_manifest, workspace_graphs)
                    index_graphs(connection, graphs_manifest, workspace_graphs)
                _index_lines_from_artifacts(connection, workspace_path)
            with engine.connect() as connection:
                result = connection.execute(text("PRAGMA integrity_check")).scalar_one()
                if result != "ok":
                    raise RuntimeError(f"rebuilt SQLite integrity check failed: {result}")
                connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        finally:
            engine.dispose()
        delete_database(destination)
        temporary.replace(destination)
        _delete_sidecars(temporary)
    except BaseException:
        delete_database(temporary)
        raise


def delete_database(path: Path) -> None:
    if path.exists():
        engine = create_engine(f"sqlite:///{path}")
        try:
            try:
                with engine.connect() as connection:
                    connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            except SQLAlchemyError:
                pass
        finally:
            engine.dispose()
        path.unlink(missing_ok=True)
    _delete_sidecars(path)


def projection_counts(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            counts: dict[str, int] = {}
            for table in ("workspaces", "graphs", "lines", "line_graphs"):
                row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                if row is None:
                    raise sqlite3.DatabaseError(f"cannot count table {table}")
                counts[table] = int(row[0])
            return counts
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def _index_lines_from_artifacts(connection: Connection, workspace_path: Path) -> None:
    lines_path = workspace_path / "lines"
    if not lines_path.exists():
        return
    for line_path in sorted(lines_path.iterdir()):
        manifest_path = line_path / "manifest.json"
        if line_path.is_dir() and line_path.name.startswith("ln-") and manifest_path.is_file():
            index_line(connection, read_json(manifest_path))


def _validate_graphs_manifest(
    manifest: dict[str, Any], workspace_graphs: tuple[Graph, ...]
) -> None:
    hashes = [graph.graph_hash for graph in workspace_graphs]
    if hashes != manifest.get("graph_hashes"):
        raise ValueError("graph records do not match their manifest")
    if len(hashes) != manifest.get("graph_count"):
        raise ValueError("graph count does not match its manifest")


def _delete_sidecars(path: Path) -> None:
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
