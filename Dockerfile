# Stage 1: dependency installer
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# INSTALL_LOCAL_MODELS=false → remote presets only (default, smaller image, ~1-2 GB less)
# INSTALL_LOCAL_MODELS=true  → also installs sentence-transformers/torch for local presets
ARG INSTALL_LOCAL_MODELS=false

# kreuzberg (Rust/maturin) has no pre-built manylinux wheel and compiles from
# source on all Linux targets — requires a C toolchain, cmake, and OpenSSL headers.
# These are builder-only; they are NOT copied into the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config libssl-dev cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then \
      uv sync --frozen --no-dev --no-install-project --extra local-models; \
    else \
      uv sync --frozen --no-dev --no-install-project; \
    fi

# Stage 2: runtime
FROM python:3.12-slim-bookworm AS runtime

# INSTALL_OCR=true  → include Tesseract (needed for OCR on image-only PDFs)
# INSTALL_OCR=false → skip Tesseract; set OCR_ENABLED=false in container env
ARG INSTALL_OCR=true
RUN if [ "$INSTALL_OCR" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
      && rm -rf /var/lib/apt/lists/*; \
    fi

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

EXPOSE 8119
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8119"]
