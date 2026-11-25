"""FastAPI server exposing the multi-agent travel planner to the Next.js frontend.

Run:
    uvicorn server:api --reload --port 8000
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

import logging_setup

logging_setup.configure("api")

import graph  # noqa: E402
import keys  # noqa: E402
import mcp_client  # noqa: E402
import mock  # noqa: E402
import rate_limit  # noqa: E402
import redis_conn  # noqa: E402
import run_store  # noqa: E402

log = logging.getLogger("voyagent.api")

MOCK_MODE = mock.enabled()

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

# Execution bounds (no unbounded agent execution): per-process concurrent runs, and a
# wall-clock deadline per run (graph.RECURSION_LIMIT bounds the number of steps).
MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "32"))
RUN_TIMEOUT_SECONDS = float(os.getenv("RUN_TIMEOUT_SECONDS", "300"))

# Created in the lifespan, inside the server's event loop (async pools are loop-bound).
travel_graph: Any = None
runs: run_store.RunStore = None  # type: ignore[assignment]
limiter: rate_limit.Limiter = None  # type: ignore[assignment]
limits = rate_limit.Limits.from_env()
_run_slots: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Open stores at startup (fail fast on misconfiguration) and close them on shutdown."""
    global travel_graph, runs, limiter, limits, _run_slots
    _run_slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    limits = rate_limit.Limits.from_env()
    redis = await redis_conn.connect()
    try:
        runs = run_store.build(redis)
        limiter = rate_limit.build(redis)
        travel_graph, close_graph = await graph.open_graph()
    except BaseException:
        if redis is not None:
            await redis.aclose()
        raise
    try:
        yield
    finally:
        await mcp_client.aclose_sessions()
        await close_graph()
        if redis is not None:
            await redis.aclose()


api = FastAPI(
    title="Multi-Agent Travel Planner API",
    version="1.0.0",
    description="LangGraph supervisor + specialist agents with human-in-the-loop approval.",
    lifespan=lifespan,
)

MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(64 * 1024)))


class BodySizeLimit:
    """Reject request bodies over MAX_BODY_BYTES with 413, before they are read into memory.

    Checks Content-Length up front and also counts streamed (chunked) bodies. Pure ASGI so it
    runs before routing and body parsing.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    return await self._reject(scope, receive, send)
        seen = 0
        started = False  # has a response started going out?
        rejected = False

        async def guarded_send(message):
            nonlocal started
            if rejected:
                return  # we already answered 413; drop whatever the app sends
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def limited_receive():
            nonlocal seen, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes and not started:
                    # Answer now and tell the app the client went away. (Raising here would be
                    # caught by FastAPI's body parsing and turned into a 400.)
                    await self._reject(scope, receive, send)
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _reject(scope, receive, send):
        await JSONResponse(status_code=413, content={"detail": "Request body too large"})(scope, receive, send)


# Registered before CORS so CORS wraps it (413s still carry CORS headers); runs before body parsing.
api.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)

# CORS (SECURITY.md F8): explicit origins from env (defaults are local dev only), explicit
# methods/headers, and no credentialed requests: auth uses bearer tokens, not cookies.
origins = [
    o.strip()
    for o in os.getenv("FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o.strip() and o.strip() != "*"
]
api.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    # e.g. FRONTEND_ORIGIN_REGEX=https://voyagent-[a-z0-9]+-team\.vercel\.app for preview deployments
    allow_origin_regex=os.getenv("FRONTEND_ORIGIN_REGEX") or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Retry-After"],
    max_age=600,
)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


@api.middleware("http")
async def _request_context(request: Request, call_next):
    """Request ID (trusted from the proxy if well-formed, else generated) + one access log line.

    Logs metadata only: method, route template, status, duration. Never bodies or query strings.
    """
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
    token = logging_setup.request_id_var.set(request_id)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        path = request.url.path
        if path.startswith("/api/") and not path.startswith("/api/v1/"):
            # Unversioned routes are kept for older web builds; same handlers as /api/v1.
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = f'</api/v1{path[4:]}>; rel="successor-version"'
        return response
    finally:
        route = request.scope.get("route")
        log.info(
            "http_request",
            extra={
                "method": request.method,
                "route": getattr(route, "path", request.url.path),
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        logging_setup.request_id_var.reset(token)


@api.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422 without echoing submitted values back (they may contain secrets or PII)."""
    errors = [{k: v for k, v in e.items() if k in ("type", "loc", "msg")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


# ---------- Schemas ----------


class PlanRequest(BaseModel):
    # Unknown fields are rejected, so clients can't send provider keys (removed feature).
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=3, max_length=2000, description="Natural-language travel request")
    user_id: str = Field("demo_user", description="User identifier")
    thread_id: str | None = Field(
        None,
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
        description="Optional client-chosen ID for a NEW thread (409 if it already exists); generated if omitted",
    )


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    feedback: str = Field("", max_length=2000)

    def resume_value(self) -> dict[str, Any]:
        return {"approved": self.approved, "feedback": self.feedback}


class PlanResponse(BaseModel):
    thread_id: str
    status: str  # "awaiting_approval" | "completed" | "blocked"
    state: dict[str, Any]
    interrupt: dict[str, Any] | None = None
    # Per service: "live" (real API) or "mock" (dummy data). Keys: llm, flights, hotels, weather.
    modes: dict[str, str] | None = None
    # Set when a live call failed and the run continued with dummy data: {"service", "message"}.
    fallback: dict[str, str] | None = None
    # Per agent: True if that agent's output is (at least partly) dummy data.
    dummy_nodes: dict[str, bool] | None = None


# ---------- Helpers ----------


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": graph.RECURSION_LIMIT}


def _public(values: dict[str, Any]) -> dict[str, Any]:
    return {k: values[k] for k in PUBLIC_FIELDS if k in values}


def _initial_input(req: PlanRequest) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=req.query)],
        "user_id": req.user_id,
        "user_query": req.query,
        "flight_results": "",
        "hotel_results": "",
        "weather_results": "",
        "budget_results": "",
        "itinerary": "",
        "final_response": "",
        "llm_calls": 0,
    }


