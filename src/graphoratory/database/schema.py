from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

workspaces = Table(
    "workspaces",
    metadata,
    Column("workspace_hash", String(64), primary_key=True),
    Column("workspace_short", String(8), nullable=False, unique=True),
    Column("created_at", String, nullable=False),
    Column("manifest_path", Text, nullable=False),
)

graphs = Table(
    "graphs",
    metadata,
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.workspace_hash", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("graph_hash", String(64), primary_key=True),
    Column("graph_short", String(8), nullable=False),
    Column("graph_order", Integer, nullable=False),
)

lines = Table(
    "lines",
    metadata,
    Column("line_hash", String(64), primary_key=True),
    Column("line_short", String(8), nullable=False, unique=True),
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.workspace_hash", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", String, nullable=False),
    Column("graph_count", Integer, nullable=False),
    Column("manifest_path", Text, nullable=False),
)

line_graphs = Table(
    "line_graphs",
    metadata,
    Column(
        "line_hash",
        String(64),
        ForeignKey("lines.line_hash", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "graph_hash",
        String(64),
        ForeignKey("graphs.graph_hash", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("position", Integer, nullable=False),
)
