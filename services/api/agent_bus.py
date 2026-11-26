"""Agents as separate services: tasks over SQS, results over Redis.

The orchestrator still owns the plan (LangGraph graph, checkpoints, approval, streaming). For
every agent listed in REMOTE_AGENTS, the graph node no longer runs the agent in-process: it
puts a task on that agent's SQS queue and waits for the result. Each agent runs as its own
service (agent_worker.py), scales on its queue depth, and holds only the credentials it needs.

    orchestrator --SendMessage--> SQS <prefix><agent> --ReceiveMessage--> agent worker(s)
    orchestrator <--BLPOP-- Redis voyagent:agent:result:<task_id> <--RPUSH-- agent worker

Why Redis for replies: the reply must reach the one orchestrator task holding the user's
stream; a per-task Redis list does that without per-instance queues.

Delivery: at most once. A worker claims a task (Redis SET NX) before running it, so a
redelivered message never runs an agent twice (no double model spend). If a worker dies mid-task
the orchestrator times out and the user sees an error; the plan can be retried.

Message contract (validated on both sides; unknown fields rejected):
    Task   {v, task_id, agent, state, run{end_user, tenant, live, fallback, llm_calls}, deadline, trace}
    Result {v, task_id, ok, update, used_dummy, fallback, llm_calls, error, error_kind, queue_wait}
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Literal, Protocol

from langchain_core.messages import messages_from_dict, messages_to_dict
from opentelemetry import context as otel_context
from opentelemetry import propagate
from pydantic import BaseModel, ConfigDict, Field

import agent_config
import keys
import metrics
import telemetry

log = logging.getLogger("voyagent.agent_bus")

# Agents that may run as separate services. human_approval (an interrupt) and final_response
# stay in the orchestrator: they belong to the approval step, not to planning.
REMOTABLE = ("supervisor", "flight_agent", "hotel_agent", "weather_agent", "budget_agent", "itinerary_agent")

# SQS allows 256 KiB per message; stay below it with room for the envelope.
MAX_TASK_BYTES = 240_000
RESULT_TTL_SECONDS = 300
VERSION = 1


def remote_agents() -> set[str]:
    raw = os.getenv("REMOTE_AGENTS", "").replace(" ", "")
    names = {n for n in raw.split(",") if n}
    unknown = names - set(REMOTABLE)
    if unknown:
        raise RuntimeError(f"REMOTE_AGENTS has agents that can't run remotely: {sorted(unknown)}")
    return names


def queue_wait_seconds() -> float:
    """Extra time a task may wait in its queue (backlog, scale-out) on top of the agent's timeout."""
    return float(os.getenv("AGENT_QUEUE_WAIT_SECONDS", "30"))


def queue_name(agent: str) -> str:
    return f"{os.getenv('AGENT_QUEUE_PREFIX', 'voyagent-local-')}{agent.replace('_', '-')}"


# ---- messages ----


class RunContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    end_user: str = ""
    tenant: str = ""
    live: bool = False
    fallback: dict | None = None
    llm_calls: int = Field(0, ge=0)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = VERSION
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    agent: str
    state: dict[str, Any]
    run: RunContext
    deadline: float  # epoch seconds; a worker drops tasks past it
    trace: dict[str, str] = Field(default_factory=dict)  # W3C traceparent/tracestate
    enqueued_at: float = 0.0


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = VERSION
    task_id: str
    ok: bool
    update: dict[str, Any] = Field(default_factory=dict)
    used_dummy: bool = False
    fallback: dict | None = None
    llm_calls: int = 0
    error: str = ""
    error_kind: Literal["", "budget", "timeout", "tool", "expired", "error"] = ""
    queue_wait: float = 0.0


def encode_state(state: dict[str, Any]) -> dict[str, Any]:
    """Graph state for an agent: plain JSON. Message history stays in the orchestrator."""
    return {k: v for k, v in state.items() if k != "messages"}