async def _new_thread_id(req: "PlanRequest") -> str:
    """Server-generated IDs are 128-bit random and carry no user identifier (SECURITY.md F2)."""
    if req.thread_id is None:
        return uuid.uuid4().hex
    if (await travel_graph.aget_state(_config(req.thread_id))).values:
        # Never let a new request write into someone else's existing thread.
        raise HTTPException(status_code=409, detail="Thread already exists")
    return req.thread_id


async def _snapshot(thread_id: str) -> PlanResponse:
    """Build a response from the checkpointed state of a thread."""
    snap = await travel_graph.aget_state(_config(thread_id))
    values = dict(snap.values or {})
    if not values:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")

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

    meta = await runs.get(thread_id) or {}
    return PlanResponse(
        thread_id=thread_id,
        status=status,
        state=_public(values),
        interrupt=interrupt_value,
        modes=meta.get("modes"),
        fallback=meta.get("fallback"),
        dummy_nodes=meta.get("dummy_nodes"),
    )


def _check_capacity() -> None:
    """Fast 503 when this process is already running its maximum number of plans."""
    if _run_slots is not None and _run_slots.locked():
        raise HTTPException(status_code=503, detail="Server busy, try again shortly", headers={"Retry-After": "5"})


async def _run_blocking(graph_input: Any, thread_id: str, api_keys: keys.ApiKeys) -> PlanResponse:
    """Same run as the stream (incl. dummy-data fallback), returning only the final state."""
    # aclosing: release the run slot and the graph stream even if we stop early.
    async with aclosing(_stream_graph(graph_input, thread_id, api_keys)) as frames:
        async for frame in frames:
            if frame.startswith("event: error"):
                message = json.loads(frame.split("data: ", 1)[1])["message"]
                raise HTTPException(status_code=500, detail=message)
    return await _snapshot(thread_id)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream_graph(graph_input: Any, thread_id: str, api_keys: keys.ApiKeys) -> AsyncIterator[str]:
    """Run the graph on the event loop and emit one SSE event per completed node.

    Provider keys and the per-run state live in ContextVars set here, in the task that
    serves this request; LangGraph copies the context into the tasks that run each node.
    """
    async with _run_slots:
        modes = keys.modes(api_keys)
        meta = await runs.get(thread_id) or {}
        meta["modes"] = modes
        meta.setdefault("dummy_nodes", {})
        meta.setdefault("fallback", None)
        await runs.save(thread_id, meta)
        yield _sse("start", {"thread_id": thread_id, "modes": modes})

        keys.set_current(api_keys)
        run = keys.start_run()
        dummy = meta["dummy_nodes"]
        announced = False
        if meta["fallback"]:
            # This plan already fell back to dummy data (e.g. during planning): keep the rest of it
            # (approval -> final plan) on dummy data too, so one plan never mixes live and dummy results.
            run.fallback = meta["fallback"]
            announced = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RUN_TIMEOUT_SECONDS
        chunks = travel_graph.astream(graph_input, config=_config(thread_id), stream_mode="updates")
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
                        yield _sse("interrupt", {"node": "human_approval", "data": payload})
                    else:
                        # Did this agent use dummy data? (read & reset per agent; agents run one at a time)
                        dummy[node] = run.used_dummy
                        run.used_dummy = False
                        await runs.save(thread_id, meta)
                        yield _sse("node", {"node": node, "data": _public(update or {}), "dummy": dummy[node]})
                if run.fallback and not announced:
                    # A live call failed: the run carries on with dummy data. Tell the UI once.
                    announced = True
                    meta["fallback"] = run.fallback
                    await runs.save(thread_id, meta)
                    yield _sse("fallback", run.fallback)
            yield _sse("done", (await _snapshot(thread_id)).model_dump())
        except TimeoutError:
            log.warning("graph_run_timeout", extra={"thread_id": thread_id, "timeout_s": RUN_TIMEOUT_SECONDS})
            yield _sse("error", {"message": f"The plan took longer than {RUN_TIMEOUT_SECONDS:.0f}s and was stopped."})
        except Exception as exc:  # surface errors to the UI instead of dropping the stream
            log.exception("graph_run_failed", extra={"thread_id": thread_id})
            yield _sse("error", {"message": keys.redact(str(exc), api_keys)})
        finally:
            await chunks.aclose()


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------- Rate limits ----------


