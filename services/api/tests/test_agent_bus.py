"""Agents as separate services (agent_bus.py, agent_worker.py).

In-process queues stand in for SQS/Redis here; tests at the bottom run against real ElasticMQ +
Redis when SQS_TEST_ENDPOINT and REDIS_TEST_URL are set (CI `agents` job).
"""

import asyncio
import json
import os
import time
import uuid

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import httpx  # noqa: E402
import pytest  # noqa: E402

import agent_bus  # noqa: E402
import agent_config  # noqa: E402
import agent_worker  # noqa: E402
import keys  # noqa: E402
import server  # noqa: E402

ALL = ",".join(agent_bus.REMOTABLE)


def _events(text: str) -> list[str]:
    return [b.split("\n", 1)[0][7:] for b in text.split("\n\n") if b.startswith("event: ")]


class Workers:
    """One worker per agent on a shared in-memory bus, like one service per agent."""

    def __init__(self, bus, agents_=agent_bus.REMOTABLE, concurrency=4):
        self.bus = bus
        self.stop = asyncio.Event()
        self.workers = [agent_worker.Worker(a, bus, concurrency=concurrency, drain_seconds=2) for a in agents_]
        self.tasks = []

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(w.run(self.stop, wait_seconds=1)) for w in self.workers]
        return self

    async def __aexit__(self, *exc):
        self.stop.set()
        await asyncio.wait_for(asyncio.gather(*self.tasks), 10)


def _task(agent="weather_agent", state=None, deadline_in=30.0, **run) -> agent_bus.Task:
    return agent_bus.Task(
        task_id=uuid.uuid4().hex,
        agent=agent,
        state=state or {"user_query": "trip to Kyoto", "trip_constraints": {"destination": "Kyoto"}},
        run=agent_bus.RunContext(**run),
        deadline=time.time() + deadline_in,
        enqueued_at=time.time(),
    )


def test_remote_agents_setting(monkeypatch):
    monkeypatch.setenv("REMOTE_AGENTS", "flight_agent, weather_agent")
    assert agent_bus.remote_agents() == {"flight_agent", "weather_agent"}
    monkeypatch.setenv("REMOTE_AGENTS", "flight_agent,human_approval")
    with pytest.raises(RuntimeError, match="human_approval"):
        agent_bus.remote_agents()
    monkeypatch.setenv("AGENT_QUEUE_PREFIX", "voyagent-staging-")
    assert agent_bus.queue_name("flight_agent") == "voyagent-staging-flight-agent"


def test_remote_agents_need_redis(monkeypatch):
    monkeypatch.setenv("REMOTE_AGENTS", "weather_agent")
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        asyncio.run(agent_bus.open_transport(None))
    monkeypatch.setenv("REMOTE_AGENTS", "")
    assert asyncio.run(agent_bus.open_transport(None)) is None


def test_full_plan_with_every_agent_as_a_service(monkeypatch):
    """Plan -> review -> approve with all six agents on their own workers; same events as in-process."""
    monkeypatch.setenv("REMOTE_AGENTS", ALL)
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
    bus = agent_bus.MemoryTransport()

    async def open_transport(_redis):
        return bus

    monkeypatch.setattr(agent_bus, "open_transport", open_transport)
    handled: list[str] = []
    real_execute = agent_bus.execute

    async def counting_execute(agent, fn, task):
        handled.append(agent)
        return await real_execute(agent, fn, task)

    monkeypatch.setattr(agent_bus, "execute", counting_execute)

    async def scenario():
        async with Workers(bus), server.lifespan(server.api):
            transport = httpx.ASGITransport(app=server.api)
            async with httpx.AsyncClient(transport=transport, base_url="http://api", timeout=60) as c:
                r = await c.post("/api/v1/plan/stream", json={"query": "4 day trip to Kyoto in April"})
                assert r.status_code == 200
                ev = _events(r.text)
                assert ev[0] == "start" and "interrupt" in ev and ev[-1] == "done", ev
                nodes = [
                    json.loads(b.split("data: ", 1)[1])["node"]
                    for b in r.text.split("\n\n")
                    if b.startswith("event: node")
                ]
                assert nodes[0] == "supervisor" and "itinerary_agent" in nodes
                tid = json.loads(r.text.split("data: ", 1)[1].split("\n", 1)[0])["thread_id"]
                snap = (await c.get(f"/api/v1/plan/{tid}")).json()
                assert snap["status"] == "awaiting_approval"
                assert snap["state"]["trip_constraints"]["destination"] == "Kyoto"
                assert snap["state"]["itinerary"]
                assert all(snap["dummy_nodes"][n] for n in nodes)  # mock mode: labelled per agent
                r = await c.post(f"/api/v1/plan/{tid}/approve", json={"approved": True})
                assert r.status_code == 200 and r.json()["status"] == "completed"
                assert r.json()["state"]["final_response"]

    asyncio.run(scenario())
    assert set(handled) >= {"supervisor", "itinerary_agent"}
    assert set(handled) <= set(agent_bus.REMOTABLE)  # final_response stayed in the orchestrator