def encode_update(update: dict[str, Any] | None) -> dict[str, Any]:
    update = dict(update or {})
    if "messages" in update:
        update["messages"] = messages_to_dict(update["messages"])
    return update


def decode_update(update: dict[str, Any]) -> dict[str, Any]:
    update = dict(update)
    if "messages" in update:
        update["messages"] = messages_from_dict(update["messages"])
    return update


# ---- transports ----


class Transport(Protocol):
    async def send(self, agent: str, body: str) -> None: ...
    async def receive(self, agent: str, max_messages: int, wait_seconds: int) -> list[tuple[str, str]]: ...
    async def ack(self, agent: str, receipt: str) -> None: ...
    async def release(self, agent: str, receipt: str) -> None: ...
    async def claim(self, task_id: str, ttl: int) -> bool: ...
    async def unclaim(self, task_id: str) -> None: ...
    async def put_result(self, task_id: str, body: str) -> None: ...
    async def wait_result(self, task_id: str, timeout: float) -> str | None: ...
    async def aclose(self) -> None: ...


class MemoryTransport:
    """In-process queues: tests and single-process development. Not for multiple processes."""

    def __init__(self):
        self.queues: dict[str, asyncio.Queue] = {}
        self.inflight: dict[str, tuple[str, str]] = {}
        self.results: dict[str, asyncio.Queue] = {}
        self.claims: set[str] = set()

    def _q(self, agent: str) -> asyncio.Queue:
        return self.queues.setdefault(agent, asyncio.Queue())

    def _r(self, task_id: str) -> asyncio.Queue:
        return self.results.setdefault(task_id, asyncio.Queue())

    async def send(self, agent: str, body: str) -> None:
        await self._q(agent).put(body)

    async def receive(self, agent: str, max_messages: int, wait_seconds: int) -> list[tuple[str, str]]:
        q = self._q(agent)
        try:
            body = await asyncio.wait_for(q.get(), wait_seconds)
        except TimeoutError:
            return []
        out = [body]
        while len(out) < max_messages and not q.empty():
            out.append(q.get_nowait())
        msgs = []
        for b in out:
            receipt = uuid.uuid4().hex
            self.inflight[receipt] = (agent, b)
            msgs.append((receipt, b))
        return msgs

    async def ack(self, agent: str, receipt: str) -> None:
        self.inflight.pop(receipt, None)

    async def release(self, agent: str, receipt: str) -> None:
        item = self.inflight.pop(receipt, None)
        if item:
            await self._q(item[0]).put(item[1])

    async def claim(self, task_id: str, ttl: int) -> bool:
        if task_id in self.claims:
            return False
        self.claims.add(task_id)
        return True

    async def unclaim(self, task_id: str) -> None:
        self.claims.discard(task_id)

    async def put_result(self, task_id: str, body: str) -> None:
        await self._r(task_id).put(body)

    async def wait_result(self, task_id: str, timeout: float) -> str | None:
        try:
            return await asyncio.wait_for(self._r(task_id).get(), timeout)
        except TimeoutError:
            return None
        finally:
            self.results.pop(task_id, None)

    async def aclose(self) -> None:
        return None


