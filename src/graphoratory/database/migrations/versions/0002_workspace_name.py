"""Index human-readable workspace names."""

import sqlalchemy as sa
from alembic import op

revision = "0002_workspace_name"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("workspace_name", sa.String(length=64)))
    op.create_index(
        "uq_workspaces_workspace_name",
        "workspaces",
        ["workspace_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_workspaces_workspace_name", table_name="workspaces")
    op.drop_column("workspaces", "workspace_name")
