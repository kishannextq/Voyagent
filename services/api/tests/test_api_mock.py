"""End-to-end API tests in mock mode.

These drive the real FastAPI app and LangGraph graph (routing, guardrail branch,
human-in-the-loop interrupt, checkpointing); only leaf LLM/tool calls are canned.
They pin the SSE contract the web app depends on before the Phase 2 refactor.
"""

import json
import os

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"
os.environ.pop("DATABASE_URL", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

SPECIALISTS = {"flight_agent", "hotel_agent", "weather_agent", "budget_agent", "itinerary_agent"}


@pytest.fixture(scope="module")
def client():
    with TestClient(server.api) as c:
        yield c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mock_mode"] is True
    assert set(body["modes"].values()) == {"mock"}
    # Health must not reveal which provider keys the server holds.
    assert "server_keys" not in body


def test_rejects_short_query(client):
    assert client.post("/api/v1/plan/stream", json={"query": "hi"}).status_code == 422


def test_rejects_oversized_query(client):
    assert client.post("/api/v1/plan", json={"query": "x" * 2001}).status_code == 422


@pytest.mark.parametrize("path", ["/api/v1/plan", "/api/v1/plan/stream"])
def test_rejects_client_supplied_keys_on_plan(client, path):
    r = client.post(path, json={"query": "3 days in Paris", "api_keys": {"groq": "gsk_fake"}})
    assert r.status_code == 422
    assert "gsk_fake" not in r.text


def test_rejects_client_supplied_keys_on_approval(client):
    done = parse_sse(client.post("/api/v1/plan/stream", json={"query": "3 days in Paris"}).text)[-1][1]
    r = client.post(
        f"/api/v1/plan/{done['thread_id']}/approve/stream",
        json={"approved": True, "api_keys": {"groq": "gsk_fake"}},
    )
    assert r.status_code == 422


def test_server_keys_are_ignored_in_mock_mode(client, monkeypatch):
    for var in ("GROQ_API_KEY", "TAVILY_API_KEY", "AVIATION_STACK_API_KEY", "OPENWEATHER_API_KEY"):
        monkeypatch.setenv(var, "server-secret-value")
    assert set(client.get("/api/v1/health").json()["modes"].values()) == {"mock"}


def test_full_plan_stream_then_approve(client):
    # Exact body shape apps/web/lib/api.ts sends (the API rejects unknown fields).
    r = client.post(
        "/api/v1/plan/stream",
        json={"query": "5 day trip to Tokyo from Delhi under 1 lakh", "user_id": "demo_user"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    names = [e for e, _ in events]

    assert names[0] == "start"
    assert "interrupt" in names
    assert names[-1] == "done"
    nodes = {d["node"] for e, d in events if e == "node"}
    assert {"supervisor"} | SPECIALISTS <= nodes

    done = events[-1][1]
    assert done["status"] == "awaiting_approval"
    assert done["state"]["trip_constraints"]["destination"] == "Tokyo"
    thread_id = done["thread_id"]

    r = client.post(f"/api/v1/plan/{thread_id}/approve/stream", json={"approved": True, "feedback": ""})
    events = parse_sse(r.text)
    final = events[-1]
    assert final[0] == "done"
    assert final[1]["status"] == "completed"
    assert "approved" in final[1]["state"]["final_response"].lower()

    # Thread is no longer awaiting approval.
    r = client.post(f"/api/v1/plan/{thread_id}/approve/stream", json={"approved": True})
    assert r.status_code == 409


def test_guardrail_blocks_off_topic(client):
    r = client.post("/api/v1/plan", json={"query": "write me a poem about the sea"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert body["interrupt"] is None


def test_unknown_thread_404(client):
    assert client.get("/api/v1/plan/does_not_exist").status_code == 404
