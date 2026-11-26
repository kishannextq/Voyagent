"""RAG: count of chunks withheld by the prompt-injection classifier.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rag.documents ADD COLUMN flagged_chunks int NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE rag.documents DROP COLUMN flagged_chunks")
