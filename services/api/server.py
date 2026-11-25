"""FastAPI server exposing the multi-agent travel planner to the Next.js frontend.

Run:
    uvicorn server:api --reload --port 8000
"""

import datetime as dt
import json
import logging
import os
import re
import time
import uuid
from contextlib import aclosing, asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

import logging_setup

logging_setup.configure("api")

import auth  # noqa: E402
import guardrails  # noqa: E402
import memory  # noqa: E402
import mock  # noqa: E402
import orchestrator  # noqa: E402
import rate_limit  # noqa: E402
import redis_conn  # noqa: E402
import threads  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from rag import extract as rag_extract  # noqa: E402
from rag import ingest as rag_ingest  # noqa: E402
from rag import retrieval as rag_retrieval  # noqa: E402
from rag import store as rag_store  # noqa: E402

log = logging.getLogger("voyagent.api")

MOCK_MODE = mock.enabled()

# Created in the lifespan, inside the server's event loop (async pools are loop-bound).
orch: orchestrator.Orchestrator = None  # type: ignore[assignment]
limiter: rate_limit.Limiter = None  # type: ignore[assignment]
limits = rate_limit.Limits.from_env()
thread_store: threads.ThreadStore = None  # type: ignore[assignment]
authenticator: auth.Authenticator = None  # type: ignore[assignment]
documents: rag_store.RagStore = None  # type: ignore[assignment]
ingest_queue: rag_ingest.Queue = None  # type: ignore[assignment]


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Open stores at startup (fail fast on misconfiguration) and close them on shutdown.

    Plans run in the orchestrator: in-process by default, or the internal orchestrator service
    when ORCHESTRATOR_URL is set (then this process holds no provider keys or graph state).
    """
    global orch, limiter, limits, thread_store, authenticator, documents, ingest_queue
    limits = rate_limit.Limits.from_env()
    authenticator = auth.Authenticator(auth.AuthConfig.from_env())
    log.info("auth_ready", extra={"mode": authenticator.config.mode})
    redis = await redis_conn.connect()
    store = None
    try:
        limiter = rate_limit.build(redis)
        store = thread_store = await threads.open_store(DATABASE_URL)
        documents = await rag_store.open_store(DATABASE_URL)
        ingest_queue = rag_ingest.Queue(redis, documents)
        orch = await orchestrator.open_from_env()
    except BaseException:
        if store is not None:
            await store.aclose()
        if redis is not None:
            await redis.aclose()
        raise
    try:
        yield
    finally:
        await ingest_queue.drain()
        await orch.aclose()
        await documents.aclose()
        await thread_store.aclose()
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

    def __init__(self, app, max_bytes: int, overrides: dict[tuple[str, str], int] | None = None):
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides or {}  # (method, exact path) -> larger limit, e.g. uploads

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.overrides.get((scope.get("method", ""), scope.get("path", "")), self.max_bytes)
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    too_big = int(value) > limit
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
                if seen > limit and not started:
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
api.add_middleware(
    BodySizeLimit,
    max_bytes=MAX_BODY_BYTES,
    overrides={(m, f"{p}/documents"): rag_extract.MAX_BYTES + 64 * 1024 for m in ("POST",) for p in ("/api/v1", "/api")},
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
    expose_headers=["X-Request-ID", "Retry-After"],
    max_age=600,
)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def _route_template(path: str, route) -> str:
    """Route template incl. its mount prefix (FastAPI keeps router routes unprefixed in scope)."""
    if route is None:
        return path  # unmatched (404) or CORS preflight
    prefix = "/api/v1" if path.startswith("/api/v1/") else "/api" if path.startswith("/api/") else ""
    return prefix + route.path


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
                "route": _route_template(request.url.path, route),
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
    # Deprecated and ignored: the user comes from the access token. Accepted so older web builds still validate.
    user_id: str | None = Field(None, description="Deprecated, ignored", deprecated=True)
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


async def _new_thread_id(req: "PlanRequest", principal: auth.Principal) -> str:
    """Create the plan's ownership record. Server IDs are 128-bit random with no user identifier.

    A client-chosen ID must be new: never let a request write into an existing thread (409).
    """
    thread_id = req.thread_id or uuid.uuid4().hex
    if req.thread_id and await orch.exists(thread_id):
        raise HTTPException(status_code=409, detail="Thread already exists")
    try:
        await thread_store.create(thread_id, principal.tenant_id, principal.sub)
    except threads.ThreadExists:
        raise HTTPException(status_code=409, detail="Thread already exists") from None
    return thread_id


def _end_user(principal: auth.Principal) -> str:
    return f"{principal.tenant_id}:{principal.sub}"


async def _authorize(thread_id: str, principal: auth.Principal) -> threads.ThreadRecord:
    """404 (not 403) for plans the caller can't access, so IDs can't be probed."""
    rec = await thread_store.get(thread_id)
    if rec is None or not auth.can_access(principal, rec.tenant_id, rec.owner_sub):
        if rec is not None:
            log.warning("plan_access_denied", extra={"thread_id": thread_id})
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
    return rec


async def _snapshot(thread_id: str) -> PlanResponse:
    snap = await orch.snapshot(thread_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
    return PlanResponse(**snap)


async def _start(thread_id: str, kind: str, payload: dict, principal: auth.Principal):
    """Start or resume a run; 503 (before any bytes are sent) when the orchestrator is full."""
    try:
        return await orch.start(thread_id, kind, payload, _end_user(principal), principal.tenant_id)
    except orchestrator.Busy:
        raise HTTPException(status_code=503, detail="Server busy, try again shortly", headers={"Retry-After": "5"}) from None
    except orchestrator.OrchestratorError:
        log.exception("orchestrator_unavailable")
        raise HTTPException(status_code=502, detail="Planner temporarily unavailable") from None


async def _run_blocking(thread_id: str, kind: str, payload: dict, principal: auth.Principal) -> PlanResponse:
    """Same run as the stream (incl. dummy-data fallback), returning only the final state."""
    stream = await _start(thread_id, kind, payload, principal)
    text = ""
    # aclosing: release the run slot and the upstream stream even if we stop early.
    async with aclosing(stream) as frames:
        async for chunk in frames:
            text += chunk
    for block in text.split("\n\n"):
        if block.startswith("event: error"):
            raise HTTPException(status_code=500, detail=json.loads(block.split("data: ", 1)[1])["message"])
    return await _snapshot(thread_id)


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---------- Rate limits ----------


def _client_id(request: Request) -> str:
    """Authenticated user when known (auth runs first on protected routes), else client IP.

    The IP is proxy-resolved: uvicorn trusts X-Forwarded-For only from FORWARDED_ALLOW_IPS.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"user:{principal.tenant_id}:{principal.sub}"
    return "ip:" + (request.client.host if request.client else "unknown")


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


