"""Checkpointer policy (no DB needed) and a real-Postgres integration test.

The integration test runs when TEST_DATABASE_URL is set (CI provides a Postgres service).
"""

import asyncio
import os
import sys
import uuid

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import pytest  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from psycopg_pool import PoolTimeout  # noqa: E402

import graph  # noqa: E402
import keys  # noqa: E402

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _run(coro):
    # psycopg async can't use Windows' default ProactorEventLoop.
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    return asyncio.run(coro, loop_factory=factory)


def test_memory_checkpointer_locally(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    saver, _ = _run(graph.open_checkpointer(None))
    assert isinstance(saver, MemorySaver)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_database_required_outside_local(monkeypatch, env):
    monkeypatch.setenv("APP_ENV", env)
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        _run(graph.open_checkpointer(None))


def test_unreachable_database_fails_instead_of_falling_back(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("PG_POOL_TIMEOUT", "2")
    with pytest.raises(PoolTimeout):
        _run(graph.open_checkpointer("postgresql://nobody:nopass@127.0.0.1:1/none"))


# ---------------- integration ----------------

pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


async def _run_plan(app, thread_id: str, query: str):
    cfg = {"configurable": {"thread_id": thread_id}}
    keys.set_current(keys.ApiKeys())  # each asyncio task has its own context copy
    keys.start_run()
    async for _ in app.astream(
        {"messages": [HumanMessage(content=query)], "user_query": query}, cfg, stream_mode="updates"
    ):
        pass
    return await app.aget_state(cfg)


@pg
def test_postgres_state_survives_new_instance_and_concurrency(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")

    async def scenario():
        replica_a, close_a = await graph.open_graph(TEST_DATABASE_URL)
        replica_b, close_b = await graph.open_graph(TEST_DATABASE_URL)
        try:
            # Concurrent runs through one pooled saver (the old single shared connection broke here).
            ids = [uuid.uuid4().hex for _ in range(6)]
            snaps = await asyncio.gather(*(_run_plan(replica_a, t, "4 day trip to Kyoto") for t in ids))
            assert all(s.next == ("human_approval",) for s in snaps)

            # Another "replica" sees the paused plan and can resume it.
            cfg = {"configurable": {"thread_id": ids[0]}}
            assert (await replica_b.aget_state(cfg)).values["trip_constraints"]["destination"] == "Kyoto"
            keys.set_current(keys.ApiKeys())
            keys.start_run()
            async for _ in replica_b.astream(
                Command(resume={"approved": True, "feedback": ""}), cfg, stream_mode="updates"
            ):
                pass
            assert "approved" in (await replica_a.aget_state(cfg)).values["final_response"].lower()

            # Deleting a plan removes its checkpoints for every replica.
            await replica_b.checkpointer.adelete_thread(ids[0])
            assert not (await replica_a.aget_state(cfg)).values
        finally:
            await close_a()
            await close_b()

    _run(scenario())


def test_database_url_built_from_parts(monkeypatch):
    import importlib

    import config

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_USER", "voyagent_admin")
    monkeypatch.setenv("DB_PASSWORD", "p@ss/w:rd")
    try:
        url = importlib.reload(config).DATABASE_URL
        assert url == "postgresql://voyagent_admin:p%40ss%2Fw%3Ard@db.internal:5432/voyagent?sslmode=require"
        assert os.environ["DATABASE_URL"] == url
    finally:
        monkeypatch.delenv("DB_HOST")
        os.environ.pop("DATABASE_URL", None)
        importlib.reload(config)
