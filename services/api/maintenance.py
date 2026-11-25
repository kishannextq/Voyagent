"""Data retention jobs (run on a schedule, e.g. an ECS scheduled task, daily).

    DATABASE_URL=... python maintenance.py purge

- Expired long-term memory rows (memory.py) are deleted.
- Plans older than PLAN_RETENTION_DAYS (default 365) are deleted with their checkpoints.
  Batches of PURGE_BATCH (default 500) per run, so a backlog is worked off over several runs.
Run metadata in Redis expires on its own (RUN_META_TTL_SECONDS).
"""

import asyncio
import json
import os
import sys

from psycopg.rows import dict_row


async def purge(database_url: str) -> dict:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection

    import memory

    days = int(os.getenv("PLAN_RETENTION_DAYS", "365"))
    batch = int(os.getenv("PURGE_BATCH", "500"))
    async with await AsyncConnection.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        cur = await conn.execute(
            "SELECT thread_id FROM app.threads WHERE created_at < now() - make_interval(days => %s) "
            "ORDER BY created_at LIMIT %s",
            (days, batch),
        )
        old = [r["thread_id"] for r in await cur.fetchall()]
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        for thread_id in old:
            await saver.adelete_thread(thread_id)
    async with await AsyncConnection.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        if old:
            await conn.execute("DELETE FROM app.threads WHERE thread_id = ANY(%s)", (old,))
    import db

    pool = await db.open_pool(database_url, "maintenance", required_table="app.user_memory")
    try:
        expired = await memory.PostgresMemoryStore(pool).purge_expired()
    finally:
        await pool.close()
    return {"plans_deleted": len(old), "memory_rows_deleted": expired}


def main(argv: list[str]) -> int:
    url = os.environ.get("DATABASE_URL", "")
    if argv[:1] != ["purge"] or not url:
        print("usage: DATABASE_URL=... python maintenance.py purge", file=sys.stderr)
        return 2
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    print(json.dumps(asyncio.run(purge(url), loop_factory=factory)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
