"""Embedding providers behind one interface; the model id is stored with every vector.

RAG_EMBEDDER:
- gateway   (deployed): OpenAI-compatible /v1/embeddings on the model gateway (alias
            RAG_EMBED_MODEL, e.g. Bedrock Titan/Cohere configured in LiteLLM). Not verified yet.
- fastembed (local/CI eval): BAAI/bge-small-en-v1.5 via ONNX, 384 dims. Downloads the model on
            first use; never used in deployed images (not installed there).
- hash      (tests/default locally): deterministic feature hashing of words and word pairs.
            Lexical only, not semantic: good for exercising the pipeline, not for quality.
All vectors are L2-normalised and RAG_EMBED_DIM long (default 384).
"""

import asyncio
import hashlib
import math
import os
import re
from typing import Protocol

import httpx

DIM = int(os.getenv("RAG_EMBED_DIM", "384"))
_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    model_id: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class HashEmbedder:
    def __init__(self, dim: int = DIM):
        self.dim = dim
        self.model_id = f"hash-v1-{dim}"

    def _one(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        v = [0.0] * self.dim
        for feat in words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]:
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return _normalise(v)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class FastEmbedEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model)
        self.model_id = f"fastembed:{model}"
        self.dim = 384

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = await asyncio.to_thread(lambda: list(self._model.embed(texts)))
        return [_normalise([float(x) for x in v]) for v in vecs]


class GatewayEmbedder:
    def __init__(self, url: str, key: str, model: str, dim: int = DIM):
        self._url = url.rstrip("/") + "/v1/embeddings"
        self._key = key
        self.model_id = f"gateway:{model}"
        self._model = model
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                self._url,
                json={"model": self._model, "input": texts, "dimensions": self.dim},
                headers={"Authorization": f"Bearer {self._key}"},
            )
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [_normalise(d["embedding"]) for d in data]


_instance: Embedder | None = None


def get() -> Embedder:
    global _instance
    if _instance is None:
        kind = os.getenv("RAG_EMBEDDER", "hash").strip().lower()
        if kind == "gateway":
            _instance = GatewayEmbedder(
                os.environ["LLM_GATEWAY_URL"], os.environ["LLM_GATEWAY_KEY"], os.getenv("RAG_EMBED_MODEL", "voyagent-embed")
            )
        elif kind == "fastembed":
            _instance = FastEmbedEmbedder()
        elif kind == "hash":
            _instance = HashEmbedder()
        else:
            raise RuntimeError("RAG_EMBEDDER must be gateway, fastembed or hash")
    return _instance
