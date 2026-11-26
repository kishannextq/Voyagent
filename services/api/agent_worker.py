"""One agent as its own service: pulls tasks from the agent's SQS queue and returns results.

Same image as the API, one ECS service per agent:
    python agent_worker.py flight_agent        (or AGENT_NAME=flight_agent)

Runs up to AGENT_CONCURRENCY tasks at once (agents wait on models and tools, not CPU). Scales
out on queue depth (infra/terraform/modules/services, `agents`). Each agent service gets only
its own credentials: e.g. the weather agent has the OpenWeather key and nothing else.

Shutdown (SIGTERM from ECS scale-in or deploy): stops taking tasks, lets running ones finish
for up to AGENT_DRAIN_SECONDS, then hands unfinished ones back to the queue.

Health: touches AGENT_HEARTBEAT_FILE every 15 s and after every poll; the container health check
fails when it goes stale (hung event loop) (`python agent_worker.py --health`).
"""

import asyncio
import logging
import os
import pathlib
import signal
import sys
import time

HEARTBEAT = pathlib.Path(os.getenv("AGENT_HEARTBEAT_FILE", "/tmp/agent-heartbeat"))  # noqa: S108 - container-local
HEARTBEAT_MAX_AGE = 90


def healthy() -> bool:
    try:
        return time.time() - HEARTBEAT.stat().st_mtime < HEARTBEAT_MAX_AGE
    except OSError:
        return False


if __name__ == "__main__" and "--health" in sys.argv:  # container health check: no heavy imports
    sys.exit(0 if healthy() else 1)

import logging_setup  # noqa: E402

AGENT = os.getenv("AGENT_NAME", "") or (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "")
logging_setup.configure(f"agent-{AGENT.replace('_', '-')}" if AGENT else "agent")

import agent_bus  # noqa: E402
import agent_config  # noqa: E402
import agents  # noqa: E402
import config  # noqa: E402,F401 - builds DATABASE_URL from DB_* parts when needed
import mcp_client  # noqa: E402
import metrics  # noqa: E402
import redis_conn  # noqa: E402
import telemetry  # noqa: E402
from rag import store as rag_store  # noqa: E402
from rag import tool as rag_tool  # noqa: E402

log = logging.getLogger("voyagent.agent_worker")

