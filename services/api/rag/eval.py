"""Retrieval evaluation: chunking strategies x retrieval modes on a golden dataset.

    uv run python -m rag.eval                    # hash embedder (lexical; pipeline check)
    RAG_EMBEDDER=fastembed uv run --group rag-local python -m rag.eval   # semantic embedder

A retrieved passage is relevant when it contains one of the question's answer phrases (so the
judgement doesn't depend on chunk boundaries). Metrics @k: Recall (any relevant passage in the
top k), Precision, MRR, NDCG (binary relevance).

Faithfulness of generated answers: `citation_support` checks that each cited sentence shares
content words with the passage it cites. A model-graded faithfulness judge is planned.
"""

import asyncio
import json
import math
import pathlib
import re
import sys
from dataclasses import dataclass

from rag import chunking, embeddings, ingest, retrieval
from rag.store import MemoryRagStore

DATA = pathlib.Path(__file__).parent / "eval_data"
TENANT = "eval"

STRATEGIES = {
    "fixed-400": (400, 400),
    "fixed-1000": (1000, 1000),
    "parent-2000/child-400": (2000, 400),
}
MODES = ("vector", "text", "hybrid")


@dataclass
class Scores:
    recall: float
    precision: float
    mrr: float
    ndcg: float


def _relevant(text: str, answers: list[str]) -> bool:
    t = re.sub(r"\s+", " ", text.lower())
    return any(a in t for a in answers)


def score(ranked: list[str], answers: list[str], k: int) -> Scores:
    rel = [_relevant(t, answers) for t in ranked[:k]]
    first = next((i for i, r in enumerate(rel) if r), None)
    dcg = sum(1 / math.log2(i + 2) for i, r in enumerate(rel) if r)
    return Scores(
        recall=1.0 if first is not None else 0.0,
        precision=sum(rel) / k,
        mrr=1 / (first + 1) if first is not None else 0.0,
        ndcg=min(1.0, dcg),  # ideal DCG = 1 (one relevant passage at rank 1)
    )


async def build_index(parent_size: int, child_size: int, embedder) -> MemoryRagStore:
    store = MemoryRagStore()
    for path in sorted(DATA.glob("*.md")):
        data = path.read_bytes()
        doc, _ = await ingest.accept_upload(store, TENANT, path.stem, data)
        text = data.decode("utf-8")
        parents = chunking.parent_child(text, parent_size, child_size, overlap=50 if child_size < parent_size else 0)
        children = [c for p in parents for c in p.children]
        vecs = iter(await embedder.embed(children))
        await store.replace_chunks(doc.id, TENANT, embedder.model_id, [(p.text, [(c, next(vecs)) for c in p.children]) for p in parents])
        await store.set_status(doc.id, "ready", chunk_count=len(children))
    return store


async def evaluate(k: int = 3, embedder=None) -> dict[str, dict[str, Scores]]:
    embedder = embedder or embeddings.get()
    questions = json.loads((DATA / "questions.json").read_text(encoding="utf-8"))
    results: dict[str, dict[str, Scores]] = {}
    for name, (parent, child) in STRATEGIES.items():
        store = await build_index(parent, child, embedder)
        results[name] = {}
        for mode in MODES:
            per_q = []
            for item in questions:
                passages = await retrieval.search(store, TENANT, item["q"], k=k, embedder=embedder, mode=mode)
                # Relevance is judged on what the model would read (the parent passage).
                per_q.append(score([p.text for p in passages], [a.lower() for a in item["answers"]], k))
            n = len(per_q)
            results[name][mode] = Scores(*(round(sum(getattr(s, f) for s in per_q) / n, 3) for f in Scores.__dataclass_fields__))
    return results


def citation_support(answer: str, passages: dict[int, str], min_overlap: float = 0.3) -> float:
    """Share of cited sentences whose content words overlap the cited passage by >= min_overlap."""
    words = re.compile(r"[a-z0-9]{4,}")
    cited = [(s, [int(n) for n in re.findall(r"\[(\d+)\]", s)]) for s in re.split(r"(?<=[.!?])\s+", answer)]
    cited = [(s, ns) for s, ns in cited if ns]
    if not cited:
        return 1.0
    ok = 0
    for sentence, labels in cited:
        sw = set(words.findall(sentence.lower()))
        best = max((len(sw & set(words.findall(passages.get(n, "").lower()))) / (len(sw) or 1) for n in labels), default=0)
        ok += best >= min_overlap
    return ok / len(cited)


def to_markdown(results, k: int, model_id: str) -> str:
    lines = [f"Embedder `{model_id}`, k={k}", "", "| strategy | mode | Recall@k | Precision@k | MRR | NDCG@k |", "|---|---|---|---|---|---|"]
    for name, modes in results.items():
        for mode, s in modes.items():
            lines.append(f"| {name} | {mode} | {s.recall} | {s.precision} | {s.mrr} | {s.ndcg} |")
    return "\n".join(lines)


if __name__ == "__main__":
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    emb = embeddings.get()
    print(to_markdown(asyncio.run(evaluate(k, emb)), k, emb.model_id))
