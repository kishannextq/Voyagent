import asyncio
import os
import pathlib

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402

import mcp_client  # noqa: E402
import server  # noqa: E402

# Fake values (assembled so secret scanners don't flag the test itself).
SECRET_VARS = {
    "GROQ_API_KEY": "gsk_" + "server_secret_value_123",
    "TAVILY_API_KEY": "tvly-" + "server-secret-value-123",
    "AVIATION_STACK_API_KEY": "aviation-secret-123",
    "OPENWEATHER_API_KEY": "weather-secret-123",
    "DATABASE_URL": "postgresql://u:dbpass@db/x",
    "REDIS_URL": "redis://:redispass@cache/0",
}


@pytest.fixture
def secret_env(monkeypatch):
    for k, v in SECRET_VARS.items():
        monkeypatch.setenv(k, v)


def test_stdio_server_env_contains_only_its_own_key(secret_env):
    env = mcp_client._connection("openweather", "the-weather-key")["env"]
    assert env["OPENWEATHER_API_KEY"] == "the-weather-key"
    for name in ("GROQ_API_KEY", "TAVILY_API_KEY", "AVIATION_STACK_API_KEY", "DATABASE_URL", "REDIS_URL"):
        assert name not in env
    assert not set(SECRET_VARS.values()) & set(env.values())

    av = mcp_client._connection("aviationstack", "the-av-key")["env"]
    assert av["AVIATION_STACK_API_KEY"] == "the-av-key"
    assert "OPENWEATHER_API_KEY" not in av and "DATABASE_URL" not in av


def test_tavily_key_sent_in_header_not_url():
    conn = mcp_client._connection("tavily", "tvly-abc123")
    assert "tvly-abc123" not in conn["url"]
    assert conn["headers"] == {"Authorization": "Bearer tvly-abc123"}


def test_weather_server_does_not_load_dotenv():
    src = (pathlib.Path(mcp_client.__file__).parent / "weather_mcp_server.py").read_text(encoding="utf-8")
    assert "load_dotenv" not in src


def test_weather_server_starts_with_minimal_env(secret_env):
    """Spawns the real stdio server with the reduced env and lists its tools (no network call)."""
    client = MultiServerMCPClient({"openweather": mcp_client._connection("openweather", "fake-key")})
    tools = asyncio.run(asyncio.wait_for(client.get_tools(), timeout=60))
    assert {"get_current_weather", "get_forecast"} <= {t.name for t in tools}


# ---------------- CORS ----------------


@pytest.fixture
def client():
    with TestClient(server.api) as c:
        yield c


def _preflight(client, origin):
    return client.options(
        "/api/plan/stream",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_cors_allows_configured_origin_without_credentials(client):
    r = _preflight(client, "http://localhost:3000")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in r.headers
    assert "DELETE" not in r.headers["access-control-allow-methods"]


def test_cors_rejects_unknown_origin(client):
    r = _preflight(client, "https://evil.example.com")
    assert "access-control-allow-origin" not in r.headers
    r = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_exposes_request_id(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert "x-request-id" in r.headers["access-control-expose-headers"].lower()
