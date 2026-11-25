"""Running plans: the LangGraph graph, provider keys, tools and run metadata.

Two implementations behind one interface:
- LocalOrchestrator: runs the graph in this process (local dev, tests, and the orchestrator
  service itself, orchestrator_app.py).
- RemoteOrchestrator: the public API calls the internal orchestrator service over HTTP
  (ORCHESTRATOR_URL) with a shared service token. Provider keys, MCP tool processes and the
  checkpointer then live only in the orchestrator.

Streams are Server-Sent Event text, the same frames the web app consumes.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx
from langchain_core.messages import HumanMessage
from langgraph.types import Command

import graph
import keys
import logging_setup
import mcp_client
import memory
import redis_conn
import run_store

log = logging.getLogger("voyagent.orchestrator")

# Fields of TravelState that are safe and useful to send to the client.
PUBLIC_FIELDS = [
    "user_query",
    "trip_constraints",
    "selected_agents",
    "supervisor_reasoning",
    "flight_results",
    "hotel_results",
    "weather_results",
    "budget_results",
    "itinerary",
    "approval_request",
    "human_feedback",
    "approved",
    "final_response",
    "llm_calls",
]


class Busy(Exception):
    """This orchestrator is already running its maximum number of plans."""


class OrchestratorError(Exception):
    pass


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": graph.RECURSION_LIMIT}


def public(values: dict[str, Any]) -> dict[str, Any]:
    return {k: values[k] for k in PUBLIC_FIELDS if k in values}


def _graph_input(kind: str, payload: dict) -> Any:
    if kind == "resume":
        return Command(resume={"approved": bool(payload["approved"]), "feedback": str(payload.get("feedback", ""))})
    if kind == "start":
        query = str(payload["query"])
        return {
            "messages": [HumanMessage(content=query)],
            "user_id": str(payload.get("user_id", "")),
            "user_query": query,
            "flight_results": "",
            "hotel_results": "",
            "weather_results": "",
            "budget_results": "",
            "itinerary": "",
            "final_response": "",
            "llm_calls": 0,
        }
    raise ValueError(f"unknown run kind: {kind}")


class Orchestrator(Protocol):
    async def exists(self, thread_id: str) -> bool: ...
    async def snapshot(self, thread_id: str) -> dict | None: ...
    async def start(self, thread_id: str, kind: str, payload: dict, end_user: str) -> AsyncIterator[str]: ...
    async def delete(self, thread_id: str) -> None: ...
    async def get_memory(self, end_user: str) -> dict[str, str]: ...
    async def delete_memory(self, end_user: str) -> None: ...
    async def modes(self) -> dict[str, str]: ...
    def busy(self) -> bool: ...
    async def aclose(self) -> None: ...


class LocalOrchestrator:
    def __init__(
        self,
        travel_graph,
        runs: run_store.RunStore,
        closers=(),
        max_runs: int = 32,
        timeout: float = 300,
        memory_store: memory.MemoryStore | None = None,
    ):
        self.graph = travel_graph
        self.runs = runs
        self.memory = memory_store or memory.InMemoryStore()
        self.timeout = timeout
        self.slots = asyncio.Semaphore(max_runs)
        self._closers = list(closers)

    @classmethod
    async def open(cls) -> "LocalOrchestrator":
        redis = await redis_conn.connect()
        closers = []
        if redis is not None:
            closers.append(redis.aclose)
        try:
            mem = await memory.open_store(os.getenv("DATABASE_URL"))
            closers.append(mem.aclose)
            travel_graph, close_graph = await graph.open_graph()
        except BaseException:
            for c in closers:
                await c()
            raise
        closers = [mcp_client.aclose_sessions, close_graph, *closers]
        return cls(
            travel_graph,
            run_store.build(redis),
            closers,
            memory_store=mem,
            max_runs=int(os.getenv("MAX_CONCURRENT_RUNS", "32")),
            timeout=float(os.getenv("RUN_TIMEOUT_SECONDS", "300")),
        )

    async def aclose(self) -> None:
        for close in self._closers:
            await close()

    def busy(self) -> bool:
        return self.slots.locked()

    async def modes(self) -> dict[str, str]:
        return keys.modes(keys.resolve())

    async def exists(self, thread_id: str) -> bool:
        return bool((await self.graph.aget_state(config(thread_id))).values)

    async def delete(self, thread_id: str) -> None:
        await self.graph.checkpointer.adelete_thread(thread_id)
        await self.runs.delete(thread_id)

    async def snapshot(self, thread_id: str) -> dict | None:
        snap = await self.graph.aget_state(config(thread_id))
        values = dict(snap.values or {})
        if not values:
            return None
        interrupt_value = None
        for task in snap.tasks or ():
            for intr in getattr(task, "interrupts", ()) or ():
                interrupt_value = intr.value
                break
        if interrupt_value is not None:
            status = "awaiting_approval"
        elif values.get("final_response") and not values.get("selected_agents") and not values.get("itinerary"):
            status = "blocked"
        else:
            status = "completed"
        meta = await self.runs.get(thread_id) or {}
        return {
            "thread_id": thread_id,
            "status": status,
            "state": public(values),
            "interrupt": interrupt_value,
            "modes": meta.get("modes"),
            "fallback": meta.get("fallback"),
            "dummy_nodes": meta.get("dummy_nodes"),
        }

    async def start(self, thread_id: str, kind: str, payload: dict, end_user: str) -> AsyncIterator[str]:
        if self.busy():
            raise Busy()
        graph_input = _graph_input(kind, payload)
        if kind == "start" and memory.enabled() and end_user:
            graph_input["user_memory"] = await self.memory.get(end_user)
        remember = kind == "resume" and memory.enabled() and bool(end_user)
        return self._stream(thread_id, graph_input, keys.resolve(), end_user, remember)

    async def get_memory(self, end_user: str) -> dict[str, str]:
        return await self.memory.get(end_user)

    async def delete_memory(self, end_user: str) -> None:
        await self.memory.delete(end_user)

    async def _stream(
        self, thread_id: str, graph_input: Any, api_keys: keys.ApiKeys, end_user: str, remember: bool = False
    ) -> AsyncIterator[str]:
        """Run the graph on the event loop and emit one SSE event per completed node.

        Provider keys and per-run state live in ContextVars set here, in the task that serves
        this stream; LangGraph copies the context into the tasks that run each node.
        """
        async with self.slots:
            modes = keys.modes(api_keys)
            meta = await self.runs.get(thread_id) or {}
            meta["modes"] = modes
            meta.setdefault("dummy_nodes", {})
            meta.setdefault("fallback", None)
            await self.runs.save(thread_id, meta)
            yield sse("start", {"thread_id": thread_id, "modes": modes})

            keys.set_current(api_keys)
            keys.set_end_user(end_user)
            run = keys.start_run()
            dummy = meta["dummy_nodes"]
            announced = False
            if meta["fallback"]:
                # This plan already fell back to dummy data (e.g. during planning): keep the rest of it
                # (approval -> final plan) on dummy data too, so one plan never mixes live and dummy results.
                run.fallback = meta["fallback"]
                announced = True
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            chunks = self.graph.astream(graph_input, config=config(thread_id), stream_mode="updates")
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        # Deadline per step, not around the yields (the client may be slow to read).
                        chunk = await asyncio.wait_for(anext(chunks), remaining)
                    except StopAsyncIteration:
                        break
                    for node, update in chunk.items():
                        if node == "__interrupt__":
                            payload = update[0].value if update else {}
                            yield sse("interrupt", {"node": "human_approval", "data": payload})
                        else:
                            # Did this agent use dummy data? (read & reset per agent; agents run one at a time)
                            dummy[node] = run.used_dummy
                            run.used_dummy = False
                            await self.runs.save(thread_id, meta)
                            yield sse("node", {"node": node, "data": public(update or {}), "dummy": dummy[node]})
                    if run.fallback and not announced:
                        # A live call failed: the run carries on with dummy data. Tell the UI once.
                        announced = True
                        meta["fallback"] = run.fallback
                        await self.runs.save(thread_id, meta)
                        yield sse("fallback", run.fallback)
                final = await self.snapshot(thread_id)
                if remember and final and final["state"].get("approved"):
                    await self.memory.put(end_user, memory.extract(final["state"]))
                yield sse("done", final)
            except TimeoutError:
                log.warning("graph_run_timeout", extra={"thread_id": thread_id, "timeout_s": self.timeout})
                yield sse("error", {"message": f"The plan took longer than {self.timeout:.0f}s and was stopped."})
            except Exception as exc:  # surface errors to the UI instead of dropping the stream
                log.exception("graph_run_failed", extra={"thread_id": thread_id})
                yield sse("error", {"message": keys.redact(str(exc), api_keys)})
            finally:
                await chunks.aclose()


class RemoteOrchestrator:
    """Client for the internal orchestrator service (orchestrator_app.py)."""

    def __init__(self, base_url: str, token: str, run_timeout: float = 300, transport=None):
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # Streams can run for the whole plan; connect quickly or fail.
            timeout=httpx.Timeout(10.0, read=run_timeout + 30),
            transport=transport,
        )

    def _headers(self) -> dict[str, str]:
        h = {"X-Internal-Token": self._token}
        rid = logging_setup.request_id_var.get()
        if rid:
            h["X-Request-ID"] = rid  # one request ID across services
        return h

    async def aclose(self) -> None:
        await self._client.aclose()

    def busy(self) -> bool:
        return False  # the orchestrator answers 503 itself (see start)

    async def modes(self) -> dict[str, str]:
        r = await self._client.get("/internal/v1/modes", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def exists(self, thread_id: str) -> bool:
        return await self.snapshot(thread_id) is not None

    async def snapshot(self, thread_id: str) -> dict | None:
        r = await self._client.get(f"/internal/v1/threads/{thread_id}", headers=self._headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def delete(self, thread_id: str) -> None:
        r = await self._client.delete(f"/internal/v1/threads/{thread_id}", headers=self._headers())
        r.raise_for_status()

    async def get_memory(self, end_user: str) -> dict[str, str]:
        r = await self._client.get("/internal/v1/memory", params={"owner": end_user}, headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def delete_memory(self, end_user: str) -> None:
        r = await self._client.delete("/internal/v1/memory", params={"owner": end_user}, headers=self._headers())
        r.raise_for_status()

    async def start(self, thread_id: str, kind: str, payload: dict, end_user: str) -> AsyncIterator[str]:
        req = self._client.build_request(
            "POST",
            f"/internal/v1/threads/{thread_id}/runs",
            json={"kind": kind, "payload": payload, "end_user": end_user},
            headers=self._headers(),
        )
        resp = await self._client.send(req, stream=True)
        if resp.status_code == 503:
            await resp.aclose()
            raise Busy()
        if resp.status_code != 200:
            await resp.aclose()
            raise OrchestratorError(f"orchestrator returned {resp.status_code}")
        return self._relay(resp)

    @staticmethod
    async def _relay(resp: httpx.Response) -> AsyncIterator[str]:
        try:
            async for text in resp.aiter_text():
                yield text
        finally:
            await resp.aclose()


async def open_from_env() -> Orchestrator:
    url = os.getenv("ORCHESTRATOR_URL", "").strip()
    if url:
        token = os.getenv("ORCHESTRATOR_TOKEN", "").strip()
        if not token:
            raise RuntimeError("ORCHESTRATOR_TOKEN is required when ORCHESTRATOR_URL is set")
        log.info("orchestrator_ready", extra={"mode": "remote"})
        return RemoteOrchestrator(url, token, run_timeout=float(os.getenv("RUN_TIMEOUT_SECONDS", "300")))
    log.info("orchestrator_ready", extra={"mode": "in-process"})
    return await LocalOrchestrator.open()