class SqsRedisTransport:
    """SQS queues for tasks (one per agent), Redis lists for results.

    Endpoint and credentials come from the standard AWS chain: the ECS task role in AWS;
    locally AWS_ENDPOINT_URL_SQS points at ElasticMQ.
    """

    RESULT_KEY = "voyagent:agent:result:"
    CLAIM_KEY = "voyagent:agent:claim:"

    def __init__(self, redis, sqs_client=None):
        import boto3

        self.redis = redis
        self.sqs = sqs_client or boto3.client("sqs")
        self._urls: dict[str, str] = {}

    async def _url(self, agent: str) -> str:
        if agent not in self._urls:
            resp = await asyncio.to_thread(self.sqs.get_queue_url, QueueName=queue_name(agent))
            self._urls[agent] = resp["QueueUrl"]
        return self._urls[agent]

    async def send(self, agent: str, body: str) -> None:
        url = await self._url(agent)
        await asyncio.to_thread(self.sqs.send_message, QueueUrl=url, MessageBody=body)

    async def receive(self, agent: str, max_messages: int, wait_seconds: int) -> list[tuple[str, str]]:
        url = await self._url(agent)
        resp = await asyncio.to_thread(
            self.sqs.receive_message,
            QueueUrl=url,
            MaxNumberOfMessages=max(1, min(10, max_messages)),
            WaitTimeSeconds=wait_seconds,
        )
        return [(m["ReceiptHandle"], m["Body"]) for m in resp.get("Messages", [])]

    async def ack(self, agent: str, receipt: str) -> None:
        url = await self._url(agent)
        await asyncio.to_thread(self.sqs.delete_message, QueueUrl=url, ReceiptHandle=receipt)

    async def release(self, agent: str, receipt: str) -> None:
        """Make an unfinished message visible again now (worker shutting down)."""
        url = await self._url(agent)
        await asyncio.to_thread(
            self.sqs.change_message_visibility, QueueUrl=url, ReceiptHandle=receipt, VisibilityTimeout=0
        )

    async def claim(self, task_id: str, ttl: int) -> bool:
        return bool(await self.redis.set(self.CLAIM_KEY + task_id, "1", nx=True, ex=ttl))

    async def unclaim(self, task_id: str) -> None:
        await self.redis.delete(self.CLAIM_KEY + task_id)

    async def put_result(self, task_id: str, body: str) -> None:
        key = self.RESULT_KEY + task_id
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, body)
            pipe.expire(key, RESULT_TTL_SECONDS)
            await pipe.execute()

    async def wait_result(self, task_id: str, timeout: float) -> str | None:
        key = self.RESULT_KEY + task_id
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            # Short BLPOPs: stays under the client's socket timeout and re-checks the deadline.
            while loop.time() < deadline:
                item = await self.redis.blpop([key], timeout=1)
                if item is not None:
                    value = item[1]
                    return value.decode() if isinstance(value, bytes) else value
            return None
        finally:
            await self.redis.delete(key)

    async def aclose(self) -> None:
        return None


_transport: Transport | None = None


def configure(transport: Transport | None) -> None:
    global _transport
    _transport = transport


def transport() -> Transport:
    if _transport is None:
        raise RuntimeError("remote agents are configured but no agent transport is open")
    return _transport


async def open_transport(redis) -> Transport | None:
    """Transport for this process, or None when no agent runs remotely."""
    if not remote_agents():
        return None
    if redis is None:
        raise RuntimeError("REMOTE_AGENTS needs REDIS_URL (agent results are returned through Redis)")
    return SqsRedisTransport(redis)


# ---- orchestrator side ----


