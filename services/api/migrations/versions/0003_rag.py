"""RAG: rag.documents, rag.parents, rag.chunks (pgvector HNSW + full-text GIN).

Revision ID: 0003
Revises: 0002

The embedding dimension is fixed at migration time (RAG_EMBED_DIM, default 384). Changing the
embedding model to a different dimension needs a new migration and re-ingestion.
"""

import os

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dim = int(os.getenv("RAG_EMBED_DIM", "384"))
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS rag")
    op.execute(
        """
        CREATE TABLE rag.documents (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   text NOT NULL,
            title       text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
            mime        text NOT NULL CHECK (mime IN ('application/pdf', 'text/plain', 'text/markdown')),
            sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            version     int NOT NULL CHECK (version >= 1),
            status      text NOT NULL CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'superseded')),
            scan        text NOT NULL CHECK (scan IN ('clean', 'not_scanned')),
            error       text,
            chunk_count int NOT NULL DEFAULT 0,
            content     bytea NOT NULL CHECK (octet_length(content) <= 5242880),
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, sha256),
            UNIQUE (tenant_id, title, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE rag.parents (
            id          bigserial PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES rag.documents (id) ON DELETE CASCADE,
            tenant_id   text NOT NULL,
            ord         int NOT NULL,
            content     text NOT NULL
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE rag.chunks (
            id              bigserial PRIMARY KEY,
            parent_id       bigint NOT NULL REFERENCES rag.parents (id) ON DELETE CASCADE,
            document_id     uuid NOT NULL REFERENCES rag.documents (id) ON DELETE CASCADE,
            tenant_id       text NOT NULL,
            content         text NOT NULL,
            embedding       vector({dim}) NOT NULL,
            embedding_model text NOT NULL,
            tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
        )
        """
    )
    op.execute("CREATE INDEX chunks_embedding_hnsw ON rag.chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX chunks_tsv ON rag.chunks USING gin (tsv)")
    op.execute("CREATE INDEX chunks_tenant ON rag.chunks (tenant_id, embedding_model)")
    op.execute("CREATE INDEX documents_tenant ON rag.documents (tenant_id, title, version DESC)")
    op.execute("CREATE INDEX parents_document ON rag.parents (document_id)")


def downgrade() -> None:
    op.execute("DROP SCHEMA rag CASCADE")
