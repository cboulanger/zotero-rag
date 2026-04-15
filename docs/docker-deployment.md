# Docker Deployment Plan

## Goal

Package the FastAPI backend as a Docker image and provide a Node.js CLI for building, pushing, and running the container — following the same pattern as `pdf-tei-editor`.

---

## Files to Create

| File | Purpose |
|------|---------|
| `Dockerfile` | Multi-stage image build |
| `bin/container.js` | CLI: `build`, `push`, `start`, `stop`, `restart`, `deploy` |
| `bin/deploy.js` | Deployment wrapper that reads `.env.deploy.*` files |
| `.env.deploy.example` | Example deployment environment file |

Update `package.json` to add `commander`, `dotenv` dependencies and `container`/`deploy` scripts.

---

## Dockerfile Design

Multi-stage build using the official `uv` Docker image:

```dockerfile
# Stage 1: dependency installer
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

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

EXPOSE 8119
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8119"]
```

### Zotero Connectivity

The container needs to reach Zotero running on the host. This requires:

- **Linux:** `--add-host=host.docker.internal:host-gateway` (passed by CLI)
- **macOS/Windows:** `host.docker.internal` resolves automatically

Default `ZOTERO_API_URL=http://host.docker.internal:23119` (set by CLI `--start`).

---

## CLI Design (`bin/container.js`)

CommonJS Node.js CLI using `commander`. Adapted from `pdf-tei-editor/bin/container.js`.

```
APP_NAME = 'zotero-rag'
REGISTRY  = 'docker.io/cboulanger/zotero-rag'
PORT      = 8119
```

### Commands

| Command | Description |
|---------|-------------|
| `build [--tag TAG]` | Build Docker image |
| `push [--tag TAG]` | Push image to registry |
| `start [options]` | Run container |
| `stop` | Stop and remove container |
| `restart [options]` | Stop + start |
| `deploy [options]` | Build + push + start |

### `start` options

| Option | Default | Description |
|--------|---------|-------------|
| `--tag TAG` | `latest` | Image tag |
| `--data-dir DIR` | `./data` | Host path mounted at `/data` |
| `--port PORT` | `8119` | Host port |
| `--zotero-host URL` | `http://host.docker.internal:23119` | Zotero API URL passed as env var |
| `--env KEY=VAL` | — | Extra env vars (repeatable) |
| `--pull` | false | Pull image before starting |

### Platform handling

- Detects `docker` or `podman` (prefers docker)
- On Linux: adds `--add-host=host.docker.internal:host-gateway`
- Volumes: `--volume <data-dir>:/data`

---

## Deploy Wrapper (`bin/deploy.js`)

Reads a `.env.deploy.*` file (default `.env.deploy`) and maps entries to CLI flags:

| Env variable | CLI flag |
|-------------|---------|
| `DEPLOY_TAG` | `--tag` |
| `DEPLOY_DATA_DIR` | `--data-dir` |
| `DEPLOY_PORT` | `--port` |
| `DEPLOY_ZOTERO_HOST` | `--zotero-host` |
| `DEPLOY_PULL` | `--pull` (boolean) |
| Everything else | `--env KEY=VALUE` |

Calls `node bin/container.js deploy [flags]`.

Usage:
```bash
node bin/deploy.js                         # uses .env.deploy
node bin/deploy.js .env.deploy.myserver    # uses named file
```

---

## Example Deployment Env File (`.env.deploy.example`)

```bash
# Deployment target configuration
DEPLOY_TAG=latest
DEPLOY_DATA_DIR=/srv/zotero-rag/data
DEPLOY_PORT=8119
DEPLOY_ZOTERO_HOST=http://host.docker.internal:23119
DEPLOY_PULL=false

# Container environment
MODEL_PRESET=remote-openai
OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# KISSKI_API_KEY=...
LOG_LEVEL=INFO
```

---

## `package.json` changes

Add to `dependencies`:
- `commander`: `^12.0.0`
- `dotenv`: `^16.0.0`

Add to `scripts`:
- `"container": "node bin/container.js"`
- `"deploy": "node bin/deploy.js"`

---

## Implementation Notes

- `pyproject.toml` is at the **repo root** (not in `backend/`), so `uv sync` works from `/app`
- Kreuzberg requires `pandoc`, `tesseract` for full functionality — these should be installed in the image for OCR/DOCX support
- The image should **not** bundle model weights; they are downloaded to `/data/models` on first use
- For GPU support a separate `Dockerfile.gpu` extending the base image with CUDA is a future option
