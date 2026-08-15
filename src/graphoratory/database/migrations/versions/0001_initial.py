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
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_short", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_hash"),
        sa.UniqueConstraint("workspace_short"),
    )
    op.create_table(
        "graphs",
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_short", sa.String(length=8), nullable=False),
        sa.Column("graph_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_hash"],
            ["workspaces.workspace_hash"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("graph_hash"),
    )
    op.create_table(
        "lines",
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.Column("line_short", sa.String(length=8), nullable=False),
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("graph_count", sa.Integer(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_hash"], ["workspaces.workspace_hash"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("line_hash"),
        sa.UniqueConstraint("line_short"),
    )
    op.create_table(
        "line_graphs",
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["line_hash"], ["lines.line_hash"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["graph_hash"], ["graphs.graph_hash"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("line_hash", "graph_hash"),
    )


def downgrade() -> None:
    op.drop_table("line_graphs")
    op.drop_table("lines")
    op.drop_table("graphs")
    op.drop_table("workspaces")
