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
    return TestClient(server.api)


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
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mock_mode"] is True
    assert set(body["modes"].values()) == {"mock"}


def test_rejects_short_query(client):
    assert client.post("/api/plan/stream", json={"query": "hi"}).status_code == 422


def test_full_plan_stream_then_approve(client):
    r = client.post("/api/plan/stream", json={"query": "5 day trip to Tokyo from Delhi under 1 lakh"})
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

    r = client.post(f"/api/plan/{thread_id}/approve/stream", json={"approved": True, "feedback": ""})
    events = parse_sse(r.text)
    final = events[-1]
    assert final[0] == "done"
    assert final[1]["status"] == "completed"
    assert "approved" in final[1]["state"]["final_response"].lower()

    # Thread is no longer awaiting approval.
    r = client.post(f"/api/plan/{thread_id}/approve/stream", json={"approved": True})
    assert r.status_code == 409


def test_guardrail_blocks_off_topic(client):
    r = client.post("/api/plan", json={"query": "write me a poem about the sea"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked"
    assert body["interrupt"] is None


def test_unknown_thread_404(client):
    assert client.get("/api/plan/does_not_exist").status_code == 404
