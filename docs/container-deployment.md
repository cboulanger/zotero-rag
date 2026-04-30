# Docker Deployment

## Architecture

The Zotero RAG backend runs as three containers:

| Container | Image | Purpose |
| --------- | ----- | ------- |
| `zotero-rag` | `cboulanger/zotero-rag` | FastAPI backend (port 8119) |
| `kreuzberg` | `ghcr.io/kreuzberg-dev/kreuzberg` | Document extraction sidecar (internal) |
| `qdrant` | `docker.io/qdrant/qdrant:v1.15` | Vector database sidecar (internal) |

All three containers communicate over a shared Docker bridge network. The
kreuzberg sidecar bundles Tesseract, Pandoc, and PDFium — no build-time
compilation is required in the main image. The Qdrant sidecar replaces the
embedded local file mode, enabling multiple uvicorn workers to share one
vector database server without file-lock contention.

---

## Quick Start (docker compose)

The simplest way to run both containers locally:

```bash
# Copy and edit the environment file
cp .env.dist .env
# Set MODEL_PRESET and the required API key

# Start both containers
docker compose up -d

# Follow logs
docker compose logs -f

# Stop
docker compose down
```

The `docker-compose.yml` at the repo root wires up the network, the kreuzberg
sidecar, and all environment variables automatically.

---

## Container Orchestration

Both `docker compose` and `bin/container.mjs` run the same three containers and wire them together in the same way; they just do it through different mechanisms.

### Shared network

All three containers must be able to reach each other by hostname. Both approaches create a dedicated Docker bridge network and attach all containers to it:

| Approach | Network name | How it is created |
| -------- | ------------ | ----------------- |
| `docker compose` | `internal` (project-scoped) | Declared in `docker-compose.yml`; created automatically |
| `bin/container.mjs` | `zotero-rag-net` | Created by `ensureNetwork()` before any container starts |

Each sidecar is given a fixed network alias so the backend can always reach it by hostname regardless of the container name:

| Sidecar | Alias | URL |
| ------- | ----- | --- |
| kreuzberg | `kreuzberg` | `http://kreuzberg:8100` |
| qdrant | `qdrant` | `http://qdrant:6333` |

### Sidecar lifecycle

**`docker compose`** manages all sidecars as regular services (`depends_on` ensures they start before `zotero-rag`).  
Stopping with `docker compose down` removes all containers and the network together.

**`bin/container.mjs`** manages sidecars explicitly:

- `start` / `deploy` (without `--systemd-service`) → starts kreuzberg and Qdrant sidecars if they are not already running, pulls images, and runs them with the correct `--network-alias`. Sidecars are named `<app-container>-kreuzberg` and `<app-container>-qdrant`. During `deploy`, already-running sidecars are left untouched by default; pass `--restart-sidecars` to force a stop-and-restart.
- `stop` → stops and removes both sidecars.
- `restart` → stops and restarts the app container and both sidecars.
- Pass `--no-kreuzberg` to skip kreuzberg (if running it separately); pass `--no-qdrant` to skip the Qdrant sidecar (falls back to local file mode).

