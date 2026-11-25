"""Who owns each plan (thread), used for access checks and listing (SECURITY.md F2).

Backends:
- Postgres (DATABASE_URL): table app.threads, created by Alembic migrations (migrations/).
- In-memory: local/test only (same APP_ENV policy as the checkpointer).

Stores no user content: thread ID, tenant, owner subject, creation time.
"""

import datetime as dt
import logging
import os
import threading
from dataclasses import dataclass
from typing import Protocol

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import settings

log = logging.getLogger("voyagent.threads")


@dataclass(frozen=True)
class ThreadRecord:
    thread_id: str
    tenant_id: str
    owner_sub: str
    created_at: dt.datetime


class ThreadExists(Exception):
    pass


class ThreadStore(Protocol):
    async def create(self, thread_id: str, tenant_id: str, owner_sub: str) -> ThreadRecord: ...
    async def get(self, thread_id: str) -> ThreadRecord | None: ...
    async def list(
        self, tenant_id: str, owner_sub: str | None, limit: int, before: dt.datetime | None = None
    ) -> list[ThreadRecord]: ...
    async def aclose(self) -> None: ...


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class MemoryThreadStore:
    def __init__(self, max_entries: int = 100_000):
        self._data: dict[str, ThreadRecord] = {}
        self._max = max_entries
        self._lock = threading.Lock()

    async def create(self, thread_id: str, tenant_id: str, owner_sub: str) -> ThreadRecord:
        with self._lock:
            if thread_id in self._data:
                raise ThreadExists(thread_id)
            if len(self._data) >= self._max:
                self._data.pop(next(iter(self._data)))  # bounded: drop the oldest
            rec = ThreadRecord(thread_id, tenant_id, owner_sub, _now())
            self._data[thread_id] = rec
            return rec

    async def get(self, thread_id: str) -> ThreadRecord | None:
        return self._data.get(thread_id)

    async def list(self, tenant_id, owner_sub, limit, before=None):
        rows = [
            r
            for r in self._data.values()
            if r.tenant_id == tenant_id
            and (owner_sub is None or r.owner_sub == owner_sub)
            and (before is None or r.created_at < before)
        ]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]

    async def aclose(self) -> None:
        return None


class PostgresThreadStore:
    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def create(self, thread_id: str, tenant_id: str, owner_sub: str) -> ThreadRecord:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO app.threads (thread_id, tenant_id, owner_sub) VALUES (%s, %s, %s) "
                "ON CONFLICT (thread_id) DO NOTHING RETURNING thread_id, tenant_id, owner_sub, created_at",
                (thread_id, tenant_id, owner_sub),
            )
            row = await cur.fetchone()
        if row is None:
            raise ThreadExists(thread_id)
        return ThreadRecord(**row)

    async def get(self, thread_id: str) -> ThreadRecord | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT thread_id, tenant_id, owner_sub, created_at FROM app.threads WHERE thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
        return ThreadRecord(**row) if row else None

    async def list(self, tenant_id, owner_sub, limit, before=None):
        sql = "SELECT thread_id, tenant_id, owner_sub, created_at FROM app.threads WHERE tenant_id = %s"
        params: list = [tenant_id]
        if owner_sub is not None:
            sql += " AND owner_sub = %s"
            params.append(owner_sub)
        if before is not None:
            sql += " AND created_at < %s"
            params.append(before)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        return [ThreadRecord(**r) for r in rows]

    async def aclose(self) -> None:
        await self._pool.close()


async def open_store(database_url: str | None) -> ThreadStore:
    """Postgres when DATABASE_URL is set (schema must be migrated: fails fast otherwise)."""
    if not database_url:
        if settings.durable_required():
            raise RuntimeError(f"DATABASE_URL is required when APP_ENV={settings.app_env()}")
        log.warning("thread_store_ready", extra={"backend": "memory", "note": "local/test only"})
        return MemoryThreadStore()
    timeout = float(os.getenv("PG_POOL_TIMEOUT", "10"))
    pool = AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=int(os.getenv("PG_APP_POOL_MAX", "5")),
        timeout=timeout,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "connect_timeout": 5},
        open=False,
        name="app",
    )
    try:
        await pool.open(wait=True, timeout=timeout)
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT to_regclass('app.threads') AS t")
            if (await cur.fetchone())["t"] is None:
                raise RuntimeError("Database schema missing: run migrations (python migrate.py)")
    except BaseException:
        await pool.close()
        raise
    log.info("thread_store_ready", extra={"backend": "postgres"})
    return PostgresThreadStore(pool)
