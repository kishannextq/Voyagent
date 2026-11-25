# RAG evaluation

Harness: `services/api/rag/eval.py`; data: `services/api/rag/eval_data/` (four short fictional
documents: a corporate travel policy, two city guides, visa notes; 22 questions, each with the
answer phrases a correct passage must contain). Relevance is judged on the passage the model
would read, so results don't depend on chunk boundaries. CI job `rag-eval` reruns it with the
semantic embedder on every API change and fails if hybrid + parent/child drops below
Recall@3 = 1.0 or MRR 0.9.

    uv run python -m rag.eval 3                                              # hash embedder
    RAG_EMBEDDER=fastembed uv run --group rag-local python -m rag.eval 3     # bge-small

## Results (k = 3, measured 2026-11-25)

Semantic embedder `fastembed:BAAI/bge-small-en-v1.5`:

| strategy | mode | Recall@3 | Precision@3 | MRR | NDCG@3 |
|---|---|---|---|---|---|
| fixed-400 | vector | 1.0 | 0.333 | 0.977 | 0.983 |
| fixed-400 | text | 0.955 | 0.318 | 0.909 | 0.921 |
| fixed-400 | hybrid | 1.0 | 0.333 | 0.97 | 0.977 |
| fixed-1000 | vector | 1.0 | 0.333 | 0.955 | 0.966 |
| fixed-1000 | text | 1.0 | 0.333 | 0.977 | 0.983 |
| fixed-1000 | hybrid | 1.0 | 0.333 | 1.0 | 1.0 |
| parent-2000/child-400 | vector | 1.0 | 0.333 | 1.0 | 1.0 |
| parent-2000/child-400 | text | 1.0 | 0.333 | 0.977 | 0.983 |
| parent-2000/child-400 | hybrid | 1.0 | 0.333 | 1.0 | 1.0 |

Lexical `hash` embedder (pipeline check, not semantic):

| strategy | mode | Recall@3 | Precision@3 | MRR | NDCG@3 |
|---|---|---|---|---|---|
| fixed-400 | vector | 0.864 | 0.288 | 0.727 | 0.763 |
| fixed-400 | text | 0.955 | 0.318 | 0.909 | 0.921 |
| fixed-400 | hybrid | 1.0 | 0.333 | 0.894 | 0.921 |
| fixed-1000 | vector | 0.864 | 0.288 | 0.606 | 0.673 |
| fixed-1000 | text | 1.0 | 0.333 | 0.977 | 0.983 |
| fixed-1000 | hybrid | 1.0 | 0.333 | 0.795 | 0.849 |
| parent-2000/child-400 | vector | 1.0 | 0.333 | 0.78 | 0.837 |
| parent-2000/child-400 | text | 1.0 | 0.333 | 0.977 | 0.983 |
| parent-2000/child-400 | hybrid | 1.0 | 0.333 | 0.955 | 0.966 |

## What this does and doesn't show

- Default chosen: **hybrid retrieval with parent-2000/child-400 chunking**: best or tied-best in
  both embedders; hybrid is the most robust when one signal is weak (see the hash embedder).
- Precision@3 is capped at 0.333 because each question has one relevant passage.
- The dataset is small and easy: documents are shorter than one parent (parents are often whole
  documents), and the semantic embedder saturates. It catches regressions; it cannot rank close
  alternatives. A larger, harder set (long policies, near-duplicate sections, multi-hop
  questions) is planned, as is a model-graded faithfulness judge. `citation_support` is a
  lexical proxy only.
- Not measured: the deployed embedding model (gateway/Bedrock) and the reranker.