**`deploy --systemd-service`** delegates lifecycle entirely to systemd (see [Systemd / Quadlet](#systemd--quadlet) below). Each sidecar gets its own Quadlet unit; the main service declares `Requires=` on both so systemd starts them in the correct order.

### Environment variable handoff

The backend locates its sidecars via environment variables:

| Variable | Sidecar | Approach | Where it is set |
| -------- | ------- | -------- | --------------- |
| `KREUZBERG_URL` | kreuzberg | `docker compose` | Hard-coded in `docker-compose.yml` |
| `KREUZBERG_URL` | kreuzberg | `bin/container.mjs` | Injected at runtime via `extraEnv` |
| `QDRANT_URL` | qdrant | `docker compose` | Hard-coded in `docker-compose.yml` |
| `QDRANT_URL` | qdrant | `bin/container.mjs` | Injected at runtime via `extraEnv` |
| `UVICORN_WORKERS` | — | `docker compose` | Passed from host env / `.env` (default `4`) |
| `UVICORN_WORKERS` | — | `bin/container.mjs` | Pass via `--env UVICORN_WORKERS=N` |

When `QDRANT_URL` is **not set**, the backend falls back to local embedded Qdrant file mode (`QdrantClient(path=...)`). This mode is incompatible with `--workers > 1` due to file-lock contention, so the Dockerfile uses a single worker in that case.

### When to use which

| Scenario | Recommended tool |
| -------- | ---------------- |
| Local development on your own machine | `docker compose up -d` |
| CI image build | GitHub Actions workflow (no orchestration needed) |
| Remote server deployment (nginx + SSL) | `bin/container.mjs deploy` or `bin/deploy.mjs` |
| Fine-grained control (custom name, port, volumes) | `bin/container.mjs start` |

---

## Files

| File | Purpose |
| ---- | ------- |
| `Dockerfile` | Multi-stage build for the main backend image |
| `docker-compose.yml` | Local multi-container setup |
| `bin/container.mjs` | CLI: `build`, `push`, `start`, `stop`, `restart`, `logs`, `deploy` |
| `bin/deploy.mjs` | Reads a `.env.deploy.*` file and delegates to `container.mjs deploy` |
| `.env.deploy.example` | Example deployment environment file |

---

## Dockerfile

Multi-stage build:

```dockerfile
# Stage 1: dependency installer (uv)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ARG INSTALL_LOCAL_MODELS=false
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then \
      uv sync --frozen --no-dev --no-install-project --extra local-models; \
    else \
      uv sync --frozen --no-dev --no-install-project; \
    fi

# Stage 2: runtime (slim, no build tools)
FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY backend/ ./backend/
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app
VOLUME /data
ENV MODEL_WEIGHTS_PATH=/data/models
ENV LOG_FILE=/data/logs/server.log
ENV KREUZBERG_URL=http://kreuzberg:8100
ENV UVICORN_WORKERS=4
EXPOSE 8119
CMD uvicorn backend.main:app --host 0.0.0.0 --port 8119 --workers ${UVICORN_WORKERS}
```

OCR (Tesseract) lives in the kreuzberg sidecar — the main image stays slim.

The worker count is controlled by `UVICORN_WORKERS` (default `4`). Multiple workers require the Qdrant sidecar — set `UVICORN_WORKERS=1` when running without it (`--no-qdrant` / local embedded file mode).

### Build arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `INSTALL_LOCAL_MODELS` | `false` | Install `sentence-transformers`/`torch` for local-inference presets (~1–2 GB extra) |

---

## CLI (`bin/container.mjs`)

```js
APP_NAME  = 'zotero-rag'
REGISTRY  = 'docker.io/cboulanger/zotero-rag'
PORT      = 8119
```

### External dependencies

| Dependency | Required for | Notes |
| ---------- | ------------ | ----- |
| `docker` 20.10+ **or** `podman` 4.0+ | all subcommands | auto-detected; daemon/socket must be reachable |
| `nginx` | `deploy` (nginx/SSL, default on) | skip with `--no-nginx` |
| `certbot` + nginx plugin | `deploy` (SSL, default on) | e.g. `apt install certbot python3-certbot-nginx`; skip with `--no-ssl` |
| `systemctl` | `deploy --systemd-service` | requires root |
| Podman Quadlet generator | `deploy --systemd-service` (Podman 4.4+) | falls back to a traditional `[Service]` unit on Podman < 4.4 |

**Podman on Debian/Ubuntu — PATH caveat:** both network backends (netavark and CNI) call `iptables` internally, which lives in `/usr/sbin`. `sudo` strips that directory from PATH by default, so always run with:

```bash
sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/deploy.mjs ...
# or
sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/container.mjs deploy ...
```

**Tested on:**

- Debian 12 (Bookworm) with Podman 4.3.1 — legacy systemd unit fallback
- Ubuntu 22.04 / 24.04 with Docker 24+
- macOS (Docker Desktop) — `build`, `push`, `start`, `stop`, `logs` only; nginx/SSL/systemd not available
- Windows — `deploy` not supported

### Commands

| Command | Description |
| ------- | ----------- |
| `build [options]` | Build main image locally |
| `push [options]` | Tag + push to registry (reads `DOCKER_HUB_USERNAME`/`DOCKER_HUB_TOKEN` from `.env`) |
| `start [options]` | Start kreuzberg + Qdrant sidecars + app container; auto-detects local → registry image |
| `stop [options]` | Stop app container and its sidecars |
| `restart [options]` | Stop + start all containers |
| `logs [options]` | Stream app or sidecar logs |
| `deploy [options]` | Pull/rebuild → start all containers → nginx config → SSL cert (Linux only for nginx/SSL) |

### `build` / `push` options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--tag TAG` | auto from git | Image tag |
| `--local-models` | off | Install sentence-transformers/torch (~1–2 GB extra; local-inference presets only) |
| `--platform PLATFORM` | host arch | Target platform, e.g. `linux/amd64` |
| `--no-cache` | — | Disable layer cache |
| `--yes` | — | Skip confirmation prompt |
| `--no-build` *(push only)* | — | Push existing image without rebuilding |

### `start` options

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--tag TAG` | `latest` | Image tag |
| `--name NAME` | `zotero-rag-<tag>` | Container name |
| `--port PORT` | `8119` | Host port |
| `--data-dir DIR` | — | Host path mounted at `/data`; sets `MODEL_WEIGHTS_PATH` (and Qdrant storage if `--qdrant-data-dir` is unset) |
| `--qdrant-data-dir DIR` | — | Override Qdrant storage path (default: `<data-dir>/qdrant-server`) |
| `--env KEY[=VAL]` | — | Extra env vars (repeatable); `KEY` alone transfers from host |
| `--volume HOST:CTR` | — | Extra volume mounts (repeatable) |
| `--restart POLICY` | — | Docker restart policy |
| `--no-detach` | — | Run in foreground |
| `--no-kreuzberg` | — | Skip kreuzberg sidecar (use if running kreuzberg separately) |
| `--no-qdrant` | — | Skip Qdrant sidecar; falls back to local embedded file mode |

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
| `--local-models` | — | Install local-inference deps when rebuilding |
| `--platform PLATFORM` | — | Target platform when rebuilding |
| `--qdrant-data-dir DIR` | — | Override Qdrant storage path (default: `<data-dir>/qdrant-server`) |
| `--no-nginx` | — | Skip nginx configuration |
| `--no-ssl` | — | Skip SSL certificate setup |
| `--email EMAIL` | `admin@<fqdn>` | Email for certbot |
| `--systemd-service NAME` | — | Register all three containers as Quadlet systemd services (requires sudo) |
| `--shared-kreuzberg NAME` | — | Depend on an existing kreuzberg service instead of creating one |
| `--shared-qdrant NAME` | — | Depend on an existing Qdrant service instead of creating one |
| `--no-qdrant` | — | Skip Qdrant sidecar; falls back to local embedded file mode |
| `--yes` | — | Skip confirmation prompt |
| `--restart-sidecars` | — | Restart kreuzberg/qdrant even if already running (default: skip if running) |
| `--reconfigure-nginx` | — | Rewrite nginx config even if already set up |
| `--reconfigure-ssl` | — | Re-run certbot even if SSL certificate already exists |

### Platform handling

- Detects `docker` or `podman` (prefers docker); verifies daemon connectivity
- On **Linux**: automatically adds `--add-host=host.docker.internal:host-gateway` (needed only if any service on the container accesses the host machine)
- `--platform linux/amd64` cross-build via QEMU is supported but unreliable for Rust packages — prefer the GitHub Actions CI workflow instead

---

## Deploy Wrapper (`bin/deploy.mjs`)

Reads a `.env.deploy.*` file (**required** positional argument) and delegates
to `container.mjs deploy`:

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
| `DEPLOY_PULL=true` | `--pull` |
| `DEPLOY_SSL=false` | `--no-ssl` |
| `DEPLOY_NGINX=false` | `--no-nginx` |
| `DEPLOY_LOCAL_MODELS=true` | `--local-models` |
| `DEPLOY_SYSTEMD_SERVICE` | `--systemd-service` |
| `DEPLOY_SHARED_KREUZBERG` | `--shared-kreuzberg` |
| `DEPLOY_SHARED_QDRANT` | `--shared-qdrant` |
| `DEPLOY_QDRANT_DATA_DIR` | `--qdrant-data-dir` |
| `DEPLOY_RESTART_SIDECARS=true` | `--restart-sidecars` |
| `DEPLOY_RECONFIGURE_NGINX=true` | `--reconfigure-nginx` |
| `DEPLOY_RECONFIGURE_SSL=true` | `--reconfigure-ssl` |
| Everything else | `--env KEY` (value loaded from env via `dotenv.config`) |

If `DEPLOY_FQDN` is unset or equals `localhost`/`127.0.0.1`, the script
automatically appends `--no-nginx --no-ssl`.

---

## Systemd / Quadlet

Use `--systemd-service <name>` (or `DEPLOY_SYSTEMD_SERVICE=<name>` in the env file) to have `deploy` register both containers as systemd services via [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) instead of relying on `--restart unless-stopped`.

### Why Quadlet over `--restart unless-stopped`

The podman restart policy only fires on clean process exits. If a container enters a zombie state (runtime thinks it's running but it isn't), the restart policy never triggers. Quadlet units always create a fresh container on start, force-removing any stale state first — the equivalent of `podman rm -f` before every `podman run`.

### What gets created

For `--systemd-service zotero-rag`:

| File | Service | Purpose |
| ---- | ------- | ------- |
| `/etc/containers/systemd/zotero-rag-kreuzberg.container` | `zotero-rag-kreuzberg.service` | Kreuzberg sidecar |
| `/etc/containers/systemd/zotero-rag-qdrant.container` | `zotero-rag-qdrant.service` | Qdrant vector DB sidecar |
| `/etc/containers/systemd/zotero-rag.container` | `zotero-rag.service` | Main backend |

The main service declares `Requires=zotero-rag-kreuzberg.service` and `Requires=zotero-rag-qdrant.service` so systemd starts all three in the correct order.

### Shared kreuzberg

If you run multiple backend instances on the same host, they can share one kreuzberg sidecar. Deploy the first instance normally, then use `--shared-kreuzberg` for subsequent ones:

```bash
# First instance — creates zotero-rag-kreuzberg.service
sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/deploy.mjs .env.deploy.instance1

# Second instance — reuses the existing kreuzberg service
# Set DEPLOY_SHARED_KREUZBERG=zotero-rag-kreuzberg in .env.deploy.instance2
sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/deploy.mjs .env.deploy.instance2
```

### Useful commands

```bash
systemctl status zotero-rag
journalctl -u zotero-rag -f
journalctl -u zotero-rag-kreuzberg -f
journalctl -u zotero-rag-qdrant -f
systemctl restart zotero-rag             # restarts app only
systemctl restart zotero-rag-kreuzberg   # restarts kreuzberg (cascades to app via Requires)
systemctl restart zotero-rag-qdrant      # restarts Qdrant (cascades to app via Requires)
```

---

## nginx template

Proxies to `http://127.0.0.1:8119` with:

- `client_max_body_size 300M` (configurable via `--max-body-size` / `DEPLOY_MAX_BODY_SIZE`)
- 300 s proxy timeouts
- `proxy_buffering off` for SSE endpoints (`/api/query/stream`)

---

## CI/CD: GitHub Actions Build

The workflow at [`.github/workflows/docker-build.yml`](../.github/workflows/docker-build.yml) builds and pushes the image on real `linux/amd64` hardware, avoiding all QEMU/cross-compilation issues.

### Triggers

| Event | Tag produced |
| ----- | ------------ |
| Push to `main` (backend/Dockerfile changes) | `latest` |
| Manual (`workflow_dispatch`) from any branch | `<branch>-<sha>` (or custom tag) |

### Manual trigger

Go to **Actions → Docker Build & Push → Run workflow**, select your branch, and optionally:

- Set a custom tag
- Enable `local_models` (adds sentence-transformers/torch)

### Required repository secrets

| Secret | Value |
| ------ | ----- |
| `DOCKER_HUB_USERNAME` | Your Docker Hub username |
| `DOCKER_HUB_TOKEN` | Docker Hub Personal Access Token (read/write) |

Create a token at [hub.docker.com/settings/security](https://hub.docker.com/settings/security).

### Layer caching

The workflow uses Docker's registry-based build cache (`buildcache` tag on Docker Hub). Subsequent builds reuse unchanged layers and complete in seconds for incremental changes.

---

## Implementation Notes

- `pyproject.toml` is at the **repo root** (not in `backend/`), so `uv sync` works from `/app`
- kreuzberg (document extraction) runs as a sidecar — Tesseract, Pandoc, and PDFium are bundled there; the main image needs no build tools
- The image does **not** bundle model weights; they are downloaded to `/data/models` on first use
- For GPU support, a separate `Dockerfile.gpu` extending the base image with CUDA is a future option
