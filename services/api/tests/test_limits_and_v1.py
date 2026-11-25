"""/api/v1 versioning, request body size limit, rate limits, and trusted proxy headers."""

import asyncio
import os
import pathlib

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import fakeredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

import rate_limit  # noqa: E402
import server  # noqa: E402

PLAN = {"query": "3 days in Rome"}


@pytest.fixture
def client():
    with TestClient(server.api) as c:
        yield c


# ---------------- versioning ----------------


def test_v1_routes_and_deprecated_aliases(client):
    v1 = client.get("/api/v1/health")
    assert v1.status_code == 200 and "deprecation" not in v1.headers
    legacy = client.get("/api/health")
    assert legacy.status_code == 200
    assert legacy.headers["deprecation"] == "true"
    assert legacy.headers["link"] == '</api/v1/health>; rel="successor-version"'
    assert legacy.json() == v1.json()


def test_openapi_documents_only_v1(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/plan/stream" in paths
    assert not [p for p in paths if p.startswith("/api/") and not p.startswith("/api/v1/")]


# ---------------- body size ----------------


def test_oversized_body_rejected_by_content_length(client, monkeypatch):
    r = client.post(
        "/api/v1/plan", content=b"x" * (server.MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413


def test_oversized_chunked_body_rejected(client):
    def chunks():
        for _ in range(server.MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    r = client.post("/api/v1/plan", content=chunks(), headers={"Content-Type": "application/json"})
    assert "content-length" not in r.request.headers
    assert r.status_code == 413


def test_413_carries_cors_headers(client):
    r = client.post(
        "/api/v1/plan",
        content=b"x" * (server.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json", "Origin": "http://localhost:3000"},
    )
    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_normal_body_accepted(client):
    assert client.post("/api/v1/plan", json=PLAN).status_code == 200


# ---------------- rate limits ----------------


def test_run_limit_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_MINUTE", "2")
    with TestClient(server.api) as client:
        assert client.post("/api/v1/plan", json=PLAN).status_code == 200
        assert client.post("/api/plan", json=PLAN).status_code == 200  # legacy alias shares the counter
        r = client.post("/api/v1/plan/stream", json=PLAN)
        assert r.status_code == 429
        assert 1 <= int(r.headers["retry-after"]) <= 60
        # Reads and health are not run-limited.
        assert client.get("/api/v1/agents").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_request_limit_applies_but_not_to_health(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "3")
    with TestClient(server.api) as client:
        assert [client.get("/api/v1/agents").status_code for _ in range(4)] == [200, 200, 200, 429]
        assert all(client.get("/api/v1/health").status_code == 200 for _ in range(5))


def test_rate_limits_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
    with TestClient(server.api) as client:
        assert {client.get("/api/v1/agents").status_code for _ in range(3)} == {200}


class FakeClock:
    def __init__(self, t=1_000_040.0):  # 20s into a 60s window
        self.t = t

    def __call__(self):
        return self.t


def test_memory_limiter_window_resets_and_is_bounded():
    clock = FakeClock()
    lim = rate_limit.MemoryLimiter(clock=clock, max_keys=10)

    async def go():
        assert await lim.hit("a", 2) is None
        assert await lim.hit("a", 2) is None
        assert await lim.hit("a", 2) == 40  # 20s into the window
        assert await lim.hit("b", 2) is None  # per key
        clock.t += 40
        assert await lim.hit("a", 2) is None  # new window
        for i in range(50):
            await lim.hit(f"k{i}", 100)
        assert len(lim._counts) <= 10

    asyncio.run(go())


def test_redis_limiter_counts_and_expires():
    async def go():
        r = fakeredis.FakeAsyncRedis()
        lim = rate_limit.RedisLimiter(r, clock=FakeClock())
        assert await lim.hit("run:1.2.3.4", 1) is None
        assert await lim.hit("run:1.2.3.4", 1) == 40
        keys = await r.keys("api:rl:*")
        assert len(keys) == 1 and 0 < await r.ttl(keys[0]) <= 61

    asyncio.run(go())


def test_redis_limiter_fails_open():
    class Broken:
        def pipeline(self, **_):
            raise ConnectionError("redis down")

    assert asyncio.run(rate_limit.RedisLimiter(Broken()).hit("k", 0)) is None


# ---------------- trusted proxy headers ----------------


async def _get_agents(app, client_ip: str, forwarded_for: str) -> int:
    transport = httpx.ASGITransport(app=app, client=(client_ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/agents", headers={"X-Forwarded-For": forwarded_for})
        return r.status_code


def test_spoofed_forwarded_for_does_not_evade_limits(monkeypatch):
    """uvicorn --proxy-headers with the image default (trust 127.0.0.1 only), on a public
    route where limits key on the client IP (authenticated routes key on the user)."""
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
    app = ProxyHeadersMiddleware(server.api, trusted_hosts="127.0.0.1")

    async def go():
        async with server.lifespan(server.api):
            # Untrusted client rotating X-Forwarded-For is still one client.
            assert await _get_agents(app, "203.0.113.9", "198.51.100.1") == 200
            assert await _get_agents(app, "203.0.113.9", "198.51.100.2") == 429
            # Through the trusted proxy, distinct real clients get their own limits.
            assert await _get_agents(app, "127.0.0.1", "198.51.100.3") == 200
            assert await _get_agents(app, "127.0.0.1", "198.51.100.4") == 200

    asyncio.run(go())


def test_image_does_not_trust_all_proxies():
    dockerfile = (pathlib.Path(server.__file__).parent / "Dockerfile").read_text(encoding="utf-8")
    assert "forwarded-allow-ips='*'" not in dockerfile
    assert "FORWARDED_ALLOW_IPS=127.0.0.1" in dockerfile


def test_access_log_route_includes_version_prefix():
    assert server._route_template("/api/v1/plan/abc12345", type("R", (), {"path": "/plan/{thread_id}"})) == (
        "/api/v1/plan/{thread_id}"
    )
    assert server._route_template("/api/plan/abc12345", type("R", (), {"path": "/plan/{thread_id}"})) == (
        "/api/plan/{thread_id}"
    )
    assert server._route_template("/nope", None) == "/nope"
