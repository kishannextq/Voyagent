# Microservices

v1 has six deployables. A capability becomes its own service only when it needs independent
scaling, deploy cadence, or a security boundary (ARCHITECTURE.md D2/D3).

| Service | Status | Responsibility | Owns data | Calls |
|---|---|---|---|---|
| `web` | exists (`apps/web`) | Next.js UI, SSR, Cognito session | none | api |
| `api` | **done** (`services/api`, `server.py`) | BFF: authn/z, tenancy, validation, rate limits, SSE relay | `app.threads` (ownership) | orchestrator, rag |
| `orchestrator` | **done** (`orchestrator_app.py`, same image as api) | LangGraph agents, tool execution, memory, HITL | checkpoints (library tables), `app.user_memory` | model-gateway, rag, tools |
| `model-gateway` | **done** (`services/model-gateway`) | LiteLLM proxy: routing, fallback, keys, cache, spend | own database `litellm`: keys, budgets, spend logs | Groq (Bedrock configured, not enabled) |
| `rag` | **done as a library** (`services/api/rag/`, used by api + orchestrator) | Retrieval, reranking, citations | `rag` schema: documents, parents, chunks | model-gateway (embeddings) |
| `ingest-worker` | **done** (`ingest_worker.py`, same image) | Async document pipeline from a Redis queue (SQS planned) | writes `rag` schema | model-gateway |

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

## model-gateway details

- **Image**: `ghcr.io/berriai/litellm` **v1.108.5 (LTS)** pinned by digest
  (`sha256:008a5916…`), config baked in (`services/model-gateway/Dockerfile`). Chosen as the newest
  release at least two weeks old. Context: litellm 1.82.7/1.82.8 on PyPI were compromised in
  March 2026 (BerriAI issue #24518); Docker images were reported unaffected. Known advisory after
  1.108.5: GHSA-2rwg-2wh4-7c4f (HIGH) only affects `azure/` models, which this config does not
  use; upgrade to ≥1.108.6 before adding Azure. GHSA-w5h5-479w-p5hx (JWT/SSO email fallback) does
  not apply: the gateway's admin UI/SSO is not used and the gateway is not exposed publicly.
- **Aliases**: `voyagent-fast` (guardrail, routing) and `voyagent-smart` (specialists, itinerary,
  final plan), each with a backup deployment in `router_settings.fallbacks`; `num_retries: 2`,
  per-deployment timeouts, cooldown after 3 failures. The API client never retries (`max_retries=0`).
- **Identity**: the API sends `user = "<tenant>:<sub>"` on every call; LiteLLM records spend per end
  user and enforces `max_end_user_budget_id` (budget created by `bootstrap.py`). The API uses one
  virtual key limited to the two aliases, with its own monthly budget and RPM limit.
- **Cache**: identical requests are served from Redis for 10 minutes.
- **Not exposed publicly**: reachable only from the api/orchestrator security group in AWS.
- **Verification**: CI `gateway` job runs the pinned image with Postgres and `tools/mock-llm`
  (fallback, alias routing, unknown keys refused, virtual-key model scoping, end-user spend and
  budget enforcement). Not verified: real Groq traffic through the gateway, Bedrock.

## orchestrator details

- One image, two deployables: `uvicorn server:api` (public api) and `uvicorn orchestrator_app:app`
  (internal). Splitting the image later is cheap; sharing it now avoids duplicated dependencies.
- Internal API (`/internal/v1/...`): modes, thread snapshot, start/resume run (SSE), delete thread,
  get/delete memory. Every call needs `X-Internal-Token` (constant-time compare; required outside
  local/test) and carries `X-Request-ID`. No OpenAPI/docs endpoints.
- The api authenticates the user, checks ownership and rate limits, then relays the orchestrator's
  SSE bytes unchanged. Orchestrator full → 503 to the client before any bytes; unreachable → 502.
- Agent behaviour is configured in `agents/*.md` (see `agent_config.py`).
