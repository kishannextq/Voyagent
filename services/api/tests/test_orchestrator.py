"""API <-> internal orchestrator service (remote mode), and plan deletion."""

import asyncio
import json
import os

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import logging_setup  # noqa: E402
import orchestrator  # noqa: E402
import orchestrator_app  # noqa: E402
import server  # noqa: E402

TOKEN = "orch-test-" + "token-123"
PLAN = {"query": "4 day trip to Kyoto"}


def _events(text: str) -> list[str]:
    return [b.split("\n", 1)[0][7:] for b in text.split("\n\n") if b.startswith("event: ")]


async def _with_remote(monkeypatch, body, token=TOKEN):
    """Run the API with a RemoteOrchestrator wired to the real orchestrator app (same loop)."""
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", TOKEN)

    async def open_remote():
        transport = httpx.ASGITransport(app=orchestrator_app.app)
        return orchestrator.RemoteOrchestrator("http://orchestrator", token, transport=transport)

    monkeypatch.setattr(orchestrator, "open_from_env", open_remote)
    async with orchestrator_app.lifespan(orchestrator_app.app), server.lifespan(server.api):
        assert isinstance(server.orch, orchestrator.RemoteOrchestrator)
        transport = httpx.ASGITransport(app=server.api)
        async with httpx.AsyncClient(transport=transport, base_url="http://api", timeout=60) as client:
            return await body(client)


def test_full_plan_through_remote_orchestrator(monkeypatch):
    async def body(c):
        r = await c.post("/api/v1/plan/stream", json=PLAN)
        assert r.status_code == 200
        ev = _events(r.text)
        assert ev[0] == "start" and "interrupt" in ev and ev[-1] == "done"
        tid = json.loads(r.text.split("data: ", 1)[1].split("\n", 1)[0])["thread_id"]

        snap = (await c.get(f"/api/v1/plan/{tid}")).json()
        assert snap["status"] == "awaiting_approval" and snap["state"]["trip_constraints"]["destination"] == "Kyoto"

        r = await c.post(f"/api/v1/plan/{tid}/approve", json={"approved": True})
        assert r.status_code == 200 and r.json()["status"] == "completed"

        assert (await c.get("/api/v1/health")).json()["modes"]["llm"] == "mock"  # modes come from the orchestrator

        assert (await c.delete(f"/api/v1/plans/{tid}")).status_code == 204
        assert (await c.get(f"/api/v1/plan/{tid}")).status_code == 404
        assert not await orchestrator_app.orch.exists(tid)

    asyncio.run(_with_remote(monkeypatch, body))


def test_wrong_service_token_is_rejected(monkeypatch):
    async def body(c):
        r = await c.post("/api/v1/plan/stream", json=PLAN)
        assert r.status_code == 502
        assert r.json() == {"detail": "Planner temporarily unavailable"}

    asyncio.run(_with_remote(monkeypatch, body, token="wrong-token-value"))


def test_orchestrator_busy_maps_to_503(monkeypatch):
    async def body(c):
        orchestrator_app.orch.slots = asyncio.Semaphore(0)
        r = await c.post("/api/v1/plan/stream", json=PLAN)
        assert r.status_code == 503 and r.headers["retry-after"] == "5"

    asyncio.run(_with_remote(monkeypatch, body))


def test_internal_api_requires_token_and_validates_input(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", TOKEN)
    with TestClient(orchestrator_app.app) as c:
        assert c.get("/internal/v1/health").status_code == 200
        assert c.get("/internal/v1/modes").status_code == 401
        assert c.get("/internal/v1/modes", headers={"X-Internal-Token": "nope"}).status_code == 401
        h = {"X-Internal-Token": TOKEN}
        assert c.get("/internal/v1/modes", headers=h).status_code == 200
        assert c.get("/internal/v1/threads/..%2Fetc", headers=h).status_code in (404, 422)
        assert c.get("/internal/v1/threads/bad id here", headers=h).status_code == 422
        r = c.post("/internal/v1/threads/abcdefgh12/runs", json={"kind": "start", "payload": {}}, headers=h)
        assert r.status_code == 422  # payload missing query
        r = c.post("/internal/v1/threads/abcdefgh12/runs", json={"kind": "rm", "payload": {}}, headers=h)
        assert r.status_code == 422
        assert c.get("/openapi.json").status_code == 404  # no public docs for the internal API


@pytest.mark.parametrize("env", ["staging", "production"])
def test_orchestrator_token_required_outside_local(monkeypatch, env):
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.delenv("ORCHESTRATOR_TOKEN", raising=False)

    async def go():
        async with orchestrator_app.lifespan(orchestrator_app.app):
            pass

    with pytest.raises(RuntimeError, match="ORCHESTRATOR_TOKEN is required"):
        asyncio.run(go())


def test_api_requires_token_for_remote_orchestrator(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orchestrator:8001")
    monkeypatch.delenv("ORCHESTRATOR_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ORCHESTRATOR_TOKEN is required"):
        asyncio.run(orchestrator.open_from_env())


def test_request_id_forwarded_to_orchestrator():
    remote = orchestrator.RemoteOrchestrator("http://o", TOKEN)
    token = logging_setup.request_id_var.set("req-abc-12345")
    try:
        h = remote._headers()
        assert h["X-Internal-Token"] == TOKEN and h["X-Request-ID"] == "req-abc-12345"
    finally:
        logging_setup.request_id_var.reset(token)
        asyncio.run(remote.aclose())


# ---------------- deletion (in-process mode) ----------------


def test_delete_plan_removes_state_and_ownership():
    with TestClient(server.api) as c:
        tid = c.post("/api/v1/plan", json=PLAN).json()["thread_id"]
        assert asyncio.run(server.orch.runs.get(tid)) is not None
        assert c.delete(f"/api/v1/plans/{tid}").status_code == 204
        assert c.get(f"/api/v1/plan/{tid}").status_code == 404
        assert tid not in [p["thread_id"] for p in c.get("/api/v1/plans").json()["items"]]
        assert asyncio.run(server.orch.runs.get(tid)) is None
        assert c.delete(f"/api/v1/plans/{tid}").status_code == 404


def test_lost_orchestrator_stream_ends_with_error_event():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: start\ndata: {}\n\n"
            raise httpx.RemoteProtocolError("peer closed connection")

    def handler(request):
        return httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})

    async def go():
        remote = orchestrator.RemoteOrchestrator("http://o", TOKEN, transport=httpx.MockTransport(handler))
        stream = await remote.start("abcdefgh12", "start", {"query": "x"}, "t:u")
        text = "".join([chunk async for chunk in stream])
        await remote.aclose()
        return text

    text = asyncio.run(go())
    assert text.startswith("event: start") and "event: error" in text and "stopped unexpectedly" in text
