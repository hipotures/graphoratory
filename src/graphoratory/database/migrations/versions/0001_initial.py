"""Create the initial filesystem projection schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("hash_full", sa.String(length=64), nullable=False),
        sa.Column("hash_short", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("hash_full"),
        sa.UniqueConstraint("hash_short"),
    )
    op.create_table(
        "corpora",
        sa.Column("hash_full", sa.String(length=64), nullable=False),
        sa.Column("hash_short", sa.String(length=8), nullable=False),
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("graph_count", sa.Integer(), nullable=False),
        sa.Column("min_order", sa.Integer(), nullable=False),
        sa.Column("max_order", sa.Integer(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.Column("graph_file", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_hash"], ["workspaces.hash_full"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("hash_full"),
        sa.UniqueConstraint("hash_short"),
    )
    op.create_table(
        "graphs",
        sa.Column("corpus_hash", sa.String(length=64), nullable=False),
        sa.Column("hash_full", sa.String(length=64), nullable=False),
        sa.Column("hash_short", sa.String(length=8), nullable=False),
        sa.Column("graph_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["corpus_hash"], ["corpora.hash_full"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("corpus_hash", "hash_full"),
    )
    op.create_table(
        "lines",
        sa.Column("hash_full", sa.String(length=64), nullable=False),
        sa.Column("hash_short", sa.String(length=8), nullable=False),
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("corpus_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("graph_count", sa.Integer(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["corpus_hash"], ["corpora.hash_full"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_hash"], ["workspaces.hash_full"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("hash_full"),
        sa.UniqueConstraint("hash_short"),
    )
    op.create_table(
        "line_graphs",
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.Column("corpus_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["line_hash"], ["lines.hash_full"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["corpus_hash", "graph_hash"],
            ["graphs.corpus_hash", "graphs.hash_full"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("line_hash", "corpus_hash", "graph_hash"),
    )


def downgrade() -> None:
    op.drop_table("line_graphs")
    op.drop_table("lines")
    op.drop_table("graphs")
    op.drop_table("corpora")
    op.drop_table("workspaces")
