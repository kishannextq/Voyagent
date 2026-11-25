"""Structured JSON logging with secret redaction and request correlation.

Rules (docs/OBSERVABILITY.md): one JSON object per line on stdout; prompts, model
output, tool data and user text are never logged, only metadata (agent, sizes,
durations, outcomes). A redaction filter strips known secret formats and the
server's configured provider keys as defence in depth.
"""

import json
import logging
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Common credential formats. Defence in depth: code must not log secrets in the first place.
_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{16,}"),  # Groq
    re.compile(r"tvly-[A-Za-z0-9-]{16,}"),  # Tavily
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI-style
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|apikey|access[_-]?key|token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]{6,}"
    ),
]

# LogRecord attributes that are not user-supplied `extra` fields.
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    try:  # configured provider keys, whatever their format
        import keys

        text = keys.redact(text, keys.ApiKeys())
    except Exception:  # noqa: S110 - redaction must never break logging
        pass
    return text


def _trace_ids():
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        return None
    ctx = trace.get_current_span().get_span_context()
    return (format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")) if ctx.is_valid else None


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            entry["request_id"] = request_id
        ids = _trace_ids()
        if ids:
            entry["trace_id"], entry["span_id"] = ids
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            entry["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(entry, default=str, ensure_ascii=False))


def configure(service: str = "api") -> None:
    """Idempotent root logger setup. LOG_LEVEL env var, default INFO."""
    root = logging.getLogger()
    if any(getattr(h, "_voyagent", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    handler._voyagent = True  # type: ignore[attr-defined]
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Library loggers that would otherwise log request URLs (which may carry keys) at INFO.
    for noisy in ("httpx", "httpcore", "mcp", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn: route through our handler, drop its own access log (we emit one).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