FUNCTIONS = {
    "supervisor": agents.supervisor_agent,
    "flight_agent": agents.flight_agent,
    "hotel_agent": agents.hotel_agent,
    "weather_agent": agents.weather_agent,
    "budget_agent": agents.budget_agent,
    "itinerary_agent": agents.itinerary_agent,
}
class Worker:
    def __init__(self, agent: str, bus: agent_bus.Transport, concurrency: int = 8, drain_seconds: float = 100):
        if agent not in FUNCTIONS:
            raise ValueError(f"unknown agent {agent!r}; one of {sorted(FUNCTIONS)}")
        self.agent = agent
        self.fn = FUNCTIONS[agent]
        self.bus = bus
        self.slots = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency
        self.drain_seconds = drain_seconds
        self.running: dict[asyncio.Task, tuple[str, str]] = {}  # task -> (receipt, task_id)
        # Claim lifetime: longer than any run of this agent, so a redelivery never runs it twice.
        self.claim_ttl = int(agent_config.SPECS[agent].timeout_seconds + agent_bus.queue_wait_seconds() + 60)

    async def handle(self, receipt: str, task: agent_bus.Task | None) -> None:
        if task is None:
            metrics.agent_tasks.labels(self.agent, "invalid").inc()
            await self.bus.ack(self.agent, receipt)
            return
        if not await self.bus.claim(task.task_id, self.claim_ttl):
            # Redelivered after another worker took it: never run an agent twice.
            metrics.agent_tasks.labels(self.agent, "duplicate").inc()
            await self.bus.ack(self.agent, receipt)
            return
        metrics.agent_inflight.labels(self.agent).inc()
        try:
            result = await agent_bus.execute(self.agent, self.fn, task)
            await self.bus.put_result(task.task_id, result.model_dump_json())
            await self.bus.ack(self.agent, receipt)
            metrics.agent_tasks.labels(self.agent, "ok" if result.ok else result.error_kind).inc()
            log.info(
                "agent_task_done",
                extra={"agent": self.agent, "task_id": task.task_id, "ok": result.ok,
                       "error_kind": result.error_kind or None, "queue_wait_s": round(result.queue_wait, 3)},
            )
        finally:
            metrics.agent_inflight.labels(self.agent).dec()

    def _spawn(self, receipt: str, body: str) -> None:
        task = agent_bus.parse_task(body)

        async def guarded():
            try:
                await self.handle(receipt, task)
            finally:
                self.slots.release()

        t = asyncio.create_task(guarded())  # own context copy: run context never leaks between tasks
        self.running[t] = (receipt, task.task_id if task else "")
        t.add_done_callback(self.running.pop)

    async def run(self, stop: asyncio.Event, wait_seconds: int = 20) -> None:
        log.info("agent_worker_ready", extra={"agent": self.agent, "concurrency": self.concurrency})
        beat = asyncio.create_task(_beat(stop))
        try:
            await self._loop(stop, wait_seconds)
        finally:
            beat.cancel()
        await self.drain()

    async def _loop(self, stop: asyncio.Event, wait_seconds: int) -> None:
        while not stop.is_set():
            await self.slots.acquire()
            if stop.is_set():
                self.slots.release()
                break
            free = 1
            while free < 10 and not self.slots.locked():
                await self.slots.acquire()
                free += 1
            try:
                msgs = await self.bus.receive(self.agent, free, wait_seconds)
            except Exception as e:
                log.warning("agent_receive_failed", extra={"agent": self.agent, "error_type": type(e).__name__})
                msgs = []
                await asyncio.sleep(1)
            heartbeat()
            for _ in range(free - len(msgs)):
                self.slots.release()
            for receipt, body in msgs:
                self._spawn(receipt, body)

    async def drain(self) -> None:
        if not self.running:
            return
        log.info("agent_worker_draining", extra={"agent": self.agent, "inflight": len(self.running)})
        _, pending = await asyncio.wait(list(self.running), timeout=self.drain_seconds)
        for t in pending:
            receipt, task_id = self.running.get(t, ("", ""))
            t.cancel()
            # Hand it back: another worker picks it up (unless its deadline has passed).
            if task_id:
                await self.bus.unclaim(task_id)
            if receipt:
                await self.bus.release(self.agent, receipt)
            metrics.agent_tasks.labels(self.agent, "released").inc()


def heartbeat() -> None:
    try:
        HEARTBEAT.touch()
    except OSError:
        pass


async def _beat(stop: asyncio.Event) -> None:
    """Heartbeat while the event loop is responsive (also when every slot is busy)."""
    while not stop.is_set():
        heartbeat()
        await asyncio.sleep(15)


async def run(agent: str, stop: asyncio.Event) -> None:
    metrics.start_server()
    telemetry.setup(f"agent-{agent.replace('_', '-')}")
    redis = await redis_conn.connect()
    if redis is None:
        raise RuntimeError("REDIS_URL is required for an agent worker (results go back through Redis)")
    docs = None
    if os.getenv("DATABASE_URL"):  # only agents that search documents get database access
        docs = await rag_store.open_store(os.getenv("DATABASE_URL"))
        rag_tool.configure(docs)
    worker = Worker(
        agent,
        agent_bus.SqsRedisTransport(redis),
        concurrency=int(os.getenv("AGENT_CONCURRENCY", "8")),
        drain_seconds=float(os.getenv("AGENT_DRAIN_SECONDS", "100")),
    )
    try:
        await worker.run(stop)
    finally:
        await mcp_client.aclose_sessions()
        if docs is not None:
            await docs.aclose()
        await redis.aclose()


def main() -> None:
    if not AGENT:
        sys.exit("usage: python agent_worker.py <agent>  (or set AGENT_NAME)")
    stop = asyncio.Event()

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        await run(AGENT, stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
