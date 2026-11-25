"""Async runtime (SECURITY.md F7): persistent MCP sessions, non-blocking runs, execution bounds."""

import asyncio
import os
import pathlib
import sys

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import graph  # noqa: E402
import mcp_client  # noqa: E402
import mock  # noqa: E402
import server  # noqa: E402

PID_SERVER = str(pathlib.Path(__file__).parent / "pid_mcp_server.py")


@pytest.fixture
def pid_server(monkeypatch):
    """Route the 'openweather' service to the local PID server (real stdio subprocess)."""

    def connection(service, key):
        return {"transport": "stdio", "command": sys.executable, "args": [PID_SERVER], "env": mcp_client._server_env()}

    monkeypatch.setattr(mcp_client, "_connection", connection)


def _text(raw) -> str:
    # langchain-mcp-adapters returns content blocks; take the text.
    if isinstance(raw, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
    return str(raw)


def test_mcp_calls_reuse_one_subprocess(pid_server):
    async def go():
        pool = mcp_client.pool()
        try:
            first = _text(await pool.call("openweather", "k1", "pid", {}))
            # Concurrent calls share the same session/subprocess.
            rest = await asyncio.gather(*(pool.call("openweather", "k1", "pid", {}) for _ in range(5)))
            assert {_text(r) for r in rest} == {first}
            assert _text(await pool.call("openweather", "k1", "echo", {"text": "hi"})) == "hi"
            assert len(pool) == 1

            # A key change replaces the session (new subprocess).
            rotated = _text(await pool.call("openweather", "k2", "pid", {}))
            assert rotated != first
            assert len(pool) == 1
        finally:
            await mcp_client.aclose_sessions()

    asyncio.run(go())


def test_dead_session_is_replaced(pid_server):
    async def go():
        pool = mcp_client.pool()
        try:
            first = _text(await pool.call("openweather", "k", "pid", {}))
            session = pool._sessions["openweather"]
            await session.aclose()  # e.g. the server process died
            assert session.closed
            assert _text(await pool.call("openweather", "k", "pid", {})) != first
        finally:
            await mcp_client.aclose_sessions()

    asyncio.run(go())


def test_connect_failure_is_redacted_and_not_cached(monkeypatch):
    secret = "tvly-" + "should-not-leak-123"

    def connection(service, key):
        return {"transport": "stdio", "command": "definitely-not-a-real-binary-" + key, "args": [], "env": {}}

    monkeypatch.setattr(mcp_client, "_connection", connection)

    async def go():
        pool = mcp_client.pool()
        try:
            with pytest.raises(RuntimeError) as exc:
                await pool.call("openweather", secret, "pid", {})
            assert secret not in str(exc.value)
            assert len(pool) == 0  # next call retries instead of reusing a broken session
        finally:
            await mcp_client.aclose_sessions()

    asyncio.run(go())


async def _plan(client: httpx.AsyncClient, query: str) -> str:
    r = await client.post("/api/plan/stream", json={"query": query})
    assert r.status_code == 200
    return r.text


def test_concurrent_runs_do_not_block_each_other(monkeypatch):
    """Runs share one event loop: while one waits on the model/tools, others progress."""
    inflight = 0
    peak = 0

    async def tracking_delay():
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.02)
        inflight -= 1

    monkeypatch.setattr(mock, "_delay", tracking_delay)

    async def go():
        async with server.lifespan(server.api):
            transport = httpx.ASGITransport(app=server.api)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                bodies = await asyncio.gather(*(_plan(client, f"{d} day trip to Paris") for d in range(3, 7)))
        assert all("event: interrupt" in b for b in bodies)

    asyncio.run(go())
    assert peak >= 4  # all four runs waiting at once; a blocking call in any node would serialize them


def test_run_timeout_stops_the_plan(monkeypatch):
    async def slow_delay():
        await asyncio.sleep(5)

    monkeypatch.setattr(mock, "_delay", slow_delay)
    monkeypatch.setattr(server, "RUN_TIMEOUT_SECONDS", 0.3)
    with TestClient(server.api) as client:
        r = client.post("/api/plan/stream", json={"query": "3 days in Rome"})
    assert "event: error" in r.text
    assert "longer than" in r.text
    assert "event: done" not in r.text


def test_busy_server_returns_503(monkeypatch):
    with TestClient(server.api) as client:
        monkeypatch.setattr(server, "_run_slots", asyncio.Semaphore(0))
        r = client.post("/api/plan/stream", json={"query": "3 days in Rome"})
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
        assert client.get("/api/health").status_code == 200  # reads are unaffected


def test_runs_have_a_step_limit():
    assert server._config("abc")["recursion_limit"] == graph.RECURSION_LIMIT <= 30
