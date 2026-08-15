"""Add the rebuildable baseline-evaluation projection."""

import sqlalchemy as sa
from alembic import op

revision = "0002_evaluations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluations",
        sa.Column("evaluation_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_hash", sa.String(length=64), nullable=False),
        sa.Column("line_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("baseline_name", sa.String(), nullable=False),
        sa.Column("graph_count", sa.Integer(), nullable=False),
        sa.Column("score_lower_numerator", sa.Text(), nullable=False),
        sa.Column("score_lower_denominator", sa.Text(), nullable=False),
        sa.Column("score_upper_numerator", sa.Text(), nullable=False),
        sa.Column("score_upper_denominator", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_hash"],
            ["workspaces.workspace_hash"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["line_hash"],
            ["lines.line_hash"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("evaluation_hash"),
    )
    op.create_index(
        "ix_evaluations_line_created_hash",
        "evaluations",
        ["line_hash", "created_at", "evaluation_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluations_line_created_hash", table_name="evaluations")
    op.drop_table("evaluations")
