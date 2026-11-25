# Observability

Status: **planned** (Phase 7). Today the API logs via `print()` (SECURITY.md F4).

## Pipeline

```
web (OTel browser SDK: traceparent on fetch)
api / orchestrator / rag / workers (OTel Python SDK, auto-instrument FastAPI, httpx, psycopg, redis)
model-gateway (LiteLLM OTel + Langfuse callback)
        │ OTLP
        ▼
Grafana Alloy ──► Prometheus (metrics) · Loki (logs) · Tempo (traces) ──► Grafana
            └──► CloudWatch (AWS-native metrics/alarms, ECS/ALB/RDS)
Langfuse: LLM traces, prompt versions, evaluation scores
```

One `trace_id` spans browser → api → orchestrator → each agent node → each tool/model call.

## Logging rules
JSON to stdout; fields `ts, level, service, trace_id, span_id, tenant_id, user_id(hash), event`.
Prompts, responses, documents and keys are **not** logged by default; a redaction processor
strips secret patterns and PII as defence in depth.

## KPIs

| Group | Metrics |
|---|---|
| HTTP (RED) | request rate, error rate, latency p50/p95/p99 per route, concurrent SSE streams |
| AI | input/output tokens, cost per request/user/tenant, model latency, model error rate, fallback rate, cache hit ratio |
| Agents | node duration, steps per run, tool success rate, HITL wait time, guardrail block rate |
| RAG | retrieval latency, Recall@K/MRR/NDCG (offline eval), faithfulness, embedding latency/cost |
| Infra | CPU/mem per task, DB connections, Redis memory, queue depth/age, DLQ size |

## Alerts (initial)
- 5xx rate > 2% for 5 min · p95 latency > SLO for 10 min
- Model fallback rate > 20% for 10 min · provider error spike
- Daily tenant spend > 80% of budget
- DLQ messages > 0 · oldest queue message age > 15 min
- DB connections > 80% of max

Dashboards (as code, in `infra/observability/dashboards/`): Service overview, AI cost & tokens,
Agent runs, RAG quality, Infrastructure.
