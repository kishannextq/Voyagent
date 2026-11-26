"""Prompt-injection classifier service (internal; never exposed publicly).

Model: protectai/deberta-v3-base-prompt-injection-v2 (Apache-2.0), ONNX, pinned revision baked
into the image with checksums (Dockerfile). Labels: SAFE / INJECTION. English-centric: other
languages are less reliable (docs/SECURITY.md F5).

    POST /v1/classify  {"texts": ["...", ...]}  ->  {"results": [{"injection": bool, "score": float}]}

Texts longer than the model's 512-token window are scored in overlapping windows and the
highest window score is used, so an injection can't hide at the end of a long document.
Auth: `X-Internal-Token: <GUARD_TOKEN>` (required outside local/test).
"""

import asyncio
import hmac
import json
import logging
import os
import pathlib
import sys
import time
from contextlib import asynccontextmanager

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

MODEL_DIR = pathlib.Path(os.getenv("GUARD_MODEL_DIR", "/models/prompt-injection"))
THRESHOLD = float(os.getenv("GUARD_THRESHOLD", "0.5"))
WINDOW = 512
STRIDE = 384
MAX_TEXTS = 32
MAX_CHARS = 20_000


class _Json(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "service": "guard",
            "event": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k in ("count", "flagged", "ms", "error_type")})
        return json.dumps(entry)


_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(_Json())
logging.basicConfig(level=logging.INFO, handlers=[_h], force=True)
log = logging.getLogger("guard")


class Classifier:
    def __init__(self, model_dir: pathlib.Path, session=None, tokenizer=None):
        if tokenizer is None:
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        if session is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(os.getenv("GUARD_THREADS", "2"))
            session = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.tok = tokenizer
        self.tok.no_padding()
        self.tok.no_truncation()
        self.session = session
        self.inputs = {i.name for i in session.get_inputs()}
        cfg = json.loads((model_dir / "config.json").read_text()) if (model_dir / "config.json").exists() else {}
        self.injection_index = int((cfg.get("label2id") or {}).get("INJECTION", 1))

    def _windows(self, ids: list[int]) -> list[list[int]]:
        cls_id, sep_id = ids[0], ids[-1]
        body = ids[1:-1]
        size = WINDOW - 2
        if len(body) <= size:
            return [ids]
        starts = list(range(0, len(body) - size, STRIDE)) + [len(body) - size]
        return [[cls_id, *body[s : s + size], sep_id] for s in starts]

    def score(self, text: str) -> float:
        ids = self.tok.encode(text[:MAX_CHARS]).ids
        best = 0.0
        for window in self._windows(ids):
            arr = np.array([window], dtype=np.int64)
            feed = {"input_ids": arr}
            if "attention_mask" in self.inputs:
                feed["attention_mask"] = np.ones_like(arr)
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(arr)
            logits = self.session.run(None, feed)[0][0].astype(np.float64)
            probs = np.exp(logits - logits.max())
            probs /= probs.sum()
            best = max(best, float(probs[self.injection_index]))
        return best


classifier: Classifier | None = None
_token = ""


@asynccontextmanager
async def lifespan(_: FastAPI):
    global classifier, _token
    _token = os.getenv("GUARD_TOKEN", "").strip()
    if not _token and os.getenv("APP_ENV", "local") in {"staging", "production"}:
        raise RuntimeError("GUARD_TOKEN is required outside local/test")
    classifier = Classifier(MODEL_DIR)
    classifier.score("warm up")
    log.info("guard_ready")
    yield


app = FastAPI(title="Voyagent guard (internal)", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def internal_auth(request: Request) -> None:
    if _token and not hmac.compare_digest(request.headers.get("x-internal-token", "").encode(), _token.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: list[str] = Field(..., min_length=1, max_length=MAX_TEXTS)


@app.get("/health")
async def health():
    return {"status": "ok" if classifier else "loading"}


@app.post("/v1/classify", dependencies=[Depends(internal_auth)])
async def classify(body: ClassifyRequest):
    start = time.perf_counter()
    scores = await asyncio.to_thread(lambda: [classifier.score(t) for t in body.texts])
    results = [{"injection": s >= THRESHOLD, "score": round(s, 4)} for s in scores]
    log.info(
        "classified",
        extra={
            "count": len(scores),
            "flagged": sum(r["injection"] for r in results),
            "ms": round((time.perf_counter() - start) * 1000, 1),
        },
    )
    return {"results": results, "threshold": THRESHOLD}