def test_no_agent_service_times_out_with_clear_error(monkeypatch):
    bus = agent_bus.MemoryTransport()
    agent_bus.configure(bus)
    spec = agent_config.SPECS["weather_agent"].model_copy(update={"timeout_seconds": 0.2})
    monkeypatch.setitem(agent_config.SPECS, "weather_agent", spec)
    monkeypatch.setenv("AGENT_QUEUE_WAIT_SECONDS", "0.1")

    async def scenario():
        keys.start_run()
        with pytest.raises(TimeoutError, match="did not respond"):
            await agent_bus.call("weather_agent", {"user_query": "x", "trip_constraints": {"destination": "Oslo"}})

    try:
        asyncio.run(scenario())
    finally:
        agent_bus.configure(None)


def test_redelivered_task_runs_once():
    bus = agent_bus.MemoryTransport()
    runs = []

    async def scenario():
        w = agent_worker.Worker("weather_agent", bus)
        real = w.fn

        async def fn(state):
            runs.append(1)
            return await real(state)

        w.fn = fn
        task = _task()
        body = task.model_dump_json()
        await bus.send("weather_agent", body)
        await bus.send("weather_agent", body)  # SQS at-least-once: same message twice
        for receipt, b in await bus.receive("weather_agent", 10, 1):
            await w.handle(receipt, agent_bus.parse_task(b))
        result = agent_bus.Result.model_validate_json(await bus.wait_result(task.task_id, 1))
        assert result.ok and "weather_results" in result.update
        assert await bus.wait_result(task.task_id, 0.1) is None  # only one result
        assert not bus.inflight  # both messages acknowledged

    asyncio.run(scenario())
    assert len(runs) == 1


def test_expired_and_misrouted_tasks_are_not_run():
    async def scenario():
        expired = await agent_bus.execute(
            "weather_agent", agent_worker.FUNCTIONS["weather_agent"], _task(deadline_in=-1)
        )
        assert not expired.ok and expired.error_kind == "expired"
        wrong = await agent_bus.execute(
            "weather_agent", agent_worker.FUNCTIONS["weather_agent"], _task(agent="flight_agent")
        )
        assert not wrong.ok and "sent to weather_agent" in wrong.error

    asyncio.run(scenario())


def test_invalid_message_is_dropped():
    bus = agent_bus.MemoryTransport()

    async def scenario():
        w = agent_worker.Worker("weather_agent", bus)
        bad = json.dumps({"task_id": "x", "agent": "weather_agent", "evil": True})
        await bus.send("weather_agent", bad)
        [(receipt, body)] = await bus.receive("weather_agent", 1, 1)
        assert agent_bus.parse_task(body) is None
        await w.handle(receipt, None)
        assert not bus.inflight

    asyncio.run(scenario())


