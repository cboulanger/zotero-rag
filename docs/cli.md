# Zotero RAG - CLI Commands

Quick reference for all CLI commands. Run from project root directory.

## Server Management

### Production (Backend Only)

| Command | Description |
|---------|-------------|
| `npm start` or `npm run server:start` | Start backend server (production mode, no auto-reload) |
| `npm run server:stop` | Stop backend server |
| `npm run server:restart` | Restart backend server (production mode) |
| `npm run server:status` | Check backend server status |

### Development (Backend + Plugin)

| Command | Description |
|---------|-------------|
| `npm run dev:start` | Start backend (dev mode with auto-reload) + plugin server |
| `npm run dev:stop` | Stop both backend and plugin servers |
| `npm run dev:restart` | Restart both servers (dev mode) |
| `npm run dev:status` | Check status of both servers |

### Plugin Only

| Command | Description |
|---------|-------------|
| `npm run dev:plugin:start` | Start plugin development server only |
| `npm run dev:plugin:stop` | Stop plugin development server and Zotero |

**Server URL:** http://localhost:8119

**Development Mode:**
The `dev:*` commands manage both the backend server and the plugin development server. The plugin dev server automatically starts its own Zotero instance via RDP (Remote Debugging Protocol) for hot-reloading. When you stop the dev environment, both the plugin server and Zotero are properly terminated.

## Testing

### Unit Tests (Fast, No External Dependencies)

| Command | Description |
|---------|-------------|
| `npm run test:backend` | Run all backend unit tests (161 tests, ~5-10 seconds) |
| `npm run test:backend:watch` | Run tests in watch mode (reruns on changes) |
| `npm run test:backend:coverage` | Run tests with coverage report (HTML output) |

### Integration Tests (Requires Zotero + API Keys)

| Command | Description |
|---------|-------------|
| `npm run test:integration:quick` | Quick health check (~30 seconds) |
| `npm run test:integration` | Full integration suite (5-15 minutes) |
| `npm run test:all` | All tests (unit + integration, 10-20 minutes) |

**See [testing.md](testing.md) for detailed testing documentation.**

## Plugin Development

### Commands

| Command | Description |
|---------|-------------|
| `npm run dev:start` | Start backend + plugin dev server together |
| `npm run dev:stop` | Stop both servers |
| `npm run dev:plugin:start` | Start plugin dev server only (starts Zotero) |
| `npm run dev:plugin:stop` | Stop plugin dev server (stops Zotero) |
| `npm run plugin:build` | Build plugin XPI (output: `plugin/dist/zotero-rag-{version}.xpi`) |

### Development Workflow

**Full Stack Development (Recommended):**

1. Start both servers: `npm run dev:start`
2. Make changes to plugin source files in `plugin/src/`
3. Changes are automatically hot-reloaded in Zotero
4. Backend API is available at <http://localhost:8119>
5. Stop both servers: `npm run dev:stop`

**Plugin Only Development:**

1. Start plugin server: `npm run dev:plugin:start` (launches Zotero)
2. Make changes to plugin source files
3. Changes are automatically hot-reloaded in Zotero
4. Stop plugin server: `npm run dev:plugin:stop` (closes Zotero)

**Installation:** In Zotero: Tools → Add-ons → Install Add-on From File → Select XPI

**Important Notes:**

- The plugin development server uses `zotero-plugin-scaffold` which provides automatic hot-reloading
- You don't need to rebuild the plugin after each change during development
- The dev server starts its own Zotero instance via RDP - **close any running Zotero instances first**
- Stopping the dev server automatically closes the Zotero instance and cleans up orphaned processes

## Direct Python Commands

```bash
# Start server
uv run uvicorn backend.main:app --reload --host localhost --port 8119

# Run tests
uv run pytest backend/tests/ -v                    # All unit tests
uv run pytest backend/tests/ -m integration -v     # Integration tests only
uv run pytest backend/tests/ -k "embedding" -v     # Tests matching keyword
uv run pytest backend/tests/ --cov=backend         # With coverage

# Package management
uv sync                # Install dependencies
uv add package-name    # Add package
uv remove package-name # Remove package
```

## Environment Setup

```bash
# Initial setup
uv sync              # Install Python dependencies
npm install          # Install Node.js dependencies
cp .env.dist .env    # Create environment config

# Configure .env
MODEL_PRESET=mac-mini-m4-16gb  # Or: cpu-only, gpu-high-memory, remote-openai, remote-kisski
OPENAI_API_KEY=sk-...          # Optional: for remote presets
KISSKI_API_KEY=...             # Optional: for KISSKI preset
```

### Hardware Presets

| Preset | Target Hardware | Memory |
|--------|----------------|--------|
| `mac-mini-m4-16gb` | Mac Mini M4, 16GB RAM (default) | ~6-7GB |
| `cpu-only` | No GPU, low memory | ~2-3GB |
| `gpu-high-memory` | GPU + >24GB RAM | ~10-12GB |
| `remote-openai` | OpenAI/Anthropic API | ~1GB |
| `remote-kisski` | GWDG KISSKI Academic Cloud | ~1GB |

**See [architecture.md](architecture.md#configuration-system) for detailed preset specifications.**

## Quick Start

```bash
# 1. Start backend server only
npm run server:start

# 2. Verify it's running
curl http://localhost:8119/health

# 3. Run tests
npm run test:backend

# 4. Build plugin
npm run plugin:build

# 5. Install plugin/dist/zotero-rag-0.1.0.xpi in Zotero
```

**For Plugin Development:**

```bash
# 1. Make sure Zotero is not running
# 2. Start both backend and plugin development servers
npm run dev:start

# 3. Make changes to plugin source in plugin/src/

# 4. Changes auto-reload in Zotero

# 5. Stop both servers (also stops Zotero)
npm run dev:stop
```

## Troubleshooting

```bash
# Server won't start
npm run server:status  # Check if running
npm run server:stop    # Force stop
npm run server:start   # Start again

# Tests failing
npm run test:backend -- -v     # Verbose output
npm run test:backend -- --lf   # Run last failed only

# Plugin build issues
rm -rf plugin/build plugin/dist  # Clean artifacts
npm run plugin:build              # Rebuild
```

---

**References:** [Architecture](architecture.md) | [Testing](testing.md) | [Master Plan](../implementation/master.md)
