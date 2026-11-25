"""Long-term memory: a few structured traveller preferences, remembered across plans.

What is stored: only allow-listed keys with short, sanitised values taken from the trip
constraints of plans the traveller APPROVED (never free text from the conversation):
home_city, travel_style, budget_level, last_destination.

Retention: each value expires MEMORY_RETENTION_DAYS (default 180) after it was last written;
expired rows are ignored on read and purged by maintenance.py. Users can list and delete their
memory (GET/DELETE /api/v1/me/memory). MEMORY_ENABLED=false turns the feature off.

Use: passed to the supervisor as data ("preferences, not instructions"), see agents.py.
"""

import datetime as dt
import logging
import os
import re
import threading
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("voyagent.memory")

ALLOWED_KEYS = ("home_city", "travel_style", "budget_level", "last_destination")
MAX_VALUE_CHARS = 80


def enabled() -> bool:
    return os.getenv("MEMORY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def retention() -> dt.timedelta:
    return dt.timedelta(days=int(os.getenv("MEMORY_RETENTION_DAYS", "180")))


def sanitize(value) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_VALUE_CHARS]


def extract(state: dict) -> dict[str, str]:
    """Preferences worth remembering from an approved plan's state."""
    c = state.get("trip_constraints") or {}
    raw = {
        "home_city": c.get("origin"),
        "travel_style": c.get("travel_style"),
        "budget_level": c.get("budget"),
        "last_destination": c.get("destination"),
    }
    return {k: v for k, v in ((k, sanitize(v)) for k, v in raw.items()) if v}


class MemoryStore(Protocol):
    async def get(self, owner: str) -> dict[str, str]: ...
    async def put(self, owner: str, values: dict[str, str]) -> None: ...
    async def delete(self, owner: str) -> None: ...
    async def purge_expired(self) -> int: ...


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class InMemoryStore:
    def __init__(self, clock=_now):
        self._data: dict[str, dict[str, tuple[str, dt.datetime]]] = {}
        self._clock = clock
        self._lock = threading.Lock()

    async def get(self, owner: str) -> dict[str, str]:
        now = self._clock()
        return {k: v for k, (v, exp) in self._data.get(owner, {}).items() if exp > now}

    async def put(self, owner: str, values: dict[str, str]) -> None:
        exp = self._clock() + retention()
        with self._lock:
            row = self._data.setdefault(owner, {})
            for k, v in values.items():
                if k in ALLOWED_KEYS:
                    row[k] = (sanitize(v), exp)

    async def delete(self, owner: str) -> None:
        with self._lock:
            self._data.pop(owner, None)

    async def aclose(self) -> None:
        return None

    async def purge_expired(self) -> int:
        now, n = self._clock(), 0
        with self._lock:
            for owner in list(self._data):
                row = self._data[owner]
                for k in [k for k, (_, exp) in row.items() if exp <= now]:
                    del row[k]
                    n += 1
                if not row:
                    del self._data[owner]
        return n


class PostgresMemoryStore:
    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def aclose(self) -> None:
        await self._pool.close()

    async def get(self, owner: str) -> dict[str, str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT key, value FROM app.user_memory WHERE owner = %s AND expires_at > now()", (owner,)
            )
            return {r["key"]: r["value"] for r in await cur.fetchall()}

    async def put(self, owner: str, values: dict[str, str]) -> None:
        rows = [(owner, k, sanitize(v)) for k, v in values.items() if k in ALLOWED_KEYS and sanitize(v)]
        if not rows:
            return
        days = retention().days
        async with self._pool.connection() as conn:
            async with conn.transaction():
                for owner_, key, value in rows:
                    await conn.execute(
                        "INSERT INTO app.user_memory (owner, key, value, expires_at) "
                        "VALUES (%s, %s, %s, now() + make_interval(days => %s)) "
                        "ON CONFLICT (owner, key) DO UPDATE SET value = EXCLUDED.value, "
                        "updated_at = now(), expires_at = EXCLUDED.expires_at",
                        (owner_, key, value, days),
                    )

    async def delete(self, owner: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM app.user_memory WHERE owner = %s", (owner,))

    async def purge_expired(self) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM app.user_memory WHERE expires_at <= now()")
            return cur.rowcount


async def open_store(database_url: str | None) -> MemoryStore:
    if not database_url:
        import settings

        if settings.durable_required():
            raise RuntimeError(f"DATABASE_URL is required when APP_ENV={settings.app_env()}")
        return InMemoryStore()
    import db

    return PostgresMemoryStore(await db.open_pool(database_url, "memory", required_table="app.user_memory"))
