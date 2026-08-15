from sqlalchemy import (
    Column,
    ForeignKey,
    ForeignKeyConstraint,
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
    Column("hash_full", String(64), primary_key=True),
    Column("hash_short", String(8), nullable=False, unique=True),
    Column("created_at", String, nullable=False),
    Column("manifest_path", Text, nullable=False),
)

corpora = Table(
    "corpora",
    metadata,
    Column("hash_full", String(64), primary_key=True),
    Column("hash_short", String(8), nullable=False, unique=True),
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.hash_full", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", String, nullable=False),
    Column("graph_count", Integer, nullable=False),
    Column("min_order", Integer, nullable=False),
    Column("max_order", Integer, nullable=False),
    Column("manifest_path", Text, nullable=False),
    Column("graph_file", Text, nullable=False),
)

graphs = Table(
    "graphs",
    metadata,
    Column(
        "corpus_hash",
        String(64),
        ForeignKey("corpora.hash_full", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("hash_full", String(64), primary_key=True),
    Column("hash_short", String(8), nullable=False),
    Column("graph_order", Integer, nullable=False),
)

lines = Table(
    "lines",
    metadata,
    Column("hash_full", String(64), primary_key=True),
    Column("hash_short", String(8), nullable=False, unique=True),
    Column(
        "workspace_hash",
        String(64),
        ForeignKey("workspaces.hash_full", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "corpus_hash",
        String(64),
        ForeignKey("corpora.hash_full", ondelete="RESTRICT"),
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
        ForeignKey("lines.hash_full", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("corpus_hash", String(64), primary_key=True),
    Column("graph_hash", String(64), primary_key=True),
    Column("position", Integer, nullable=False),
    ForeignKeyConstraint(
        ["corpus_hash", "graph_hash"],
        ["graphs.corpus_hash", "graphs.hash_full"],
        ondelete="RESTRICT",
    ),
)
