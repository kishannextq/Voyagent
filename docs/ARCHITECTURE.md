# Architecture

Status legend: **[done]** implemented and tested · **[planned]** designed, not built.

## 1. Context

Voyagent is a multi-agent travel planner: a LangGraph supervisor routes a request to specialist
agents (flights, hotels, weather, budget, itinerary), pauses for human approval, then produces a
final plan, streaming progress to the browser over Server-Sent Events.

This repository evolves that application into a multi-tenant, observable, AWS-hosted AI platform.
Voyagent remains the product; the platform capabilities (auth, model gateway, RAG, guardrails,
observability) are built around it, not as a separate generic product.

## 2. Current state (Phase 1 baseline)

```
Browser ──HTTPS+SSE──► apps/web (Next.js 14)
                          │  fetch + SSE parser (lib/api.ts)
                          ▼
                    services/api (FastAPI, single process)
                      ├─ LangGraph graph (8 nodes, human-in-the-loop interrupt)
                      ├─ Groq LLM (langchain-groq)               [mock mode available]
                      ├─ MCP tools: AviationStack (stdio), Weather (stdio), Tavily (HTTP)
                      └─ Checkpointer: Postgres if DATABASE_URL else in-memory
```

Known gaps are tracked in [SECURITY.md](SECURITY.md#findings-register) and
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## 3. Target architecture (v1)

```
Browser
  │
  ▼
CloudFront + AWS WAF ── static assets cached at edge
  │
  ▼
Application Load Balancer (TLS, path routing)
  ├── /*        → web           (Next.js SSR, Cognito Hosted UI session, httpOnly cookies)
  └── /api/*    → api           (FastAPI BFF: JWT validation, RBAC, tenant scoping,
                                 rate limits, input guardrails, SSE fan-out)
                    │ HTTP (internal, mTLS-ready, trace-propagated)
        ┌───────────┼─────────────────────────┐
        ▼           ▼                         ▼
   orchestrator   rag                    (SQS / EventBridge)
   LangGraph      retrieve → rerank            │
   agents, tool   → compress → cite            ▼
   sandbox, PG    (pgvector, hybrid)     ingest-worker
   checkpointer         │                S3 → validate/scan → extract
        │               │                → chunk → embed → index
        └──────┬────────┘
               ▼
        model-gateway (LiteLLM proxy)
        Bedrock · Groq · HF endpoints · OpenAI-compatible
        routing, fallback, virtual keys, Redis cache, spend logs
               │
        Bedrock Guardrails (input/output) · Langfuse (LLM traces)

Data: Aurora PostgreSQL (schema + role per service) · ElastiCache Redis · S3 · KMS · Secrets Manager
Telemetry: OpenTelemetry SDK → Grafana Alloy → Prometheus / Loki / Tempo → Grafana; CloudWatch for AWS-native
```

### Request lifecycle (chat/plan request)

1. Browser → CloudFront/WAF (bot, rate, OWASP rules) → ALB.
2. `api` validates the Cognito JWT (JWKS cached), resolves `tenant_id`, `user_id`, roles.
3. Pydantic request validation, per-user/tenant rate limit (Redis token bucket).
4. Input guardrails: deterministic rules (injection patterns, PII) + Bedrock Guardrails.
5. `api` calls `orchestrator` and relays its event stream as SSE to the browser.
6. `orchestrator` runs the LangGraph graph; agents call tools (allow-listed, sandboxed) and
   `rag`; every LLM call goes through `model-gateway` with a per-tenant virtual key.
7. Output guardrails (secret/PII redaction, Bedrock Guardrails) on each user-visible chunk.
8. Usage (tokens, cost, latency) is attributed to tenant/user via gateway spend logs + Langfuse.

## 4. Key decisions

| # | Decision | Rationale | Alternatives rejected |
|---|---|---|---|
| D1 | Monorepo on `integration/platform` | Feature branches must share code to integrate; separate per-branch trees could not | One repo per branch (current state) |
| D2 | ~6 deployable services in v1, not 23 | Team/codebase size; avoid a distributed monolith. Split further only for independent scaling, deploy cadence or security boundary | Service per capability |
| D3 | Guardrails, prompts, usage accounting as libraries first | Same latency/scale profile as callers; no network hop on the hot path | Dedicated services from day one |
| D4 | No API Gateway on the streaming path | HTTP APIs cap integration time at 30 s and REST APIs default to 29 s; agent runs stream for longer. ALB supports long-lived SSE | API Gateway → ECS |
| D5 | No Nginx | ALB already does TLS, routing, health checks on Fargate | Nginx sidecar |
| D6 | ECS Fargate, not Kubernetes | Fewer moving parts; no cluster ops; sufficient autoscaling | EKS |
| D7 | SQS + EventBridge + Step Functions; no Kafka/RabbitMQ | No workload yet requires log replay or broker routing semantics | Kafka, RabbitMQ |
| D8 | pgvector first; Superlinked evaluated against a golden set | Postgres already required; one fewer datastore. Decision revisited with retrieval metrics | Dedicated vector DB up front |
| D9 | No DynamoDB in v1 | Postgres + Redis cover current access patterns | DynamoDB for sessions/state |
| D10 | Server-side provider keys only; "bring your own keys" removed | Keys must never transit or be stored in the browser; per-tenant control via LiteLLM virtual keys | Optional BYO keys |
| D11 | LiteLLM as the model gateway (self-hosted proxy) | Provider-agnostic API, fallback, budgets, spend tracking out of the box | Custom gateway |

## 5. Service map

See [MICROSERVICES.md](MICROSERVICES.md). Data ownership: [DATABASE_DESIGN.md](DATABASE_DESIGN.md).
