"""Remove redundant machine-specific artifact paths."""

from alembic import op

revision = "0003_portable_persistence"
down_revision = "0002_workspace_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("manifest_path")
    with op.batch_alter_table("lines") as batch:
        batch.drop_column("manifest_path")


def downgrade() -> None:
    raise NotImplementedError("portable persistence cannot recreate removed absolute paths")
