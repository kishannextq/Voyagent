"""OpenAI-compatible mock model server, for gateway tests only (no real model, no keys).

- POST /v1/chat/completions: echoes a deterministic reply naming the model that answered.
  Models listed in MOCK_LLM_FAIL_MODELS (comma-separated) return HTTP 500, to test fallbacks.
- GET /_requests: the requests received (model, user), so tests can assert routing/attribution.

Run: uvicorn server:app --app-dir tools/mock-llm --port 9200
"""

import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="mock-llm (test only)")
_seen: list[dict] = []


def _fail_models() -> set[str]:
    return {m.strip() for m in os.getenv("MOCK_LLM_FAIL_MODELS", "").split(",") if m.strip()}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "")
    _seen.append({"model": model, "user": body.get("user")})
    if model in _fail_models():
        raise HTTPException(status_code=500, detail="mock upstream failure")
    last = (body.get("messages") or [{}])[-1].get("content", "")
    content = f"[{model}] ok: {str(last)[:40]}"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@app.get("/_requests")
def requests_seen():
    return _seen


@app.delete("/_requests")
def reset():
    _seen.clear()
    return {"ok": True}