def _screen(text: str) -> str:
    """Input rails (guardrails/policy.yaml): 400 on injection/jailbreak patterns; PII redacted."""
    verdict = guardrails.check_input(text)
    if not verdict.allowed:
        raise HTTPException(status_code=400, detail="Request blocked by content policy")
    return verdict.text


def _start_payload(req: "PlanRequest", principal: auth.Principal) -> dict:
    return {"query": _screen(req.query), "user_id": principal.sub}


def _resume_payload(body: "ApprovalRequest") -> dict:
    return {"approved": body.approved, "feedback": _screen(body.feedback) if body.feedback else ""}


async def current_principal(request: Request) -> auth.Principal:
    return await authenticator(request)


Caller = Annotated[auth.Principal, Depends(current_principal)]
# Order matters: authenticate first so limits key on the user, not the IP.
_READ = [Depends(current_principal), Depends(limit_requests)]
_RUN = [Depends(current_principal), Depends(limit_requests), Depends(limit_runs)]


# ---------- Routes ----------

router = APIRouter()


@router.get("/health")
async def health():
    """`modes`: whether the next plan will use live services or dummy data."""
    return {
        "status": "ok",
        "mock_mode": MOCK_MODE,
        "modes": await orch.modes(),
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


class PlanSummary(BaseModel):
    thread_id: str
    created_at: dt.datetime
    owner: bool  # False only for tenant admins listing other users' plans


class PlanList(BaseModel):
    items: list[PlanSummary]
    next_before: dt.datetime | None = None  # pass as `before` for the next page


@router.get("/plans", response_model=PlanList, dependencies=_READ)
async def list_plans(
    principal: Caller,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: dt.datetime | None = None,
    all_users: bool = False,
):
    """Your plans, newest first. Tenant admins may pass all_users=true for the whole tenant."""
    if all_users and not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    owner = None if all_users else principal.sub
    rows = await thread_store.list(principal.tenant_id, owner, limit, before)
    items = [PlanSummary(thread_id=r.thread_id, created_at=r.created_at, owner=r.owner_sub == principal.sub) for r in rows]
    return PlanList(items=items, next_before=rows[-1].created_at if len(rows) == limit else None)


@router.post("/plan", response_model=PlanResponse, dependencies=_RUN)
async def create_plan(req: PlanRequest, principal: Caller):
    """Run the agents until the human-approval checkpoint (waits for the result)."""
    payload = _start_payload(req, principal)
    thread_id = await _new_thread_id(req, principal)
    return await _run_blocking(thread_id, "start", payload, principal)


@router.post("/plan/stream", dependencies=_RUN)
async def create_plan_stream(req: PlanRequest, principal: Caller):
    """Same as POST /plan but streams agent progress as Server-Sent Events."""
    payload = _start_payload(req, principal)
    thread_id = await _new_thread_id(req, principal)
    return StreamingResponse(
        await _start(thread_id, "start", payload, principal),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/plan/{thread_id}", response_model=PlanResponse, dependencies=_READ)
async def get_plan(thread_id: str, principal: Caller):
    await _authorize(thread_id, principal)
    return await _snapshot(thread_id)


@router.post(
    "/plan/{thread_id}/approve",
    response_model=PlanResponse,
    dependencies=_RUN,
)
async def approve_plan(thread_id: str, body: ApprovalRequest, principal: Caller):
    """Resume a thread paused at human approval (waits for the result)."""
    await _authorize(thread_id, principal)
    current = await _snapshot(thread_id)
    if current.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")
    return await _run_blocking(thread_id, "resume", _resume_payload(body), principal)


@router.post("/plan/{thread_id}/approve/stream", dependencies=_RUN)
async def approve_plan_stream(thread_id: str, body: ApprovalRequest, principal: Caller):
    await _authorize(thread_id, principal)
    current = await _snapshot(thread_id)
    if current.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")
    return StreamingResponse(
        await _start(thread_id, "resume", _resume_payload(body), principal),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


class MemoryView(BaseModel):
    enabled: bool
    preferences: dict[str, str]
    retention_days: int


@router.get("/me/memory", response_model=MemoryView, dependencies=_READ)
async def get_memory(principal: Caller):
    """What Voyagent remembers about you (from plans you approved) and for how long."""
    prefs = await orch.get_memory(_end_user(principal)) if memory.enabled() else {}
    return MemoryView(enabled=memory.enabled(), preferences=prefs, retention_days=memory.retention().days)


@router.delete("/me/memory", status_code=204, dependencies=_READ)
async def delete_memory(principal: Caller):
    """Forget all remembered preferences."""
    await orch.delete_memory(_end_user(principal))
    log.info("memory_deleted")


@router.delete("/plans/{thread_id}", status_code=204, dependencies=_READ)
async def delete_plan(thread_id: str, principal: Caller):
    """Delete a plan and its stored state (checkpoints, run metadata, ownership record)."""
    rec = await _authorize(thread_id, principal)
    if rec.owner_sub != principal.sub and not principal.is_admin:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
    await orch.delete(thread_id)
    await thread_store.delete(thread_id)
    log.info("plan_deleted", extra={"thread_id": thread_id, "by_admin": rec.owner_sub != principal.sub})


# ---------- Organisation documents (RAG) ----------


class DocumentView(BaseModel):
    id: str
    title: str
    mime: str
    version: int
    status: str
    scan: str
    chunk_count: int
    error: str | None = None
    created_at: dt.datetime | None = None


def _doc_view(d: rag_store.DocumentRecord) -> DocumentView:
    return DocumentView(**{k: getattr(d, k) for k in DocumentView.model_fields})


def _require_admin(principal: auth.Principal) -> None:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")


@router.post("/documents", status_code=202, response_model=DocumentView, dependencies=_RUN)
async def upload_document(
    principal: Caller,
    file: Annotated[UploadFile, File(description="PDF, Markdown or plain text, max 5 MiB")],
    title: Annotated[str, Form(min_length=1, max_length=200)],
):
    """Add a document to your organisation's knowledge (tenant admins). Processed asynchronously:
    poll GET /documents/{id} until status is `ready` or `failed`."""
    _require_admin(principal)
    data = await file.read(rag_extract.MAX_BYTES + 1)
    try:
        doc, duplicate = await rag_ingest.accept_upload(documents, principal.tenant_id, title.strip(), data)
    except rag_extract.Rejected as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if not duplicate:
        await ingest_queue.enqueue(doc.id)
    log.info("document_uploaded", extra={"document_id": doc.id, "duplicate": duplicate, "bytes": len(data)})
    return _doc_view(doc)


@router.get("/documents", response_model=list[DocumentView], dependencies=_READ)
async def list_documents(principal: Caller):
    return [_doc_view(d) for d in await documents.list_documents(principal.tenant_id)]


class SearchResult(BaseModel):
    label: int
    document_id: str
    title: str
    text: str


@router.get("/documents/search", response_model=list[SearchResult], dependencies=_READ)
async def search_documents(
    principal: Caller,
    q: Annotated[str, Query(min_length=2, max_length=500)],
    k: Annotated[int, Query(ge=1, le=10)] = 4,
):
    """What the agents would retrieve for this query from your organisation's documents."""
    passages = await rag_retrieval.search(documents, principal.tenant_id, q, k=k)
    return [SearchResult(label=p.label, document_id=p.document_id, title=p.title, text=p.text) for p in passages]


@router.get("/documents/{doc_id}", response_model=DocumentView, dependencies=_READ)
async def get_document(doc_id: str, principal: Caller):
    doc = await documents.get_document(principal.tenant_id, _uuid(doc_id))
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return _doc_view(doc)


@router.delete("/documents/{doc_id}", status_code=204, dependencies=_READ)
async def delete_document(doc_id: str, principal: Caller):
    _require_admin(principal)
    if not await documents.delete_document(principal.tenant_id, _uuid(doc_id)):
        raise HTTPException(status_code=404, detail="Document not found")
    log.info("document_deleted", extra={"document_id": doc_id})


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found") from None


api.include_router(router, prefix="/api/v1", tags=["v1"])
# Deprecated aliases for web builds that predate /api/v1 (Deprecation + Link headers added above).
api.include_router(router, prefix="/api", tags=["legacy"], deprecated=True, include_in_schema=False)
