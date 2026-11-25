"""Document ingestion: upload handling and the processing job (run by ingest_worker.py)."""

import asyncio
import hashlib
import logging
import os

import metrics
from rag import chunking, embeddings, extract
from rag.store import DocumentRecord, RagStore

log = logging.getLogger("voyagent.rag.ingest")

QUEUE_KEY = "rag:ingest"
EMBED_BATCH = 64


async def accept_upload(store: RagStore, tenant_id: str, title: str, data: bytes) -> tuple[DocumentRecord, bool]:
    """Validate, scan and store an upload as `pending`. Raises extract.Rejected with a safe reason."""
    mime = extract.validate(data)
    scan = await extract.scan(data)
    return await store.add_document(tenant_id, title, mime, hashlib.sha256(data).hexdigest(), data, scan)


async def process(store: RagStore, doc_id: str, embedder: embeddings.Embedder | None = None) -> None:
    """Extract, chunk (parent/child), embed and index one document. Idempotent: re-running replaces chunks."""
    embedder = embedder or embeddings.get()
    tenant_id, mime, data = await store.load_content(doc_id)
    await store.set_status(doc_id, "processing")
    try:
        text = await asyncio.to_thread(extract.extract, data, mime)
        parents = chunking.parent_child(
            text,
            parent_size=int(os.getenv("RAG_PARENT_CHARS", "2000")),
            child_size=int(os.getenv("RAG_CHILD_CHARS", "400")),
        )
        children = [c for p in parents for c in p.children]
        vectors: list[list[float]] = []
        for i in range(0, len(children), EMBED_BATCH):
            vectors.extend(await embedder.embed(children[i : i + EMBED_BATCH]))
        it = iter(vectors)
        payload = [(p.text, [(c, next(it)) for c in p.children]) for p in parents]
        await store.replace_chunks(doc_id, tenant_id, embedder.model_id, payload)
        await store.set_status(doc_id, "ready", chunk_count=len(children))
        metrics.rag_ingests.labels("ready").inc()
        log.info("document_ingested", extra={"document_id": doc_id, "chunks": len(children), "model": embedder.model_id})
    except extract.Rejected as e:
        await store.set_status(doc_id, "failed", error=str(e))
        metrics.rag_ingests.labels("failed").inc()
        log.warning("document_rejected", extra={"document_id": doc_id, "reason": str(e)})
    except Exception as e:
        await store.set_status(doc_id, "failed", error="Processing failed")
        metrics.rag_ingests.labels("failed").inc()
        log.exception("document_ingest_failed", extra={"document_id": doc_id, "error_type": type(e).__name__})


class Queue:
    """Redis list when available (shared with ingest_worker.py; SQS in AWS later), else process
    in the background of this process (local/test)."""

    def __init__(self, redis_client, store: RagStore):
        self._r = redis_client
        self._store = store
        self._tasks: set[asyncio.Task] = set()

    async def enqueue(self, doc_id: str) -> None:
        if self._r is not None:
            await self._r.lpush(QUEUE_KEY, doc_id)
            return
        task = asyncio.get_running_loop().create_task(process(self._store, doc_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for in-process jobs (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
