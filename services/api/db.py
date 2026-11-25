"""Async Postgres pools for application tables (schema `app`, managed by Alembic)."""

import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def open_pool(database_url: str, name: str, required_table: str | None = None) -> AsyncConnectionPool:
    """Open a pool and fail fast if the database is unreachable or the schema isn't migrated."""
    timeout = float(os.getenv("PG_POOL_TIMEOUT", "10"))
    pool = AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=int(os.getenv("PG_APP_POOL_MAX", "5")),
        timeout=timeout,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "connect_timeout": 5},
        open=False,
        name=name,
    )
    try:
        await pool.open(wait=True, timeout=timeout)
        if required_table:
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT to_regclass(%s) AS t", (required_table,))
                if (await cur.fetchone())["t"] is None:
                    raise RuntimeError("Database schema missing: run migrations (python migrate.py)")
    except BaseException:
        await pool.close()
        raise
    return pool
