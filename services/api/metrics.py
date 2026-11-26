"""Prometheus metrics (names and labels are part of the dashboards/alerts contract).

Served on a separate port (METRICS_PORT, e.g. 9464), never on the public API port.
Labels are low-cardinality only: no user, tenant, thread or free-text values.
"""

import os

from prometheus_client import Counter, Gauge, Histogram, start_http_server

_LAT = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

http_requests = Counter("voyagent_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
http_duration = Histogram("voyagent_http_request_duration_seconds", "HTTP request duration", ["service", "route"], buckets=_LAT)

runs = Counter("voyagent_runs_total", "Plan runs by outcome", ["kind", "outcome"])  # completed|interrupted|error|timeout
runs_active = Gauge("voyagent_runs_active", "Plan runs in progress")
run_duration = Histogram("voyagent_run_duration_seconds", "Plan run duration", ["kind"], buckets=_LAT)
busy_rejections = Counter("voyagent_busy_rejections_total", "Runs refused because the orchestrator was full")

agent_duration = Histogram("voyagent_agent_duration_seconds", "Agent (graph node) duration", ["agent"], buckets=_LAT)
budget_exceeded = Counter("voyagent_budget_exceeded_total", "Agent/run budget violations", ["scope"])
llm_calls = Counter("voyagent_llm_calls_total", "Model calls", ["role", "mode"])  # live|mock|fallback
llm_duration = Histogram("voyagent_llm_call_duration_seconds", "Live model call duration", ["role"], buckets=_LAT)
tool_calls = Counter("voyagent_tool_calls_total", "Tool calls", ["service", "outcome"])  # live|mock|fallback|denied

guardrail_events = Counter("voyagent_guardrail_events_total", "Guardrail decisions", ["stage", "rule"])
rate_limited = Counter("voyagent_rate_limited_total", "Requests refused by rate limits", ["scope"])
rag_searches = Counter("voyagent_rag_searches_total", "Document searches by agents", ["outcome"])  # hits|empty|error
rag_ingests = Counter("voyagent_rag_ingest_total", "Document ingestion results", ["status"])  # ready|failed

# Remote agents (agent_bus.py): orchestrator side
agent_remote_calls = Counter("voyagent_agent_remote_calls_total", "Remote agent calls", ["agent", "outcome"])  # ok|no_response|budget|timeout|expired|tool|error
agent_remote_duration = Histogram("voyagent_agent_remote_duration_seconds", "Remote agent call, enqueue to result", ["agent"], buckets=_LAT)
agent_queue_wait = Histogram("voyagent_agent_queue_wait_seconds", "Time a task waited in its agent queue", ["agent"], buckets=_LAT)
# Agent workers (agent_worker.py)
agent_tasks = Counter("voyagent_agent_tasks_total", "Tasks handled by an agent worker", ["agent", "outcome"])  # ok|error kinds|duplicate|invalid|released
agent_inflight = Gauge("voyagent_agent_tasks_inflight", "Tasks running in this agent worker", ["agent"])

_started = False


def start_server() -> None:
    """Expose /metrics on METRICS_PORT (if set). Idempotent."""
    global _started
    port = os.getenv("METRICS_PORT", "").strip()
    if port and not _started:
        start_http_server(int(port), addr=os.getenv("METRICS_ADDR", "0.0.0.0"))  # noqa: S104 - internal port
        _started = True