def _client_id(request: Request) -> str:
    # The proxy-resolved client IP (uvicorn trusts X-Forwarded-For only from FORWARDED_ALLOW_IPS).
    # Replaced by the authenticated user/tenant once auth lands.
    return request.client.host if request.client else "unknown"


async def _enforce(request: Request, scope: str, limit: int) -> None:
    if not limits.enabled:
        return
    retry_after = await limiter.hit(f"{scope}:{_client_id(request)}", limit)
    if retry_after is not None:
        log.warning("rate_limited", extra={"scope": scope})
        raise HTTPException(
            status_code=429, detail="Too many requests, slow down", headers={"Retry-After": str(retry_after)}
        )


async def limit_requests(request: Request) -> None:
    await _enforce(request, "req", limits.requests_per_minute)


async def limit_runs(request: Request) -> None:
    """Graph runs cost LLM/tool calls: a tighter limit on top of the general one."""
    await _enforce(request, "run", limits.runs_per_minute)


# ---------- Routes ----------

router = APIRouter()


@router.get("/health")
async def health():
    """`modes`: whether the next plan will use live services or dummy data."""
    return {
        "status": "ok",
        "mock_mode": MOCK_MODE,
        "modes": keys.modes(keys.resolve()),
    }


@router.get("/agents", dependencies=[Depends(limit_requests)])
async def agents():
    """Static description of the pipeline, used by the UI to render the agent graph."""
    return [
        {"id": "supervisor", "name": "Supervisor", "description": "Guardrails + routes the request to specialists"},
        {"id": "flight_agent", "name": "Flights", "description": "Searches live flights via AviationStack MCP"},
        {"id": "hotel_agent", "name": "Hotels", "description": "Finds stays via Tavily search"},
        {"id": "weather_agent", "name": "Weather", "description": "Forecast via Weather MCP"},
        {"id": "budget_agent", "name": "Budget", "description": "Estimates trip cost"},
        {"id": "itinerary_agent", "name": "Itinerary", "description": "Drafts the day-by-day plan"},
        {"id": "human_approval", "name": "Your Review", "description": "Human-in-the-loop approval"},
        {"id": "final_response", "name": "Final Plan", "description": "Polishes the approved plan"},
    ]


@router.post("/plan", response_model=PlanResponse, dependencies=[Depends(limit_requests), Depends(limit_runs)])
async def create_plan(req: PlanRequest):
    """Run the agents until the human-approval checkpoint (waits for the result)."""
    _check_capacity()
    thread_id = await _new_thread_id(req)
    return await _run_blocking(_initial_input(req), thread_id, keys.resolve())


@router.post("/plan/stream", dependencies=[Depends(limit_requests), Depends(limit_runs)])
async def create_plan_stream(req: PlanRequest):
    """Same as POST /api/plan but streams agent progress as Server-Sent Events."""
    _check_capacity()
    thread_id = await _new_thread_id(req)
    return StreamingResponse(
        _stream_graph(_initial_input(req), thread_id, keys.resolve()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/plan/{thread_id}", response_model=PlanResponse, dependencies=[Depends(limit_requests)])
async def get_plan(thread_id: str):
    return await _snapshot(thread_id)


@router.post(
    "/plan/{thread_id}/approve",
    response_model=PlanResponse,
    dependencies=[Depends(limit_requests), Depends(limit_runs)],
)
async def approve_plan(thread_id: str, body: ApprovalRequest):
    """Resume a thread paused at human approval (waits for the result)."""
    current = await _snapshot(thread_id)
    if current.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")
    _check_capacity()
    return await _run_blocking(Command(resume=body.resume_value()), thread_id, keys.resolve())


@router.post("/plan/{thread_id}/approve/stream", dependencies=[Depends(limit_requests), Depends(limit_runs)])
async def approve_plan_stream(thread_id: str, body: ApprovalRequest):
    current = await _snapshot(thread_id)
    if current.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")
    _check_capacity()
    return StreamingResponse(
        _stream_graph(Command(resume=body.resume_value()), thread_id, keys.resolve()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


api.include_router(router, prefix="/api/v1", tags=["v1"])
# Deprecated aliases for web builds that predate /api/v1 (Deprecation + Link headers added above).
api.include_router(router, prefix="/api", tags=["legacy"], deprecated=True, include_in_schema=False)
