"""RAG ingest worker: processes uploaded documents from the Redis queue.

Same image as the API:  python ingest_worker.py
Needs DATABASE_URL, REDIS_URL and the RAG_EMBEDDER settings. Scale horizontally; each job is
idempotent (re-processing a document replaces its chunks).
"""

import asyncio
import logging
import os
import signal

import logging_setup

logging_setup.configure("ingest-worker")

import redis_conn  # noqa: E402
from rag import ingest  # noqa: E402
from rag import store as rag_store  # noqa: E402

log = logging.getLogger("voyagent.ingest_worker")


async def run(stop: asyncio.Event) -> None:
    redis = await redis_conn.connect()
    if redis is None:
        raise RuntimeError("REDIS_URL is required for the ingest worker")
    store = await rag_store.open_store(os.getenv("DATABASE_URL"))
    log.info("ingest_worker_ready")
    try:
        while not stop.is_set():
            item = await redis.brpop([ingest.QUEUE_KEY], timeout=2)
            if item is None:
                continue
            doc_id = item[1].decode() if isinstance(item[1], bytes) else item[1]
            await ingest.process(store, doc_id)
    finally:
        await store.aclose()
        await redis.aclose()


def main() -> None:
    stop = asyncio.Event()

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        await run(stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
