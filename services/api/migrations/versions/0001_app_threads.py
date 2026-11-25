"""Plan ownership: app.threads.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute(
        """
        CREATE TABLE app.threads (
            thread_id  text PRIMARY KEY CHECK (thread_id ~ '^[A-Za-z0-9_-]{8,64}$'),
            tenant_id  text NOT NULL,
            owner_sub  text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX threads_owner_recent ON app.threads (tenant_id, owner_sub, created_at DESC)")
    op.execute("CREATE INDEX threads_tenant_recent ON app.threads (tenant_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE app.threads")