def test_run_context_crosses_the_queue(monkeypatch):
    """Caller identity, tenant, budget and fallback travel with the task and come back."""
    seen = {}

    async def fake_agent(state):
        seen.update(user=keys.end_user(), tenant=keys.tenant(), calls=keys.run_state().llm_calls)
        agent_config.count_llm_call(keys.run_state())
        keys.trigger_fallback("weather", "provider said no")
        return {"weather_results": "ok"}

    async def scenario():
        result = await agent_bus.execute("weather_agent", fake_agent, _task(end_user="u-1", tenant="t-1", llm_calls=3))
        assert result.ok and result.used_dummy
        assert result.llm_calls == 4 and result.fallback["service"] == "weather"

    monkeypatch.setitem(
        agent_config.SPECS, "weather_agent", agent_config.SPECS["weather_agent"].model_copy(update={"max_llm_calls": 1})
    )
    asyncio.run(scenario())
    assert seen == {"user": "u-1", "tenant": "t-1", "calls": 3}


def test_run_budget_is_enforced_across_services(monkeypatch):
    """A remote agent can't escape the per-plan model-call budget."""
    bus = agent_bus.MemoryTransport()
    agent_bus.configure(bus)

    async def scenario():
        run = keys.start_run()
        run.llm_calls = agent_config.RUN_MAX_LLM_CALLS
        async with Workers(bus, agents_=("budget_agent",)):
            with pytest.raises(agent_config.BudgetExceeded):
                await agent_bus.call("budget_agent", {"user_query": "x", "trip_constraints": {}})

    try:
        asyncio.run(scenario())
    finally:
        agent_bus.configure(None)


def test_shutdown_hands_unfinished_tasks_back():
    bus = agent_bus.MemoryTransport()

    async def scenario():
        w = agent_worker.Worker("weather_agent", bus, drain_seconds=0.2)
        started = asyncio.Event()

        async def slow(state):
            started.set()
            await asyncio.sleep(30)

        w.fn = slow
        task = _task()
        await bus.send("weather_agent", task.model_dump_json())
        stop = asyncio.Event()
        runner = asyncio.create_task(w.run(stop, wait_seconds=1))
        await asyncio.wait_for(started.wait(), 5)
        stop.set()
        await asyncio.wait_for(runner, 10)
        # Back on the queue and unclaimed: another worker can take it.
        assert bus.queues["weather_agent"].qsize() == 1
        assert task.task_id not in bus.claims

    asyncio.run(scenario())


def test_task_size_limit(monkeypatch):
    bus = agent_bus.MemoryTransport()
    agent_bus.configure(bus)

    async def scenario():
        keys.start_run()
        with pytest.raises(ValueError, match="too large"):
            await agent_bus.call("budget_agent", {"user_query": "x" * (agent_bus.MAX_TASK_BYTES + 1)})

    try:
        asyncio.run(scenario())
    finally:
        agent_bus.configure(None)


# ---- real ElasticMQ (SQS API) + Redis ----

SQS = os.getenv("SQS_TEST_ENDPOINT", "")
REDIS = os.getenv("REDIS_TEST_URL", "")
needs_sqs = pytest.mark.skipif(not (SQS and REDIS), reason="set SQS_TEST_ENDPOINT and REDIS_TEST_URL")


@needs_sqs
def test_sqs_round_trip(monkeypatch):
    import boto3
    import redis.asyncio as redis

    monkeypatch.setenv("AGENT_QUEUE_PREFIX", "voyagent-test-")
    sqs = boto3.client(
        "sqs", endpoint_url=SQS, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )  # noqa: S106 - ElasticMQ ignores credentials
    sqs.create_queue(QueueName=agent_bus.queue_name("weather_agent"))

    async def scenario():
        r = redis.Redis.from_url(REDIS, socket_timeout=5)
        bus = agent_bus.SqsRedisTransport(r, sqs)
        agent_bus.configure(bus)
        try:
            async with Workers(bus, agents_=("weather_agent",)):
                keys.start_run()
                update = await agent_bus.call(
                    "weather_agent", {"user_query": "x", "trip_constraints": {"destination": "Oslo"}}
                )
                assert "Oslo" in update["weather_results"] or update["weather_results"]
                assert update["messages"][0].content == "Weather agent completed."
        finally:
            agent_bus.configure(None)
            await r.aclose()

    asyncio.run(scenario())
