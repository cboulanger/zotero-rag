# Stage 1: dependency installer
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# INSTALL_LOCAL_MODELS=false → remote presets only (default, smaller image, ~1-2 GB less)
# INSTALL_LOCAL_MODELS=true  → also installs sentence-transformers/torch for local presets
ARG INSTALL_LOCAL_MODELS=false

WORKDIR /app
COPY pyproject.toml uv.lock ./
ENV UV_HTTP_TIMEOUT=120
RUN if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then \
      uv sync --frozen --no-dev --no-install-project --extra local-models; \
    else \
      uv sync --frozen --no-dev --no-install-project; \
    fi
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# Stage 2: runtime
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY backend/ ./backend/
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app

# Data directory for persistent storage
VOLUME /data
ENV VECTOR_DB_PATH=/data/qdrant
ENV MODEL_WEIGHTS_PATH=/data/models
ENV LOG_FILE=/data/logs/server.log

# Kreuzberg sidecar URL (set by docker-compose or container.mjs)
ENV KREUZBERG_URL=http://kreuzberg:8100

# 1 worker: the service is I/O-bound (kreuzberg, Qdrant, OpenAI are external),
# so asyncio concurrency within one process is sufficient. Multiple workers would
# each have their own in-memory async-task store and break task polling (issue #30).
ENV UVICORN_WORKERS=1

EXPOSE 8119
CMD uvicorn backend.main:app --host 0.0.0.0 --port 8119 --workers ${UVICORN_WORKERS}
