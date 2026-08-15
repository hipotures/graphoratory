"""Index graph-corpus generator provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0004_graph_corpus_generator"
down_revision = "0003_portable_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("graph_corpora")
