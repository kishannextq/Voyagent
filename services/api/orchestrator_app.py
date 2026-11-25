"""Internal orchestrator service: runs plans for the public API (never exposed publicly).

Same image as the API, different command:
    uvicorn orchestrator_app:app --port 8001

Every request needs `X-Internal-Token: <ORCHESTRATOR_TOKEN>` (required in staging/production;
in AWS the service is also reachable only from the API's security group). Callers are trusted
to have authenticated the user and checked plan ownership.
"""

import hmac
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import logging_setup

logging_setup.configure("orchestrator")

from fastapi import Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

import orchestrator  # noqa: E402
import settings  # noqa: E402

log = logging.getLogger("voyagent.orchestrator_app")

_THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

orch: orchestrator.LocalOrchestrator = None  # type: ignore[assignment]
_token = ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    global orch, _token
    _token = os.getenv("ORCHESTRATOR_TOKEN", "").strip()
    if not _token:
        if settings.durable_required():
            raise RuntimeError(f"ORCHESTRATOR_TOKEN is required when APP_ENV={settings.app_env()}")
        log.warning("orchestrator_token_unset", extra={"note": "local/test only: internal calls are not authenticated"})
    orch = await orchestrator.LocalOrchestrator.open()
    try:
        yield
    finally:
        await orch.aclose()


app = FastAPI(title="Voyagent orchestrator (internal)", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _request_context(request: Request, call_next):
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
    token = logging_setup.request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        log.info(
            "http_request",
            extra={
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        logging_setup.request_id_var.reset(token)


async def internal_auth(request: Request) -> None:
    if not _token:
        return
    supplied = request.headers.get("x-internal-token", "")
    if not hmac.compare_digest(supplied.encode(), _token.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _thread(thread_id: str) -> str:
    if not _THREAD_RE.match(thread_id):
        raise HTTPException(status_code=422, detail="Invalid thread id")
    return thread_id


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["start", "resume"]
    payload: dict
    end_user: str = Field("", max_length=300)


@app.get("/internal/v1/health")
async def health():
    return {"status": "ok"}


@app.get("/internal/v1/modes", dependencies=[Depends(internal_auth)])
async def modes():
    return await orch.modes()


@app.get("/internal/v1/threads/{thread_id}", dependencies=[Depends(internal_auth)])
async def snapshot(thread_id: str):
    snap = await orch.snapshot(_thread(thread_id))
    if snap is None:
        raise HTTPException(status_code=404, detail="Not found")
    return snap


@app.delete("/internal/v1/threads/{thread_id}", status_code=204, dependencies=[Depends(internal_auth)])
async def delete(thread_id: str):
    await orch.delete(_thread(thread_id))


@app.get("/internal/v1/memory", dependencies=[Depends(internal_auth)])
async def get_memory(owner: str = Query(..., min_length=1, max_length=300)):
    return await orch.get_memory(owner)


@app.delete("/internal/v1/memory", status_code=204, dependencies=[Depends(internal_auth)])
async def delete_memory(owner: str = Query(..., min_length=1, max_length=300)):
    await orch.delete_memory(owner)


@app.post("/internal/v1/threads/{thread_id}/runs", dependencies=[Depends(internal_auth)])
async def run(thread_id: str, body: RunRequest):
    try:
        stream = await orch.start(_thread(thread_id), body.kind, body.payload, body.end_user)
    except orchestrator.Busy:
        raise HTTPException(status_code=503, detail="Busy", headers={"Retry-After": "5"}) from None
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid run payload") from None
    return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
