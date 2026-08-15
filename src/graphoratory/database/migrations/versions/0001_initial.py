"""Create the project-wide artifact index."""

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
        sa.Column("workspace_name", sa.String(length=64)),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_hash"),
        sa.UniqueConstraint("workspace_name"),
    )
    op.create_index(
        "ix_workspaces_workspace_short",
        "workspaces",
        ["workspace_short"],
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
        sa.PrimaryKeyConstraint("workspace_hash", "graph_hash"),
    )
    op.create_index("ix_graphs_graph_short", "graphs", ["graph_short"])
    op.create_table(
        "graph_corpora",
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("generator", sa.String(), nullable=False),
        sa.Column("configuration_json", sa.Text(), nullable=False),
        sa.Column("requested_graph_count", sa.Integer(), nullable=False),
        sa.Column("actual_graph_count", sa.Integer(), nullable=False),
        sa.Column("attempted_candidates", sa.Integer(), nullable=False),
        sa.Column("rejected_invalid_candidates", sa.Integer(), nullable=False),
        sa.Column("duplicate_candidates", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_hash"],
            ["workspaces.workspace_hash"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_hash"),
    )
    op.create_table(
        "lines",
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.Column("line_short", sa.String(length=8), nullable=False),
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("graph_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_hash"],
            ["workspaces.workspace_hash"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("line_hash"),
    )
    op.create_index("ix_lines_line_short", "lines", ["line_short"])
    op.create_index(
        "ix_lines_workspace_created_hash",
        "lines",
        ["workspace_hash", "created_at", "line_hash"],
    )
    op.create_table(
        "line_graphs",
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["line_hash"],
            ["lines.line_hash"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_hash", "graph_hash"],
            ["graphs.workspace_hash", "graphs.graph_hash"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("line_hash", "graph_hash"),
    )


def downgrade() -> None:
    op.drop_table("line_graphs")
    op.drop_index("ix_lines_workspace_created_hash", table_name="lines")
    op.drop_index("ix_lines_line_short", table_name="lines")
    op.drop_table("lines")
    op.drop_table("graph_corpora")
    op.drop_index("ix_graphs_graph_short", table_name="graphs")
    op.drop_table("graphs")
    op.drop_index("ix_workspaces_workspace_short", table_name="workspaces")
    op.drop_table("workspaces")
