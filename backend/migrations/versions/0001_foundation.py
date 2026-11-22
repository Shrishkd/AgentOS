"""Establish the schema baseline and allow the API to check its revision."""

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON alembic_version TO agentos_api")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM agentos_api")
