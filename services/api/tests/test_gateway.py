"""Model gateway (LiteLLM) integration.

Unit part always runs. Integration needs a running gateway on config.test.yaml in front of
tools/mock-llm started with MOCK_LLM_FAIL_MODELS=mock-primary:
  GATEWAY_TEST_URL, GATEWAY_MASTER_KEY, MOCK_LLM_URL       -> fallback, aliases, API client path
  + GATEWAY_TEST_DB=1 (gateway has a database)             -> virtual keys, end-user spend, budgets
CI runs all of it (gateway job).
"""

import asyncio
import os
import time

import httpx
import pytest
from langchain_core.messages import HumanMessage

import config

GW = os.getenv("GATEWAY_TEST_URL", "").rstrip("/")
MASTER = os.getenv("GATEWAY_MASTER_KEY", "")
MOCK = os.getenv("MOCK_LLM_URL", "").rstrip("/")
needs_gateway = pytest.mark.skipif(not (GW and MASTER and MOCK), reason="gateway not running")
needs_db = pytest.mark.skipif(not (GW and os.getenv("GATEWAY_TEST_DB")), reason="gateway without database")


# One event loop for the module: langchain-openai caches its async HTTP client per process,
# so it must not outlive the loop it was created on (the app itself runs a single loop).
_loop = asyncio.Runner()


def run(coro):
    return _loop.run(coro)


@pytest.fixture
def gateway_client(monkeypatch):
    """config.get_llm pointed at the gateway (as when LLM_GATEWAY_URL is set)."""
    monkeypatch.setattr(config, "LLM_GATEWAY_URL", GW or "http://gateway.invalid")
    config.get_llm.cache_clear()
    yield config.get_llm
    config.get_llm.cache_clear()


def test_gateway_client_uses_aliases_and_no_client_retries(gateway_client):
    from langchain_openai import ChatOpenAI

    smart, fast = gateway_client("k", "smart"), gateway_client("k", "fast")
    assert isinstance(smart, ChatOpenAI)
    assert smart.model_name == "voyagent-smart" and fast.model_name == "voyagent-fast"
    assert smart.max_retries == 0  # the gateway retries; client retries would multiply attempts
    assert str(smart.openai_api_base).endswith("/v1")


def test_without_gateway_calls_groq_directly(monkeypatch):
    from langchain_groq import ChatGroq

    monkeypatch.setattr(config, "LLM_GATEWAY_URL", "")
    config.get_llm.cache_clear()
    try:
        assert isinstance(config.get_llm("gsk_x", "smart"), ChatGroq)
    finally:
        config.get_llm.cache_clear()


def test_llm_key_is_gateway_key_when_gateway_configured(monkeypatch):
    import keys

    monkeypatch.delenv("MOCK_MODE", raising=False)
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gw:4000")
    monkeypatch.setenv("LLM_GATEWAY_KEY", "sk-virtual")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_be_used")
    assert keys.server_keys().groq == "sk-virtual"
    monkeypatch.delenv("LLM_GATEWAY_URL")
    assert keys.server_keys().groq == "gsk_should_not_be_used"


# ---------------- integration ----------------


def _admin(path: str, body: dict) -> dict:
    r = httpx.post(f"{GW}{path}", json=body, headers={"Authorization": f"Bearer {MASTER}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _seen() -> list[dict]:
    return httpx.get(f"{MOCK}/_requests", timeout=10).json()


@needs_gateway
def test_fallback_to_backup_through_api_client(gateway_client):
    httpx.delete(f"{MOCK}/_requests", timeout=10)
    reply = run(gateway_client(MASTER, "smart").ainvoke([HumanMessage(content="plan Kyoto")]))
    assert reply.content.startswith("[mock-backup]")  # primary fails in the test setup
    models = [r["model"] for r in _seen()]
    assert models[-1] == "mock-backup" and "mock-primary" in models
    assert models.count("mock-primary") <= 2  # bounded: 1 try + num_retries=1


@needs_gateway
def test_fast_alias_routes_to_fast_model(gateway_client):
    reply = run(gateway_client(MASTER, "fast").ainvoke([HumanMessage(content="route this")]))
    assert reply.content.startswith("[mock-fast]")


@needs_gateway
def test_gateway_rejects_unknown_keys():
    r = httpx.post(
        f"{GW}/v1/chat/completions",
        json={"model": "voyagent-fast", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer sk-not-a-real-key"},
        timeout=30,
    )
    assert r.status_code in (400, 401) and "choices" not in r.text


@needs_db
def test_virtual_key_limited_to_its_models(gateway_client):
    key = _admin("/key/generate", {"models": ["voyagent-fast"], "max_budget": 1.0, "rpm_limit": 100})["key"]
    ok = run(gateway_client(key, "fast").ainvoke([HumanMessage(content="hi")]))
    assert ok.content.startswith("[mock-fast]")
    with pytest.raises(Exception, match="(?i)not allowed|access|401|403|key"):
        run(gateway_client(key, "smart").ainvoke([HumanMessage(content="hi")]))


@needs_db
def test_end_user_spend_tracked_and_budget_enforced(gateway_client):
    # Each mock call costs 15 tokens * $0.001 = $0.015; budget allows ~1 call.
    _admin("/budget/new", {"budget_id": "default-end-user-budget", "max_budget": 0.02})
    user = f"acme:user-{int(time.time())}"
    llm = gateway_client(MASTER, "fast").bind(user=user)
    run(llm.ainvoke([HumanMessage(content="one")]))
    run(llm.ainvoke([HumanMessage(content="two")]))

    spend = 0.0
    for _ in range(30):  # spend is flushed to the database asynchronously
        r = httpx.get(
            f"{GW}/customer/info", params={"end_user_id": user}, headers={"Authorization": f"Bearer {MASTER}"}
        )
        if r.status_code == 200:
            spend = float(r.json().get("spend") or 0)
            if spend > 0.02:
                break
        time.sleep(1)
    assert spend > 0.02, f"end-user spend not recorded: {spend}"

    with pytest.raises(Exception, match="(?i)budget"):
        run(llm.ainvoke([HumanMessage(content="three")]))


def test_agent_calls_carry_role_and_end_user(monkeypatch):
    import agents
    import keys

    calls = []

    class FakeLLM:
        def __init__(self, role, user=None):
            self.role, self.user = role, user

        def bind(self, **kw):
            return FakeLLM(self.role, kw.get("user"))

        async def ainvoke(self, messages):
            calls.append((self.role, self.user))
            return type("R", (), {"content": "ok"})()

    monkeypatch.setattr(agents, "get_llm", lambda key, role="smart": FakeLLM(role))

    async def go():
        keys.set_current(keys.ApiKeys(groq="k", tavily="t", aviationstack="a", openweather="w"))
        keys.start_run()
        keys.set_end_user("acme:alice")
        assert await agents._llm_text("sys", "prompt", role="fast") == "ok"
        assert await agents._llm_text("sys", "prompt") == "ok"

    asyncio.run(go())
    assert calls == [("fast", "acme:alice"), ("smart", "acme:alice")]
