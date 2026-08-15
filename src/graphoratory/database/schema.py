from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
    Column("workspace_short", String(8), nullable=False),
    Column("workspace_name", String(64), unique=True),
    Column("created_at", String, nullable=False),
)

graphs = Table(
    "graphs",
    metadata,
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.workspace_hash", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    ),
    Column("graph_hash", String(64), primary_key=True),
    Column("graph_short", String(8), nullable=False),
    Column("graph_order", Integer, nullable=False),
)

graph_corpora = Table(
    "graph_corpora",
    metadata,
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.workspace_hash", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("generator", String, nullable=False),
    Column("configuration_json", Text, nullable=False),
    Column("requested_graph_count", Integer, nullable=False),
    Column("actual_graph_count", Integer, nullable=False),
    Column("attempted_candidates", Integer, nullable=False),
    Column("rejected_invalid_candidates", Integer, nullable=False),
    Column("duplicate_candidates", Integer, nullable=False),
)

lines = Table(
    "lines",
    metadata,
    Column("line_hash", String(64), primary_key=True),
    Column("line_short", String(8), nullable=False),
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.workspace_hash", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", String, nullable=False),
    Column("graph_count", Integer, nullable=False),
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
        "workspace_hash",
        String(64),
        nullable=False,
    ),
    Column(
        "graph_hash",
        String(64),
        primary_key=True,
    ),
    Column("position", Integer, nullable=False),
    ForeignKeyConstraint(
        ["workspace_hash", "graph_hash"],
        ["graphs.workspace_hash", "graphs.graph_hash"],
        ondelete="RESTRICT",
    ),
)

Index("ix_workspaces_workspace_short", workspaces.c.workspace_short)
Index("ix_graphs_graph_short", graphs.c.graph_short)
Index("ix_lines_line_short", lines.c.line_short)
Index(
    "ix_lines_workspace_created_hash",
    lines.c.workspace_hash,
    lines.c.created_at,
    lines.c.line_hash,
)
