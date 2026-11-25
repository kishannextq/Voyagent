"""Document search as an agent tool (allow-listed as `rag.search` in agents/*.md)."""

import logging
import os

import agent_config
import keys
import metrics
import telemetry
from rag import retrieval
from rag.store import RagStore

log = logging.getLogger("voyagent.rag.tool")

_store: RagStore | None = None


def configure(store: RagStore | None) -> None:
    global _store
    _store = store


def enabled() -> bool:
    return os.getenv("RAG_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


async def search_documents(query: str, k: int = 4) -> str:
    """Search the caller's organisation documents; returns a citable, delimited block or ''.

    Failures never break the plan: retrieval problems are logged and the agent continues
    without documents.
    """
    agent_config.check_tool("rag", "search")
    tenant = keys.tenant()
    if not (enabled() and _store and tenant):
        return ""
    try:
        with telemetry.tracer.start_as_current_span("rag.search"):
            passages = await retrieval.search(_store, tenant, query, k=k)
    except Exception as e:
        log.warning("rag_search_failed", extra={"error_type": type(e).__name__})
        metrics.rag_searches.labels("error").inc()
        return ""
    metrics.rag_searches.labels("hits" if passages else "empty").inc()
    log.info("rag_search", extra={"hits": len(passages)})
    return retrieval.format_for_prompt(passages)
