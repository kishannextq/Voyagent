# syntax=docker/dockerfile:1.7
# Voyagent API — FastAPI + LangGraph. Multi-stage: build venv with uv, run as non-root.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY *.py alembic.ini ./
COPY migrations ./migrations
COPY agents ./agents
COPY rag ./rag

FROM python:3.13-slim AS runtime
# OS security updates; drop pip (not needed at runtime; its vendored libs carry CVEs).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
# FORWARDED_ALLOW_IPS: trust X-Forwarded-For only from these proxies (read by uvicorn). Deployments
# set the load balancer subnets; never '*', which lets any client spoof its IP and dodge rate limits.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 \
    FORWARDED_ALLOW_IPS=127.0.0.1
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/v1/health', timeout=2)"
CMD ["sh", "-c", "exec uvicorn server:api --host 0.0.0.0 --port ${PORT} --proxy-headers"]
