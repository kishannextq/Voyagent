# Microservices

v1 has six deployables. A capability becomes its own service only when it needs independent
scaling, deploy cadence, or a security boundary (ARCHITECTURE.md D2/D3).

| Service | Status | Responsibility | Owns data | Calls |
|---|---|---|---|---|
| `web` | exists (`apps/web`) | Next.js UI, SSR, Cognito session | none | api |
| `api` | exists (`services/api`), monolithic today | BFF: authn/z, tenancy, validation, rate limits, input rails, SSE relay | `app` schema: tenants, users, conversations, plans | orchestrator, rag |
| `orchestrator` | planned (extract from api) | LangGraph agents, tool execution, memory, HITL | `orchestrator` schema: checkpoints, long-term memory | model-gateway, rag, tools |
| `model-gateway` | planned | LiteLLM proxy: routing, fallback, keys, cache, spend | `litellm` schema: keys, budgets, spend logs | Bedrock, Groq, HF |
| `rag` | planned | Retrieval, reranking, compression, citations | `rag` schema: documents, chunks, embeddings | model-gateway (embeddings/rerank) |
| `ingest-worker` | planned | Async document pipeline from SQS | writes `rag` schema via rag-owned contract*, S3 objects | S3, model-gateway |

\* `ingest-worker` and `rag` are one bounded context (same schema owner, deployed separately
because their scaling profiles differ: bursty batch vs latency-sensitive reads).

Libraries (in-process, shared via a `packages/` workspace): `guardrails`, `prompts`, `telemetry`,
`authz`.

## Per-service standard (applies to every service)

| Concern | Standard |
|---|---|
| Health | `GET /health/live` (process up), `GET /health/ready` (deps reachable) |
| Contracts | OpenAPI generated from Pydantic; versioned `/v1`; breaking change → `/v2` |
| Auth | Edge: Cognito JWT at `api`. Internal: service-to-service JWT (short-lived, audience-bound); security groups restrict callers |
| Errors | RFC 9457 problem+json; no stack traces to clients; every error logged with trace ID |
| Retries | Only idempotent ops; exponential backoff + jitter; max 3 attempts; total deadline |
| Circuit breaking | Per downstream; open after consecutive failures; half-open probe |
| Timeouts | Every outbound call has a timeout; request deadline propagated |
| Telemetry | OTel traces + RED metrics + JSON logs with `trace_id`, `tenant_id` (never content/PII) |
| Container | Multi-stage, non-root, healthcheck, pinned base image, Trivy-clean |
| Tests | Unit + contract tests in CI; integration tests against compose |

## Current `api` (as imported)

| Item | Value |
|---|---|
| Entry | `services/api/server.py` → `uvicorn server:api` |
| Graph | `graph.py`: supervisor → selected specialists (fixed order) → itinerary → human_approval (interrupt) → final_response |
| Tools | `mcp_client.py`: AviationStack + Weather via stdio MCP subprocesses, Tavily via hosted MCP |
| State | `state.py` `TravelState`; checkpointed per `thread_id` |
| Tests | `tests/test_api_mock.py` |
