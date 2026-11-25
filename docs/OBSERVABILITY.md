# Observability

Status per item: **[done]** implemented and verified (how is stated), **[planned]** not yet.

## Pipeline

```
api / orchestrator / ingest-worker
  ├─ traces  (OpenTelemetry SDK: FastAPI server spans, httpx client spans, custom spans) ── OTLP ──┐
  ├─ metrics (prometheus_client on METRICS_PORT 9464, internal only) ◄── scraped by Prometheus     │
  └─ logs    (JSON to stdout, with request_id + trace_id/span_id) ── container logs ──┐            │
                                                                                     ▼            ▼
                                                       OpenTelemetry Collector (filelog + otlp)
                                                             │ logs → Loki      │ traces → Tempo
                                                             ▼                  ▼
                                    Grafana (Prometheus · Loki · Tempo; trace ↔ log links)
```

- **[done]** One trace spans api → orchestrator (W3C `traceparent` injected on every internal
  call) with spans for the plan run, each agent, live model calls, tool calls and document search.
  Verified in CI: Tempo returns a trace containing spans from both services.
- **[done]** Log lines carry `trace_id`/`span_id`; Grafana's Loki datasource links them to Tempo.
- **[done]** Span attributes and metric labels never contain user text, prompts, outputs, user,
  tenant or thread identifiers (tests assert this).
- **[planned]** Browser spans (web → api); model gateway spans (LiteLLM `callbacks: ["otel"]`);
  Langfuse for prompt/response-level LLM tracing (needs its own storage; would hold user content,
  so it needs a retention and access decision first); CloudWatch for AWS-native metrics (Phase 8).

Enable locally: `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` in `.env`, then
`docker compose --profile observability up` → Grafana http://localhost:3001 (admin /
`GRAFANA_ADMIN_PASSWORD`). Container log shipping reads Docker's log files, which works on Linux
and in CI; on Docker Desktop they live inside its VM.

## Metrics [done]

| Metric | Labels | Meaning |
|---|---|---|
| `voyagent_http_requests_total`, `voyagent_http_request_duration_seconds` | service, method, route, status | RED per route (route templates, not raw paths) |
| `voyagent_runs_total`, `voyagent_run_duration_seconds`, `voyagent_runs_active` | kind (start/resume), outcome (completed/interrupted/error/timeout) | plan runs |
| `voyagent_busy_rejections_total` | | 503s because an orchestrator was full |
| `voyagent_agent_duration_seconds` | agent | per graph node |
| `voyagent_budget_exceeded_total` | scope | agent/run LLM-call budget violations |
| `voyagent_llm_calls_total`, `voyagent_llm_call_duration_seconds` | role (fast/smart), mode (live/mock/fallback) | model calls |
| `voyagent_tool_calls_total` | service, outcome (live/mock/fallback/denied) | tool calls |
| `voyagent_guardrail_events_total` | stage, rule | input blocked/redacted, output withheld/cleaned |
| `voyagent_rate_limited_total` | scope | 429s |
| `voyagent_rag_searches_total`, `voyagent_rag_ingest_total` | outcome / status | document search and ingestion |

Not yet: tokens and cost per request/tenant (the gateway records spend per end user; exporting
it needs LiteLLM's Prometheus integration), DB pool and Redis metrics, queue depth.

## Alerts [done: rules; planned: routing]

`infra/observability/prometheus/alerts.yml` (validated with `promtool` in CI, loaded by
Prometheus): target down · API 5xx > 5% (5 min) · plan failures/timeouts > 10% · p95 plan
> 120 s · model fallbacks · orchestrator at capacity (503s) · **system-prompt canary leak
(critical)** · prompt-injection spike · ingestion failures. Routing (Alertmanager / SNS /
PagerDuty) is planned with the AWS environment.

## Dashboards [done]

`infra/observability/grafana/dashboards/voyagent-overview.json` (provisioned): API rate and p95,
plan runs by outcome, active runs, run and agent p95, model calls by mode and p95, tool calls,
guardrail events, 429/503, document search/ingestion, and recent errors from Loki.

## Logging rules [done]

JSON to stdout: `ts, level, service, logger, event, request_id, trace_id, span_id` + event
fields. Prompts, responses, documents and keys are not logged; a redaction filter strips secret
patterns as defence in depth (`logging_setup.py`).
