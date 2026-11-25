import os
import re

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import run_store  # noqa: E402
import server  # noqa: E402

META = {"modes": {"llm": "mock"}, "fallback": None, "dummy_nodes": {"supervisor": True}}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_memory_store_is_bounded_lru():
    store = run_store.MemoryRunStore(max_entries=3)
    for i in range(3):
        store.save(f"t{i}", META)
    store.get("t0")  # touch: t1 becomes least recently used
    store.save("t3", META)
    assert len(store) == 3
    assert store.get("t1") is None
    assert store.get("t0") == META


def test_memory_store_expires_entries():
    clock = FakeClock()
    store = run_store.MemoryRunStore(ttl=60, clock=clock)
    store.save("t", META)
    clock.t += 59
    assert store.get("t") == META
    clock.t += 2
    assert store.get("t") is None
    assert len(store) == 0


def test_memory_store_returns_copies():
    store = run_store.MemoryRunStore()
    store.save("t", META)
    got = store.get("t")
    got["dummy_nodes"]["x"] = False
    assert "x" not in store.get("t")["dummy_nodes"]


def test_redis_store_roundtrip_with_ttl():
    r = fakeredis.FakeRedis()
    store = run_store.RedisRunStore(r, ttl=120)
    store.save("abc", META)
    assert store.get("abc") == META
    assert 0 < r.ttl("api:run:abc") <= 120
    assert store.get("missing") is None


@pytest.mark.parametrize("env", ["staging", "production"])
def test_build_requires_redis_outside_local(monkeypatch, env):
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(RuntimeError, match="REDIS_URL is required"):
        run_store.build()


def test_build_rejects_unknown_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(RuntimeError, match="APP_ENV must be one of"):
        run_store.build()


def test_build_memory_locally(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert isinstance(run_store.build(), run_store.MemoryRunStore)


# ---- server integration ----


@pytest.fixture
def client():
    return TestClient(server.api)


def test_generated_thread_id_is_random_and_has_no_user_id(client):
    body = client.post("/api/plan", json={"query": "3 days in Rome", "user_id": "alice"}).json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["thread_id"])
    assert "alice" not in body["thread_id"]


def test_run_metadata_persisted_and_served(client):
    tid = client.post("/api/plan", json={"query": "3 days in Rome"}).json()["thread_id"]
    meta = server.runs.get(tid)
    assert meta["modes"] == {"llm": "mock", "flights": "mock", "hotels": "mock", "weather": "mock"}
    assert meta["dummy_nodes"]["supervisor"] is True
    assert client.get(f"/api/plan/{tid}").json()["dummy_nodes"] == meta["dummy_nodes"]


def test_cannot_start_new_plan_in_existing_thread(client):
    tid = client.post("/api/plan", json={"query": "3 days in Rome"}).json()["thread_id"]
    r = client.post("/api/plan", json={"query": "5 days in Paris", "thread_id": tid})
    assert r.status_code == 409


@pytest.mark.parametrize("bad", ["short", "has space here", "../../etc/passwd", "x" * 65])
def test_client_thread_id_format_validated(client, bad):
    assert client.post("/api/plan", json={"query": "3 days in Rome", "thread_id": bad}).status_code == 422
