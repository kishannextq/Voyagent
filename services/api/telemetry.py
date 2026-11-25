"""OpenTelemetry tracing setup (traces go to an OTLP collector when configured).

Enabled when OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. http://otel-collector:4318). Then:
- incoming requests get server spans (FastAPI), outgoing httpx calls get client spans and carry
  W3C `traceparent`, so api -> orchestrator -> model gateway is one trace;
- code adds spans for plan runs, agents, model calls, tool calls and document search;
- JSON log lines include trace_id/span_id (logging_setup) for log <-> trace correlation.
Without the endpoint everything is a no-op (the global tracer provider is the default one).
Span attributes never include user text, prompts, outputs or identifiers of people.
"""

import logging
import os

from opentelemetry import trace

log = logging.getLogger("voyagent.telemetry")

tracer = trace.get_tracer("voyagent")
_configured = False


def setup(service: str, app=None) -> bool:
    global _configured
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False
    if not _configured:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": os.getenv("OTEL_SERVICE_NAME", service),
                "service.namespace": "voyagent",
                "deployment.environment": os.getenv("APP_ENV", "local"),
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # endpoint from OTEL_* env
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _configured = True
        log.info("tracing_enabled", extra={"endpoint": endpoint})
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    return True


def current_ids() -> tuple[str, str] | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
