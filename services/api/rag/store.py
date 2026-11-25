"""Document and chunk storage, always scoped by tenant.

- PostgresRagStore: pgvector (HNSW, cosine) + Postgres full-text search (GIN), schema `rag`
  (migration 0003). Documents keep their original bytes (<= 5 MiB) until S3 storage lands.
- MemoryRagStore: brute-force cosine + a small BM25, for local dev and tests only.

Versioning: uploading a document with an existing title creates version n+1; when it becomes
ready the older versions are marked `superseded` and leave search. Identical bytes (same
sha256) in a tenant are deduplicated.
"""

import datetime as dt
import math
import re
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be by for from has have in is it of on or that the this to was were will with".split())


@dataclass
class DocumentRecord:
    id: str
    tenant_id: str
    title: str
    mime: str
    sha256: str
    version: int
    status: str  # pending | processing | ready | failed | superseded
    scan: str
    chunk_count: int = 0
    error: str | None = None
    created_at: dt.datetime | None = None


@dataclass
class Chunk:
    id: int
    document_id: str
    title: str
    parent_id: int
    parent_text: str
    text: str


class RagStore(Protocol):
    async def add_document(
        self, tenant_id: str, title: str, mime: str, sha256: str, content: bytes, scan: str
    ) -> tuple[DocumentRecord, bool]: ...
    async def get_document(self, tenant_id: str, doc_id: str) -> DocumentRecord | None: ...
    async def load_content(self, doc_id: str) -> tuple[str, str, bytes]: ...
    async def set_status(self, doc_id: str, status: str, error: str | None = None, chunk_count: int = 0) -> None: ...
    async def replace_chunks(self, doc_id: str, tenant_id: str, model_id: str, parents: list) -> None: ...
    async def list_documents(self, tenant_id: str) -> list[DocumentRecord]: ...
    async def delete_document(self, tenant_id: str, doc_id: str) -> bool: ...
    async def vector_search(self, tenant_id: str, model_id: str, vec: list[float], k: int) -> list[tuple[int, float]]: ...
    async def text_search(self, tenant_id: str, query: str, k: int) -> list[tuple[int, float]]: ...
    async def fetch(self, chunk_ids: list[int]) -> dict[int, Chunk]: ...
    async def aclose(self) -> None: ...


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class MemoryRagStore:
    def __init__(self):
        self._docs: dict[str, DocumentRecord] = {}
        self._content: dict[str, bytes] = {}
        self._chunks: dict[int, dict] = {}
        self._parents: dict[int, str] = {}
        self._next = 1
        self._lock = threading.Lock()

    async def add_document(self, tenant_id, title, mime, sha256, content, scan):
        with self._lock:
            for d in self._docs.values():
                if d.tenant_id == tenant_id and d.sha256 == sha256:
                    return d, True
            version = 1 + max((d.version for d in self._docs.values() if d.tenant_id == tenant_id and d.title == title), default=0)
            rec = DocumentRecord(
                str(uuid.uuid4()), tenant_id, title, mime, sha256, version, "pending", scan, created_at=dt.datetime.now(dt.UTC)
            )
            self._docs[rec.id] = rec
            self._content[rec.id] = content
            return rec, False

    async def get_document(self, tenant_id, doc_id):
        d = self._docs.get(doc_id)
        return d if d and d.tenant_id == tenant_id else None

    async def load_content(self, doc_id):
        d = self._docs[doc_id]
        return d.tenant_id, d.mime, self._content[doc_id]

    async def set_status(self, doc_id, status, error=None, chunk_count=0):
        with self._lock:
            d = self._docs[doc_id]
            d.status, d.error, d.chunk_count = status, error, chunk_count
            if status == "ready":
                for other in self._docs.values():
                    if other.tenant_id == d.tenant_id and other.title == d.title and other.version < d.version:
                        other.status = "superseded"

    async def replace_chunks(self, doc_id, tenant_id, model_id, parents):
        with self._lock:
            for cid in [c for c, v in self._chunks.items() if v["document_id"] == doc_id]:
                del self._chunks[cid]
            for parent_text, children in parents:
                pid = self._next
                self._next += 1
                self._parents[pid] = parent_text
                for text, vec in children:
                    cid = self._next
                    self._next += 1
                    self._chunks[cid] = {
                        "document_id": doc_id,
                        "tenant_id": tenant_id,
                        "parent_id": pid,
                        "text": text,
                        "vec": vec,
                        "model": model_id,
                        "tokens": Counter(_tokens(text)),
                    }

    async def list_documents(self, tenant_id):
        docs = [d for d in self._docs.values() if d.tenant_id == tenant_id]
        return sorted(docs, key=lambda d: (d.title, -d.version))

    async def delete_document(self, tenant_id, doc_id):
        with self._lock:
            d = self._docs.get(doc_id)
            if not d or d.tenant_id != tenant_id:
                return False
            del self._docs[doc_id]
            self._content.pop(doc_id, None)
            for cid in [c for c, v in self._chunks.items() if v["document_id"] == doc_id]:
                del self._chunks[cid]
            return True

    def _searchable(self, tenant_id):
        live = {d.id for d in self._docs.values() if d.tenant_id == tenant_id and d.status == "ready"}
        return [(cid, c) for cid, c in self._chunks.items() if c["tenant_id"] == tenant_id and c["document_id"] in live]

    async def vector_search(self, tenant_id, model_id, vec, k):
        scored = [
            (cid, sum(a * b for a, b in zip(c["vec"], vec, strict=False)))
            for cid, c in self._searchable(tenant_id)
            if c["model"] == model_id
        ]
        return sorted(scored, key=lambda x: -x[1])[:k]

    async def text_search(self, tenant_id, query, k):
        rows = self._searchable(tenant_id)
        q = set(_tokens(query))
        if not rows or not q:
            return []
        n = len(rows)
        avg = sum(sum(c["tokens"].values()) for _, c in rows) / n
        df = Counter(t for _, c in rows for t in set(c["tokens"]) & q)
        scored = []
        for cid, c in rows:
            length = sum(c["tokens"].values())
            s = 0.0
            for t in q:
                tf = c["tokens"].get(t, 0)
                if tf:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
            if s > 0:
                scored.append((cid, s))
        return sorted(scored, key=lambda x: -x[1])[:k]

    async def fetch(self, chunk_ids):
        out = {}
        for cid in chunk_ids:
            c = self._chunks.get(cid)
            if c:
                d = self._docs.get(c["document_id"])
                out[cid] = Chunk(cid, c["document_id"], d.title if d else "", c["parent_id"], self._parents[c["parent_id"]], c["text"])
        return out

    async def aclose(self):
        return None


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class PostgresRagStore:
    def __init__(self, pool):
        self._pool = pool

    async def add_document(self, tenant_id, title, mime, sha256, content, scan):
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute("SELECT * FROM rag.documents WHERE tenant_id = %s AND sha256 = %s", (tenant_id, sha256))
                row = await cur.fetchone()
                if row:
                    return _doc(row), True
                # Serialise version numbering per (tenant, title).
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{tenant_id}\x00{title}",))
                cur = await conn.execute(
                    "INSERT INTO rag.documents (tenant_id, title, mime, sha256, version, status, scan, content) "
                    "SELECT %s, %s, %s, %s, coalesce(max(version), 0) + 1, 'pending', %s, %s "
                    "FROM rag.documents WHERE tenant_id = %s AND title = %s RETURNING *",
                    (tenant_id, title, mime, sha256, scan, content, tenant_id, title),
                )
                return _doc(await cur.fetchone()), False

    async def get_document(self, tenant_id, doc_id):
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM rag.documents WHERE tenant_id = %s AND id = %s", (tenant_id, doc_id))
            row = await cur.fetchone()
        return _doc(row) if row else None

    async def load_content(self, doc_id):
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT tenant_id, mime, content FROM rag.documents WHERE id = %s", (doc_id,))
            row = await cur.fetchone()
        return row["tenant_id"], row["mime"], bytes(row["content"])

    async def set_status(self, doc_id, status, error=None, chunk_count=0):
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "UPDATE rag.documents SET status = %s, error = %s, chunk_count = %s, updated_at = now() "
                    "WHERE id = %s RETURNING tenant_id, title, version",
                    (status, error, chunk_count, doc_id),
                )
                row = await cur.fetchone()
                if status == "ready" and row:
                    await conn.execute(
                        "UPDATE rag.documents SET status = 'superseded', updated_at = now() "
                        "WHERE tenant_id = %s AND title = %s AND version < %s AND status = 'ready'",
                        (row["tenant_id"], row["title"], row["version"]),
                    )

    async def replace_chunks(self, doc_id, tenant_id, model_id, parents):
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM rag.parents WHERE document_id = %s", (doc_id,))
                for ord_, (parent_text, children) in enumerate(parents):
                    cur = await conn.execute(
                        "INSERT INTO rag.parents (document_id, tenant_id, ord, content) VALUES (%s, %s, %s, %s) RETURNING id",
                        (doc_id, tenant_id, ord_, parent_text),
                    )
                    pid = (await cur.fetchone())["id"]
                    async with conn.cursor() as c:
                        await c.executemany(
                            "INSERT INTO rag.chunks (parent_id, document_id, tenant_id, content, embedding, embedding_model) "
                            "VALUES (%s, %s, %s, %s, %s::vector, %s)",
                            [(pid, doc_id, tenant_id, text, _vec(vec), model_id) for text, vec in children],
                        )

    async def list_documents(self, tenant_id):
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM rag.documents WHERE tenant_id = %s ORDER BY title, version DESC", (tenant_id,)
            )
            return [_doc(r) for r in await cur.fetchall()]

    async def delete_document(self, tenant_id, doc_id):
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM rag.documents WHERE tenant_id = %s AND id = %s", (tenant_id, doc_id))
            return cur.rowcount > 0

    async def vector_search(self, tenant_id, model_id, vec, k):
        async with self._pool.connection() as conn:
            async with conn.transaction():
                # Keep scanning the HNSW index until enough rows pass the tenant filter (pgvector >= 0.8).
                await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                cur = await conn.execute(
                    "SELECT c.id, 1 - (c.embedding <=> %s::vector) AS score FROM rag.chunks c "
                    "JOIN rag.documents d ON d.id = c.document_id "
                    "WHERE c.tenant_id = %s AND c.embedding_model = %s AND d.status = 'ready' "
                    "ORDER BY c.embedding <=> %s::vector LIMIT %s",
                    (_vec(vec), tenant_id, model_id, _vec(vec), k),
                )
                return [(r["id"], float(r["score"])) for r in await cur.fetchall()]

    async def text_search(self, tenant_id, query, k):
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT c.id, ts_rank_cd(c.tsv, q) AS score FROM rag.chunks c "
                "JOIN rag.documents d ON d.id = c.document_id, websearch_to_tsquery('english', %s) q "
                "WHERE c.tenant_id = %s AND d.status = 'ready' AND c.tsv @@ q ORDER BY score DESC LIMIT %s",
                (query, tenant_id, k),
            )
            return [(r["id"], float(r["score"])) for r in await cur.fetchall()]

    async def fetch(self, chunk_ids):
        if not chunk_ids:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT c.id, c.document_id, d.title, c.parent_id, p.content AS parent_text, c.content AS text "
                "FROM rag.chunks c JOIN rag.parents p ON p.id = c.parent_id JOIN rag.documents d ON d.id = c.document_id "
                "WHERE c.id = ANY(%s)",
                (chunk_ids,),
            )
            return {
                r["id"]: Chunk(r["id"], str(r["document_id"]), r["title"], r["parent_id"], r["parent_text"], r["text"])
                for r in await cur.fetchall()
            }

    async def aclose(self):
        await self._pool.close()


def _doc(r: dict) -> DocumentRecord:
    return DocumentRecord(
        str(r["id"]), r["tenant_id"], r["title"], r["mime"], r["sha256"], r["version"], r["status"], r["scan"],
        r["chunk_count"], r["error"], r["created_at"],
    )


_memory_singleton: MemoryRagStore | None = None


async def open_store(database_url: str | None) -> RagStore:
    """Postgres when DATABASE_URL is set. The in-memory store is shared within a process, so the
    in-process api and orchestrator see the same documents (local/test only)."""
    global _memory_singleton
    if not database_url:
        import settings

        if settings.durable_required():
            raise RuntimeError(f"DATABASE_URL is required when APP_ENV={settings.app_env()}")
        if _memory_singleton is None:
            _memory_singleton = MemoryRagStore()
        return _memory_singleton
    import db

    return PostgresRagStore(await db.open_pool(database_url, "rag", required_table="rag.chunks"))