async def call(agent: str, state: dict[str, Any]) -> dict[str, Any]:
    """Run one agent on its service and return its state update (graph node body)."""
    spec = agent_config.SPECS[agent]
    run = keys.run_state()
    timeout = spec.timeout_seconds + queue_wait_seconds()
    task_id = uuid.uuid4().hex
    with telemetry.tracer.start_as_current_span(f"agent.remote {agent}", attributes={"agent.name": agent}):
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        task = Task(
            task_id=task_id,
            agent=agent,
            state=encode_state(state),
            run=RunContext(
                end_user=keys.end_user(),
                tenant=keys.tenant(),
                live=keys.is_complete(keys.current()),
                fallback=run.fallback,
                llm_calls=getattr(run, "llm_calls", 0),
            ),
            deadline=time.time() + timeout,
            trace=carrier,
            enqueued_at=time.time(),
        )
        body = task.model_dump_json()
        if len(body.encode()) > MAX_TASK_BYTES:
            raise ValueError(f"{agent} task is too large for the queue ({len(body.encode())} bytes)")
        bus = transport()
        started = time.monotonic()
        await bus.send(agent, body)
        raw = await bus.wait_result(task_id, timeout)
        metrics.agent_remote_duration.labels(agent).observe(time.monotonic() - started)
    if raw is None:
        metrics.agent_remote_calls.labels(agent, "no_response").inc()
        log.warning("agent_no_response", extra={"agent": agent, "task_id": task_id, "timeout_s": timeout})
        raise TimeoutError(f"{agent} did not respond within {timeout:.0f}s (agent service down or overloaded)")
    result = Result.model_validate_json(raw)
    metrics.agent_queue_wait.labels(agent).observe(result.queue_wait)
    # Fold the agent's run effects back into this run.
    run.llm_calls = max(getattr(run, "llm_calls", 0), result.llm_calls)
    if result.used_dummy:
        run.used_dummy = True
    if result.fallback and run.fallback is None:
        run.fallback = result.fallback
    if not result.ok:
        metrics.agent_remote_calls.labels(agent, result.error_kind or "error").inc()
        if result.error_kind == "budget":
            raise agent_config.BudgetExceeded(result.error)
        if result.error_kind in ("timeout", "expired"):
            raise TimeoutError(result.error)
        raise RuntimeError(result.error or f"{agent} failed")
    metrics.agent_remote_calls.labels(agent, "ok").inc()
    return decode_update(result.update)


def node(name: str, fn):
    """Graph node for an agent: remote when listed in REMOTE_AGENTS, else in-process."""
    if name in remote_agents():

        async def remote(state):
            return await call(name, state)

        remote.__name__ = name
        return remote
    return agent_config.wrap(name, fn)


# ---- worker side ----


async def execute(agent: str, fn, task: Task) -> Result:
    """Run one task with the run context it carries. Never raises (errors become results)."""
    now = time.time()
    queue_wait = max(0.0, now - task.enqueued_at) if task.enqueued_at else 0.0
    base = {"task_id": task.task_id, "queue_wait": queue_wait}
    if task.agent != agent:
        return Result(ok=False, error=f"task for {task.agent} sent to {agent}", error_kind="error", **base)
    remaining = task.deadline - now
    if remaining <= 0:
        return Result(ok=False, error=f"{agent} task expired in the queue", error_kind="expired", **base)

    keys.set_current(keys.server_keys() if task.run.live else keys.ApiKeys())
    keys.set_end_user(task.run.end_user)
    keys.set_tenant(task.run.tenant)
    run = keys.start_run()
    run.fallback = task.run.fallback
    run.llm_calls = task.run.llm_calls
    token = otel_context.attach(propagate.extract(task.trace))
    try:
        update = await asyncio.wait_for(agent_config.wrap(agent, fn)(task.state), remaining)
        return Result(
            ok=True,
            update=encode_update(update),
            used_dummy=run.used_dummy,
            fallback=run.fallback,
            llm_calls=run.llm_calls,
            **base,
        )
    except agent_config.BudgetExceeded as e:
        kind, msg = "budget", str(e)
    except agent_config.ToolNotAllowed as e:
        kind, msg = "tool", str(e)
    except TimeoutError as e:
        kind, msg = "timeout", str(e) or f"{agent} ran past its deadline"
    except Exception as e:
        log.exception("agent_task_failed", extra={"agent": agent, "task_id": task.task_id})
        kind, msg = "error", keys.redact(f"{agent} failed: {type(e).__name__}: {e}")
    finally:
        otel_context.detach(token)
    return Result(
        ok=False, error=msg, error_kind=kind, used_dummy=run.used_dummy, fallback=run.fallback,
        llm_calls=run.llm_calls, **base,
    )


def parse_task(body: str) -> Task | None:
    try:
        return Task.model_validate(json.loads(body))
    except Exception as e:  # malformed or foreign message: drop it, never run it
        log.warning("agent_task_invalid", extra={"error_type": type(e).__name__})
        return None
