"""Alembic migrations + Postgres thread store against a real database (TEST_DATABASE_URL, CI)."""

import asyncio
import os
import sys
import uuid

import pytest

import migrate
import threads

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _run(coro):
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(coro, loop_factory=factory)


def test_store_needs_database_outside_local(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        _run(threads.open_store(None))


def test_migrate_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert migrate.main() == 2


@pg
def test_unmigrated_database_fails_fast():
    import psycopg

    name = "nomig_" + uuid.uuid4().hex[:8]
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    try:
        url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/" + name
        with pytest.raises(RuntimeError, match="run migrations"):
            _run(threads.open_store(url))
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE {name}")


@pg
def test_migrations_are_idempotent_and_store_works(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    assert migrate.main() == 0
    assert migrate.main() == 0  # re-running is a no-op

    async def go():
        store = await threads.open_store(TEST_DATABASE_URL)
        try:
            tenant = "t-" + uuid.uuid4().hex[:8]
            a = await store.create(uuid.uuid4().hex, tenant, "alice")
            with pytest.raises(threads.ThreadExists):
                await store.create(a.thread_id, tenant, "bob")
            b = await store.create(uuid.uuid4().hex, tenant, "alice")
            await store.create(uuid.uuid4().hex, tenant, "bob")
            assert (await store.get(a.thread_id)).owner_sub == "alice"
            mine = await store.list(tenant, "alice", 10)
            assert [r.thread_id for r in mine] == [b.thread_id, a.thread_id]
            assert len(await store.list(tenant, None, 10)) == 3
            assert [r.thread_id for r in await store.list(tenant, "alice", 10, before=b.created_at)] == [a.thread_id]
        finally:
            await store.aclose()

    _run(go())


@pg
def test_thread_id_format_enforced_by_database():
    import psycopg

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO app.threads (thread_id, tenant_id, owner_sub) VALUES ('../x', 't', 'u')")
