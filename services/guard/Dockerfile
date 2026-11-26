# syntax=docker/dockerfile:1.7
# Voyagent guard: prompt-injection classifier. The model is downloaded at build time from a pinned
# Hugging Face revision and verified by SHA-256; the running container needs no network access.

FROM python:3.13-slim AS model
ARG MODEL_REPO=protectai/deberta-v3-base-prompt-injection-v2
ARG MODEL_REV=90c9989b1a342275dd0d1a95aad283c04e075671
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /models/prompt-injection
COPY model.sha256 ./
RUN set -eu; base="https://huggingface.co/${MODEL_REPO}/resolve/${MODEL_REV}/onnx"; \
    for f in model.onnx tokenizer.json config.json; do curl -fsSL --retry 3 -o "$f" "$base/$f"; done; \
    sha256sum -c model.sha256

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
COPY app.py ./

FROM python:3.13-slim AS runtime
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
COPY --from=model --chown=app:app /models /models
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8002 \
    GUARD_MODEL_DIR=/models/prompt-injection
USER app
EXPOSE 8002
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)"
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
