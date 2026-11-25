"""Per-plan run metadata: service modes, fallback reason, which agents used dummy data.

Replaces unbounded module-level dicts (SECURITY.md F6). Backends:
- Redis (REDIS_URL): shared across replicas, entries expire after RUN_META_TTL_SECONDS.
- In-memory: bounded LRU with the same TTL, for local dev and tests only.

Contains no secrets and no user content.
"""

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

log = logging.getLogger("voyagent.run_store")

DEFAULT_TTL = int(os.getenv("RUN_META_TTL_SECONDS", str(7 * 24 * 3600)))
DEFAULT_MAX_ENTRIES = int(os.getenv("RUN_META_MAX_ENTRIES", "10000"))

RunMeta = dict[str, Any]  # {"modes": {...}, "fallback": {...}|None, "dummy_nodes": {...}}


class RunStore(Protocol):
    async def get(self, thread_id: str) -> RunMeta | None: ...
    async def save(self, thread_id: str, meta: RunMeta) -> None: ...
    async def delete(self, thread_id: str) -> None: ...


class MemoryRunStore:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES, ttl: int = DEFAULT_TTL, clock=time.monotonic):
        self._data: OrderedDict[str, tuple[float, RunMeta]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()

    async def get(self, thread_id: str) -> RunMeta | None:
        with self._lock:
            item = self._data.get(thread_id)
            if item is None:
                return None
            expires, meta = item
            if expires <= self._clock():
                del self._data[thread_id]
                return None
            self._data.move_to_end(thread_id)
            return json.loads(json.dumps(meta))  # copy: callers can't mutate stored state

    async def save(self, thread_id: str, meta: RunMeta) -> None:
        with self._lock:
            self._data[thread_id] = (self._clock() + self._ttl, json.loads(json.dumps(meta)))
            self._data.move_to_end(thread_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    async def delete(self, thread_id: str) -> None:
        with self._lock:
            self._data.pop(thread_id, None)

    def __len__(self) -> int:
        return len(self._data)


class RedisRunStore:
    PREFIX = "api:run:"

    def __init__(self, client, ttl: int = DEFAULT_TTL):
        self._r = client
        self._ttl = ttl

    # Run metadata is advisory (UI labels: live/demo, fallback notice): a Redis outage degrades
    # those labels but never fails a plan. Errors are logged and counted.

    async def get(self, thread_id: str) -> RunMeta | None:
        try:
            raw = await self._r.get(self.PREFIX + thread_id)
        except Exception as e:
            _degraded("get", e)
            return None
        return json.loads(raw) if raw else None

    async def save(self, thread_id: str, meta: RunMeta) -> None:
        try:
            await self._r.set(self.PREFIX + thread_id, json.dumps(meta), ex=self._ttl)
        except Exception as e:
            _degraded("save", e)

    async def delete(self, thread_id: str) -> None:
        try:
            await self._r.delete(self.PREFIX + thread_id)
        except Exception as e:
            _degraded("delete", e)


def _degraded(op: str, e: Exception) -> None:
    log.warning("run_store_unavailable", extra={"op": op, "error_type": type(e).__name__})


def build(redis_client) -> RunStore:
    """Redis-backed when a client is given (redis_conn.connect enforces the APP_ENV policy)."""
    if redis_client is not None:
        log.info("run_store_ready", extra={"backend": "redis"})
        return RedisRunStore(redis_client)
    log.warning("run_store_ready", extra={"backend": "memory", "note": "local/test only"})
    return MemoryRunStore()
