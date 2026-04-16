# Docker Deployment Plan

## Goal

Package the FastAPI backend as a Docker image and provide a Node.js CLI for building, pushing, and running the container — following the same pattern as `pdf-tei-editor`.

---

## Files to Create

| File | Purpose |
| ------ | --------- |
| `Dockerfile` | Multi-stage image build |
| `bin/container.mjs` | CLI: `build`, `push`, `start`, `stop`, `restart`, `logs`, `deploy` |
| `bin/deploy.mjs` | Reads a `.env.deploy.*` file and delegates to `container.mjs deploy` |
| `.env.deploy.example` | Example deployment environment file |

Update `package.json` to add `commander`, `dotenv` dependencies and `container`/`deploy` scripts.

> **Note on module format:** Both `bin/` files use ESM (`import`/`export`). Because `package.json` sets `"type": "commonjs"`, the `.mjs` extension is used to opt into ESM without changing the root module type (which would break `zotero-plugin.config.js`).

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

**Local mode** (Zotero on the host machine): The container needs to reach Zotero running on the host.

- **Linux:** `--add-host=host.docker.internal:host-gateway` (added automatically by CLI on Linux)
- **macOS/Windows:** `host.docker.internal` resolves automatically

Default `ZOTERO_API_URL=http://host.docker.internal:23119` (passed automatically by `start`).

**Remote mode** (plugin uploads documents): Set `REQUIRE_ZOTERO=false` to skip the Zotero connectivity check at startup.

---

## CLI Design (`bin/container.mjs`)

ESM Node.js CLI using `commander`. Pattern adapted from `pdf-tei-editor/bin/container.js`.

```js
APP_NAME  = 'zotero-rag'
REGISTRY  = 'docker.io/cboulanger/zotero-rag'
PORT      = 8119
```

### Commands

| Command | Description |
| ------- | ----------- |
| `build [--tag] [--no-cache] [--yes]` | Build Docker image locally |
| `push [--tag] [--no-build] [--no-cache] [--yes]` | Tag + push to registry (reads `DOCKER_HUB_USERNAME`/`DOCKER_HUB_TOKEN` from `.env`) |
| `start [options]` | Run container; auto-detects local → registry image; pulls if needed |
| `stop [--name] [--all] [--remove]` | Stop and optionally remove container |
| `restart [options]` | Stop + start |
| `logs [--name] [-f] [--tail N]` | Stream container logs |
| `deploy [options]` | Pull/rebuild → start → nginx config → SSL cert (Linux only for nginx/SSL) |

### `start` options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--tag TAG` | `latest` | Image tag |
| `--name NAME` | `zotero-rag-<tag>` | Container name |
| `--port PORT` | `8119` | Host port |
| `--data-dir DIR` | — | Host path mounted at `/data`; sets `VECTOR_DB_PATH` and `MODEL_WEIGHTS_PATH` |
| `--zotero-host URL` | `http://host.docker.internal:23119` | Passed as `ZOTERO_API_URL` |
| `--env KEY[=VAL]` | — | Extra env vars (repeatable); `KEY` alone transfers from host |
| `--volume HOST:CTR` | — | Extra volume mounts (repeatable) |
| `--restart POLICY` | — | Docker restart policy |
| `--no-detach` | — | Run in foreground |

### Platform handling

- Detects `docker` or `podman` (prefers docker)
- On **Linux**: automatically adds `--add-host=host.docker.internal:host-gateway`

### `deploy` options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--fqdn FQDN` | *(required)* | Domain name; triggers nginx + SSL setup |
| `--tag TAG` | `latest` | Image tag |
| `--port PORT` | `8119` | Host port |
| `--data-dir DIR` | — | Persistent data directory |
| `--env KEY[=VAL]` | — | Container env vars (repeatable) |
| `--pull` | false | Pull image from registry before deploying |
| `--rebuild` | false | Rebuild image locally before deploying |
| `--no-cache` | — | Disable layer cache (use with `--rebuild`) |
| `--no-nginx` | — | Skip nginx configuration |
| `--no-ssl` | — | Skip SSL certificate setup |
| `--email EMAIL` | `admin@<fqdn>` | Email for certbot |
| `--yes` | — | Skip confirmation prompt |

`deploy` requires `sudo` when nginx/SSL setup is requested (same constraint as pdf-tei-editor).

### nginx template

Proxies to `http://127.0.0.1:8119` with:

- `client_max_body_size 100M`
- 300 s proxy timeouts
- `proxy_buffering off` for SSE endpoints

---

## Deploy Wrapper (`bin/deploy.mjs`)

Reads a `.env.deploy.*` file (**required** positional argument) and delegates to `container.mjs deploy`.

```bash
node bin/deploy.mjs .env.deploy.myserver
```

### Env-file to CLI flag mapping

| Env variable | CLI flag |
| ----------- | -------- |
| `DEPLOY_FQDN` | `--fqdn` |
| `DEPLOY_TAG` | `--tag` |
| `DEPLOY_DATA_DIR` | `--data-dir` |
| `DEPLOY_PORT` | `--port` |
| `DEPLOY_PULL` | `--pull` (boolean: `1/true/on` → present, `0/false/off` → omitted) |
| `DEPLOY_SSL=false` | `--no-ssl` |
| `DEPLOY_NGINX=false` | `--no-nginx` |
| Everything else | `--env KEY` (value loaded from env via `dotenv.config`) |

If no `DEPLOY_FQDN` is set, or it is `localhost`/`127.0.0.1`, the script automatically appends `--no-nginx --no-ssl`.

---

## Example Deployment Env File (`.env.deploy.example`)

```bash
# Deployment target
DEPLOY_FQDN=rag.example.com
DEPLOY_TAG=latest
DEPLOY_DATA_DIR=/srv/zotero-rag/data
DEPLOY_PORT=8119
DEPLOY_PULL=true
# DEPLOY_SSL=false    # uncomment to skip SSL

# Container environment variables
MODEL_PRESET=remote-openai
OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
LOG_LEVEL=INFO

# Remote server mode (plugin uploads documents; no local Zotero needed)
# REQUIRE_ZOTERO=false
# API_KEY=your-secret-key          # Require X-API-Key header when set
# ALLOWED_ORIGINS=https://myhost   # Restrict CORS origins (default: *)
```

---

## `package.json` changes

Add to `dependencies`:

- `commander`: `^12.0.0`
- `dotenv`: `^16.0.0`

Add to `scripts`:

- `"container": "node bin/container.mjs"`
- `"deploy": "node bin/deploy.mjs"`

---

## Implementation Notes

- `pyproject.toml` is at the **repo root** (not in `backend/`), so `uv sync` works from `/app`
- Kreuzberg requires `pandoc`, `tesseract` for full functionality — install in the image for OCR/DOCX support
- The image should **not** bundle model weights; they are downloaded to `/data/models` on first use
- For GPU support a separate `Dockerfile.gpu` extending the base image with CUDA is a future option
- The `push` command reads `DOCKER_HUB_USERNAME` and `DOCKER_HUB_TOKEN` from `.env` (same pattern as pdf-tei-editor)
