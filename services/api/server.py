"""FastAPI server exposing the multi-agent travel planner to the Next.js frontend.

Run:
    uvicorn server:api --reload --port 8000
"""

import contextvars
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

import logging_setup

logging_setup.configure("api")

import keys  # noqa: E402
import mock  # noqa: E402
import run_store  # noqa: E402
from graph import app as travel_graph  # noqa: E402

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

api = FastAPI(
    title="Multi-Agent Travel Planner API",
    version="1.0.0",
    description="LangGraph supervisor + specialist agents with human-in-the-loop approval.",
)

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
    expose_headers=["X-Request-ID"],
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
    return {"configurable": {"thread_id": thread_id}}


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


# Per-thread run info (no secrets): service modes, fallback reason, which agents used dummy data.
runs: run_store.RunStore = run_store.build()


def _new_thread_id(req: "PlanRequest") -> str:
    """Server-generated IDs are 128-bit random and carry no user identifier (SECURITY.md F2)."""
    if req.thread_id is None:
        return uuid.uuid4().hex
    if travel_graph.get_state(_config(req.thread_id)).values:
        # Never let a new request write into someone else's existing thread.
        raise HTTPException(status_code=409, detail="Thread already exists")
    return req.thread_id


def _snapshot(thread_id: str) -> PlanResponse:
    """Build a response from the checkpointed state of a thread."""
    snap = travel_graph.get_state(_config(thread_id))
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

    meta = runs.get(thread_id) or {}
    return PlanResponse(
        thread_id=thread_id,
        status=status,
        state=_public(values),
        interrupt=interrupt_value,
        modes=meta.get("modes"),
        fallback=meta.get("fallback"),
        dummy_nodes=meta.get("dummy_nodes"),
    )


def _run_blocking(graph_input: Any, thread_id: str, api_keys: keys.ApiKeys) -> PlanResponse:
    """Same run as the stream (incl. dummy-data fallback), returning only the final state."""
    for frame in _stream_graph(graph_input, thread_id, api_keys):
        if frame.startswith("event: error"):
            message = json.loads(frame.split("data: ", 1)[1])["message"]
            raise HTTPException(status_code=500, detail=message)
    return _snapshot(thread_id)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream_graph(graph_input: Any, thread_id: str, api_keys: keys.ApiKeys) -> Iterator[str]:
    """Run the graph and emit one SSE event per completed node.

    Sync generator: Starlette iterates it in a threadpool, so the blocking
    LangGraph/LLM calls don't stall the event loop. Each next() may run in a
    different thread/context, so every step runs inside one pinned context that
    carries this run's provider keys.
    """
    modes = keys.modes(api_keys)
    meta = runs.get(thread_id) or {}
    meta["modes"] = modes
    meta.setdefault("dummy_nodes", {})
    meta.setdefault("fallback", None)
    runs.save(thread_id, meta)
    yield _sse("start", {"thread_id": thread_id, "modes": modes})

    ctx = contextvars.copy_context()
    ctx.run(keys.set_current, api_keys)
    run = ctx.run(keys.start_run)
    dummy = meta["dummy_nodes"]
    announced = False
    if meta["fallback"]:
        # This plan already fell back to dummy data (e.g. during planning): keep the rest of it
        # (approval → final plan) on dummy data too, so one plan never mixes live and dummy results.
        run.fallback = meta["fallback"]
        announced = True
    try:
        chunks = ctx.run(
            lambda: iter(travel_graph.stream(graph_input, config=_config(thread_id), stream_mode="updates"))
        )
        while True:
            try:
                chunk = ctx.run(next, chunks)
            except StopIteration:
                break
            for node, update in chunk.items():
                if node == "__interrupt__":
                    payload = update[0].value if update else {}
                    yield _sse("interrupt", {"node": "human_approval", "data": payload})
                else:
                    # Did this agent use dummy data? (read & reset per agent; agents run one at a time)
                    dummy[node] = run.used_dummy
                    run.used_dummy = False
                    runs.save(thread_id, meta)
                    yield _sse("node", {"node": node, "data": _public(update or {}), "dummy": dummy[node]})
            if run.fallback and not announced:
                # A live call failed: the run carries on with dummy data. Tell the UI once.
                announced = True
                meta["fallback"] = run.fallback
                runs.save(thread_id, meta)
                yield _sse("fallback", run.fallback)
        yield _sse("done", _snapshot(thread_id).model_dump())
    except Exception as exc:  # surface errors to the UI instead of dropping the stream
        log.exception("graph_run_failed", extra={"thread_id": thread_id})
        yield _sse("error", {"message": keys.redact(str(exc), api_keys)})


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------- Routes ----------


@api.get("/api/health")
def health():
    """`modes`: whether the next plan will use live services or dummy data."""
    return {
        "status": "ok",
        "mock_mode": MOCK_MODE,
        "modes": keys.modes(keys.resolve()),
    }


@api.get("/api/agents")
def agents():
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


@api.post("/api/plan", response_model=PlanResponse)
def create_plan(req: PlanRequest):
    """Run the agents until the human-approval checkpoint (blocking)."""
    thread_id = _new_thread_id(req)
    return _run_blocking(_initial_input(req), thread_id, keys.resolve())


@api.post("/api/plan/stream")
def create_plan_stream(req: PlanRequest):
    """Same as POST /api/plan but streams agent progress as Server-Sent Events."""
    thread_id = _new_thread_id(req)
    return StreamingResponse(
        _stream_graph(_initial_input(req), thread_id, keys.resolve()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@api.get("/api/plan/{thread_id}", response_model=PlanResponse)
def get_plan(thread_id: str):
    return _snapshot(thread_id)


@api.post("/api/plan/{thread_id}/approve", response_model=PlanResponse)
def approve_plan(thread_id: str, body: ApprovalRequest):
    """Resume a thread paused at human approval (blocking)."""
    current = _snapshot(thread_id)
    if current.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")
    return _run_blocking(Command(resume=body.resume_value()), thread_id, keys.resolve())


@api.post("/api/plan/{thread_id}/approve/stream")
def approve_plan_stream(thread_id: str, body: ApprovalRequest):
    current = _snapshot(thread_id)
    if current.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")
    return StreamingResponse(
        _stream_graph(Command(resume=body.resume_value()), thread_id, keys.resolve()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
