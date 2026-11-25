"""Long-term memory: app.user_memory (allow-listed preference keys, with expiry).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.user_memory (
            owner      text NOT NULL,
            key        text NOT NULL CHECK (key IN ('home_city', 'travel_style', 'budget_level', 'last_destination')),
            value      text NOT NULL CHECK (char_length(value) BETWEEN 1 AND 80),
            updated_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            PRIMARY KEY (owner, key)
        )
        """
    )
    op.execute("CREATE INDEX user_memory_expiry ON app.user_memory (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE app.user_memory")
