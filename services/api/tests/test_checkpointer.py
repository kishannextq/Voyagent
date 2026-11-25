"""Checkpointer policy (no DB needed) and a real-Postgres integration test.

The integration test runs when TEST_DATABASE_URL is set (CI provides a Postgres service).
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

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


def test_memory_checkpointer_locally(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    assert isinstance(graph.build_checkpointer(None), MemorySaver)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_database_required_outside_local(monkeypatch, env):
    monkeypatch.setenv("APP_ENV", env)
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        graph.build_checkpointer(None)


def test_unreachable_database_fails_instead_of_falling_back(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("PG_POOL_TIMEOUT", "2")
    with pytest.raises(PoolTimeout):
        graph.build_checkpointer("postgresql://nobody:nopass@127.0.0.1:1/none")


# ---------------- integration ----------------

pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _run_plan(app, thread_id: str, query: str):
    cfg = {"configurable": {"thread_id": thread_id}}
    token = keys.set_current(keys.ApiKeys())
    keys.start_run()
    try:
        list(app.stream({"messages": [HumanMessage(content=query)], "user_query": query}, cfg, stream_mode="updates"))
        return app.get_state(cfg)
    finally:
        keys.reset_current(token)


@pg
def test_postgres_state_survives_new_instance_and_concurrency(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    replica_a = graph.build_graph(graph.build_checkpointer(TEST_DATABASE_URL))
    replica_b = graph.build_graph(graph.build_checkpointer(TEST_DATABASE_URL))

    # Concurrent runs through one pooled saver (the old single shared connection broke here).
    ids = [uuid.uuid4().hex for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        snaps = list(ex.map(lambda t: _run_plan(replica_a, t, "4 day trip to Kyoto"), ids))
    assert all(s.next == ("human_approval",) for s in snaps)

    # Another "replica" sees the paused plan and can resume it.
    cfg = {"configurable": {"thread_id": ids[0]}}
    assert replica_b.get_state(cfg).values["trip_constraints"]["destination"] == "Kyoto"
    token = keys.set_current(keys.ApiKeys())
    keys.start_run()
    try:
        list(replica_b.stream(Command(resume={"approved": True, "feedback": ""}), cfg, stream_mode="updates"))
    finally:
        keys.reset_current(token)
    assert "approved" in replica_a.get_state(cfg).values["final_response"].lower()
