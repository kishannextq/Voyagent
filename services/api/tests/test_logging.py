"""Logging must be structured, correlated, and must never contain user content or secrets."""

import json
import logging
import os

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import logging_setup  # noqa: E402
import server  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging_setup.JsonFormatter("api"))
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def captured():
    handler = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


@pytest.mark.parametrize(
    "secret",
    [
        "gsk_abcdefghijklmnopqrstuvwx",
        "tvly-dev-abcdefghijklmnopqrstu",
        "sk-proj-abcdefghijklmnopqrstu",
        "AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    ],
)
def test_redacts_known_secret_formats(secret):
    out = logging_setup.redact(f"calling provider with {secret} now")
    assert secret not in out
    assert "***" in out


def test_redacts_key_value_pairs():
    assert "hunter2pass" not in logging_setup.redact('{"password": "hunter2pass"}')
    assert "abc123def456" not in logging_setup.redact("https://x/mcp/?tavilyApiKey=abc123def456")


def test_json_line_has_core_fields():
    rec = logging.LogRecord("voyagent.test", logging.INFO, __file__, 1, "something_happened", None, None)
    rec.agent = "flight_agent"
    entry = json.loads(logging_setup.JsonFormatter("api").format(rec))
    assert entry["event"] == "something_happened"
    assert entry["level"] == "INFO"
    assert entry["service"] == "api"
    assert entry["agent"] == "flight_agent"


def test_request_id_generated_and_propagated():
    client = TestClient(server.api)
    generated = client.get("/api/health").headers["x-request-id"]
    assert len(generated) == 32
    assert (
        client.get("/api/health", headers={"X-Request-ID": "trace-abc-12345"}).headers["x-request-id"]
        == "trace-abc-12345"
    )
    # Malformed IDs (header injection attempts) are replaced, not reflected.
    assert client.get("/api/health", headers={"X-Request-ID": "bad id\r\nx"}).headers["x-request-id"] != "bad id\r\nx"


def test_plan_run_logs_no_user_content(captured):
    marker = "Zanzibar-Marker-7781"
    client = TestClient(server.api)
    r = client.post("/api/plan/stream", json={"query": f"4 day trip to Paris, note {marker}"})
    assert r.status_code == 200

    assert captured, "expected log lines"
    blob = "\n".join(captured)
    assert marker not in blob
    assert "Draft itinerary" not in blob  # model output
    for line in captured:
        json.loads(line)  # every line is valid JSON
    events = {json.loads(line)["event"] for line in captured}
    assert {"http_request", "guardrail_decision", "supervisor_routed"} <= events
