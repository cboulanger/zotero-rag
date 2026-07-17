# General Coding Guidelines

## Container Runtime

- **Always use `podman`** for all container operations — never `docker`
- The deploy commands in `bin/container.mjs` only support podman
- For compose operations use `podman compose` (not `docker-compose`)

## Debugging and Hotfixing a Production Container

### Deployment overview

Production instances are deployed with:
```bash
sudo env "PATH=$PATH:/usr/sbin:/sbin" node bin/deploy.mjs .local/.env.deploy.<target>
```
This requires a CI-built image in the registry (`DEPLOY_PULL=true`). For hotfixes without CI, see below.

The deploy env file (`.local/.env.deploy.<target>`) contains `DEPLOY_*` keys that map to `container.mjs deploy` flags, plus container env vars. The critical keys for identifying containers:

```
DEPLOY_FQDN=rag.example.com   # determines container names
DEPLOY_TAG=latest              # image tag
DEPLOY_PORT=9119               # host port
```

### Deriving container names

Container names are derived deterministically from `DEPLOY_FQDN` (dots replaced with hyphens):

```
APP_NAME = "zotero-rag"   (constant, defined in bin/container.mjs)
FQDN     = "rag.example.com"

Main container:      zotero-rag-rag-example-com
Qdrant sidecar:      zotero-rag-rag-example-com-qdrant
Kreuzberg sidecar:   zotero-rag-rag-example-com-kreuzberg
Systemd service:     zotero-rag  (or the value of DEPLOY_SYSTEMD_SERVICE)
Image name:          zotero-rag:latest
```

To confirm names at any time: `sudo podman ps | grep zotero-rag`

### Two separate podman image stores

`podman` (no sudo) and `sudo podman` use **different image stores**:
- User store: `~/.local/share/containers/storage` — used by `node bin/container.mjs build`
- Root store: `/var/lib/containers/storage` — used by systemd / `sudo podman run`

**`node bin/container.mjs build` is useless for production.** Always build with `sudo podman build` when the service runs under systemd as root.

### How the systemd service works

The service definition uses `ExecStartPre=-/usr/bin/podman rm -f <container-name>`, so **every restart creates a fresh container from the current image**. This means:
- `sudo podman cp` file changes into a running container **are lost on the next restart**
- Sending SIGHUP to the container (via `sudo podman kill --signal HUP`) signals PID 1 (the shell wrapper), which kills it; systemd then restarts from the unchanged image
- The only way to make changes survive restarts is to rebuild the image

### Hotfix workflow (no CI required)

For small changes (1–few files), use a thin patch image. Full rebuilds re-download all dependencies and often time out on slow connections.

**1. Edit the source files** normally with Edit/Write tools.

**2. Build a thin patch image** (seconds, no network):
```bash
# Create a temporary patch Dockerfile listing only the changed files
cat > /tmp/Dockerfile.patch << 'EOF'
FROM localhost/zotero-rag:latest
COPY backend/path/to/changed.py /app/backend/path/to/changed.py
EOF

sudo podman build -f /tmp/Dockerfile.patch -t zotero-rag:latest /home/cloud/zotero-rag
```

**3. Verify the new image has the change:**
```bash
sudo podman run --rm zotero-rag:latest grep -c "new_symbol" /app/backend/path/to/changed.py
```

**4. Restart the service:**
```bash
sudo systemctl restart zotero-rag.service   # or the value of DEPLOY_SYSTEMD_SERVICE
```

**5. Verify** (allow ~8s for startup):
```bash
sleep 8 && sudo systemctl status zotero-rag.service | head -5
```

### Inspecting the running container

```bash
# Check logs
sudo podman logs -f zotero-rag-rag-example-com

# Run a one-off command inside the container
sudo podman exec zotero-rag-rag-example-com python3 -c "..."

# Check which image the container is running from
sudo podman inspect zotero-rag-rag-example-com --format '{{.Image}}'

# List env vars (includes API keys — handle carefully)
sudo podman exec zotero-rag-rag-example-com env
```

### Cleanup

After hotfixing, remove non-root (user-space) images to free space:
```bash
podman rmi --all
```

### Debugging the cron indexer

The hourly cron job is defined in `/etc/cron.d/zotero-rag-indexer`. It runs `index_libraries.py` inside the main container via `podman exec` and appends **stderr** to the log file.

**Key flow — auto-index keys (not `--slugs-file` / `ZOTERO_API_KEY`):**

The cron job no longer uses a static slugs file or a global `ZOTERO_API_KEY`. Instead, indexing targets come from the encrypted auto-index key store at `<data_path>/system/autoindex_keys.json`. The same personal Zotero API key a user enters in the plugin's setup wizard (or Preferences) also authenticates their normal plugin use — auto-indexing is just an on/off toggle reusing that key, not a separate credential. Keys are added by users via the plugin (Preferences → Automatic indexing) or on the server with:

```bash
uv run python bin/autoindex_add_key.py <read-only-zotero-api-key>
```

Only **read-only** Zotero API keys are accepted; write-scoped keys are rejected at submission time.

**`AUTOINDEX_SECRET` env var (required):**

`AUTOINDEX_SECRET` must be set in the deploy env file. It is a Fernet symmetric key used to encrypt/decrypt the key store. Without it the cron job exits immediately with "AUTOINDEX_SECRET is not set; no keys can be decrypted. Nothing to index." and the auto-index API endpoints return 503.

Generate a new secret with:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add the output as `AUTOINDEX_SECRET=<value>` in `.local/.env.deploy.<target>`.

The encrypted key store's path on the host is `<DEPLOY_DATA_DIR>/system/autoindex_keys.json`
(root-owned; `DEPLOY_DATA_DIR` is set per deployment in its `.local/.env.deploy.<target>`
file), and the matching `AUTOINDEX_SECRET` lives in that same deploy env file.
Decrypting the store gives the read-only Zotero keys already on file for
auto-indexing — useful when you need a read-only key (e.g. for the admin
scheduler endpoints below) and don't want to ask the user for a fresh one.
Easiest done from inside the running container via `podman exec`, which
already has `AUTOINDEX_SECRET` in its env and the data volume mounted — see
`backend/services/autoindex_key_store.py`'s `AutoIndexKeyStore.iter_decrypted()`.

**Key validation and pruning:**

Each run re-validates all stored keys against `api.zotero.org/keys`. Keys that are permanently invalid (revoked, expired, or write-scoped) are pruned from the store. Keys that fail due to transient errors (network issues, 5xx responses) are kept and their previously-stored targets are reused for that run. Pruned keys appear in `cron_status.json` under `key_issues` and in the plugin Preferences.

**Log file location (on the host):**

```text
/home/cloud/data/zotero-rag/logs/cron_indexer.log
```

**Important:** Timestamps in the log are **UTC**, not local time (CEST = UTC+2).

**Watch live progress (filter out noisy HTTP lines):**
```bash
tail -f /home/cloud/data/zotero-rag/logs/cron_indexer.log | grep -v "HTTP Request"
```

**Check recent meaningful events:**
```bash
grep -v "HTTP Request" /home/cloud/data/zotero-rag/logs/cron_indexer.log | tail -20
```

**Run manually (writes to log file, matches what cron does):**
```bash
sudo podman exec zotero-rag-zotero-rag-panya-de python bin/index_libraries.py \
  > /dev/null 2>> /home/cloud/data/zotero-rag/logs/cron_indexer.log &
```

Running without `2>>` redirects output to the terminal/background and nothing appears in the log.

**Enable/disable indexing:**

If the deployment uses the built-in scheduler (`AUTOINDEX_INTERVAL_MINUTES`
set in the deploy env file), pause/resume it without a restart, as an admin
— the calling Zotero key must belong to an owner/admin of the server's
`AUTHORIZED_GROUP_ID` (see `docs/cron-indexing.md`'s "Admin Controls"
section):

```bash
curl -X POST https://rag.example.com/api/autoindex/scheduler/pause \
  -H "X-Zotero-API-Key: <admin-read-only-key>"
curl -X POST https://rag.example.com/api/autoindex/scheduler/resume \
  -H "X-Zotero-API-Key: <admin-read-only-key>"
```

To force an immediate full run instead of waiting for the next tick:

```bash
curl -X POST https://rag.example.com/api/autoindex/scheduler/run-now \
  -H "X-Zotero-API-Key: <admin-read-only-key>"
```

To disable scheduling entirely, unset `AUTOINDEX_INTERVAL_MINUTES` in the
deploy env file and restart the service; setting it again and restarting
re-enables it.

If the deployment still uses the external `/etc/cron.d/zotero-rag-indexer`
job instead (see `docs/cron-indexing.md`'s "Alternative: external scheduler"):

```bash
# Disable (comment out)
sudo sed -i 's|^0 \* \* \* \* root /usr/bin/podman exec|#DISABLED 0 * * * * root /usr/bin/podman exec|' /etc/cron.d/zotero-rag-indexer

# Re-enable
sudo sed -i 's|^#DISABLED 0 \* \* \* \* root|0 * * * * root|' /etc/cron.d/zotero-rag-indexer

# Verify
sudo cat /etc/cron.d/zotero-rag-indexer
```

**Monitor memory and process RSS during a run** (`ps --sort` flag not available on this Debian system — use `sort` pipe):
```bash
free -h
ps -eo pid,rss,pcpu,comm | sort -k2 -rn | grep -E "python|qdrant|uvicorn" | head -8
```

**Check for OOM kills:**
```bash
sudo dmesg --since "1 hour ago" | grep -i "oom\|killed process"
```

**Note on Qdrant RSS:** Qdrant shows a very large RSS (e.g. 13 GB) in `ps` because it uses memory-mapped files for its vector store. This is normal — those pages are file-backed and the kernel can evict them. Check `free -h`'s **available** column (not `free`) to assess actual memory pressure.

**Host swap:** An 8 GB swapfile lives at `/swapfile` (persistent via `/etc/fstab`). It must be set up manually on each new server — it is intentionally not in the deploy scripts because disk capacity varies per host.

## Python Environment

- **Python Version**: 3.12 (downgraded from 3.13 due to PyTorch compatibility issues on Windows)
- **Package Manager**: Always use `uv` for all Python operations
- **Virtual Environment**: All Python commands must be executed within the uv-managed virtual environment
  - Use `uv run <command>` for one-off commands
  - Use `uv pip install <package>` for installing dependencies
  - Never use global Python or pip directly

## Live Server

`npm start`

[OK] Server started successfully (PID: 55430)
[OK] Access at: <http://localhost:8119>
[OK] API docs at: <http://localhost:8119/docs>
[OK] Logs: /Volumes/Data-SSD/Code/zotero-rag/logs/server.log

## Testing

### Python Tests

- Use Python's built-in `unittest` framework
- Test files should be named `test_*.py` and placed in a `tests/` directory
- Run tests with: `uv run pytest` (after installing pytest) or `uv run python -m unittest discover`
- Aim for comprehensive coverage of all library methods and services
- Write tests before or alongside implementation (TDD encouraged)

### Container Smoke Test

After any change to `Dockerfile`, `docker-compose.yml`, `docker-compose.smoke.yml`, or the container startup path (e.g. `backend/main.py`, `backend/dependencies.py`, `backend/config/settings.py`), run the container smoke test to verify the full stack builds and starts correctly:

```bash
uv run pytest -m container -v -s
```

This test is excluded from the default `uv run pytest` run. It requires podman or docker and is skipped automatically if neither is available.

### Startup Sequence Test

After any change to `docker-compose.yml` or `bin/container.mjs` (especially the Qdrant wait logic, healthcheck, or sidecar startup order), run the startup sequence test:

```bash
uv run python scripts/test_startup_sequence.py
```

This verifies that:

- `podman compose up` waits for Qdrant to be healthy before starting `zotero-rag`
- `container.mjs start` calls `waitForQdrant` and resolves it before launching the main container
- `container.mjs restart` does the same during a restart cycle

### Node.js Tests

- Use Node.js built-in test runner (available in Node 23)
- Test files should be named `*.test.js` or placed in a `test/` directory
- Run tests with: `node --test`
- Test all plugin UI interactions and API communication logic

## Code Organization

- **Modularity**: Organize code into reusable modules to keep business logic clean and lightweight
- **Separation of Concerns**: Separate API routes, business logic, data access, and utilities into distinct modules
- **Single Responsibility**: Each module/class should have a single, well-defined purpose
- **DRY Principle**: Avoid code duplication by extracting common functionality into shared utilities

## Project Structure

### Backend (Python/FastAPI)

```text
backend/
├── api/              # FastAPI routes and endpoint handlers
├── services/         # Core business logic (embeddings, LLM, RAG)
├── models/           # Pydantic models and data schemas
├── db/               # Database interfaces and repositories
├── utils/            # Shared utilities and helpers
├── tests/            # Unit and integration tests
└── pyproject.toml    # UV project configuration
```

**Note:** Environment configuration template is at project root: `.env.dist`

### Plugin (Node.js/JavaScript)

```text
plugin/
├── src/
│   ├── bootstrap.js      # Plugin lifecycle
│   ├── ui/               # Dialog and UI components
│   ├── api/              # Backend communication
│   ├── zotero/           # Zotero API interactions
│   └── utils/            # Shared utilities
├── locale/               # Localization files
├── test/                 # Plugin tests
├── manifest.json         # Plugin manifest
└── package.json          # Node.js dependencies
```

## Zotero Plugin Development

### Reference Documentation

Zotero plugin development knowledge (bootstrap lifecycle, chrome protocol, dialogs, menus, preferences, logging, Zotero API patterns, toolkit helpers, useful APIs) has been moved to Claude Skills at **<https://github.com/cboulanger/zotero-skills/>**.

Available skills (invoke via `/skill-name` in Claude Code):

- **`zotero-plugin-dev`** — vanilla plugin development, Zotero APIs, and discovered API index
- **`zotero-plugin-toolkit`** — `UITool`, `DialogHelper`, `VirtualizedTableHelper`, and other toolkit helpers

### Development Workflow

**Hot Reload Plugin Development Server:**

- This project uses the `zotero-plugin` development scaffold (<https://zotero-plugin-dev.github.io/zotero-plugin-scaffold/quick-start.html>)
- **DO NOT rebuild the plugin** after making changes to plugin source files
- The development server automatically reloads changes in Zotero
- To start development mode: `npm run start` (or appropriate command from package.json)
- Only use `scripts/build_plugin.py` for creating final distribution builds

### UI Development - Pragmatic Approach

**XUL (XML User Interface Language) is deprecated in Firefox but still functional in Zotero 7/8.**

**Recommended Approach:**

- Use **HTML elements with `html:` namespace** when possible for future-proofing
- **XUL is acceptable** for dialogs, preferences, and UI components if it simplifies development
- Prefer HTML for new code, but don't block on XUL if it works
- Focus on functionality over ideological purity

**Common Patterns:**

- XUL `<dialog>` with HTML children using `html:` namespace works well
- Mix XUL layout (`<vbox>`, `<hbox>`) with HTML form elements
- Use `createXULElement()` for menu items and structural elements
- Use `html:` prefix for form inputs, labels, buttons when feasible

### Dialog and Window Creation

For creating dialog windows in Zotero plugins:

1. Use XUL `<dialog>` or `<window>` as root element for `window.openDialog()`
2. Mix HTML elements (with `html:` namespace) for form controls
3. Apply styles using standard CSS files
4. Reference working plugin examples in `zotero-addons/` directory

## Code Quality

- **Type Hints**: Use Python type hints for all function signatures and class attributes
- **Docstrings**: Document all public functions, classes, and modules using clear docstrings
- **Error Handling**: Implement proper error handling with specific exception types
- **Logging**: Use appropriate logging levels (DEBUG, INFO, WARNING, ERROR) for operational visibility. In Zotero plugin scripts, use `console.log/warn/error` as normal — the `console` object is patched at the top of each script to route output through `Services.console` so messages appear in the Browser Console (Tools > Developer > Browser Console). See `docs/zotero-plugin-dev.md` for details.
- **Code Style**: Follow PEP 8 for Python, Standard JavaScript style for Node.js
- **Console Output**: Avoid Unicode emoji characters (✅ ❌ ➜ etc.) in print statements as they cause UnicodeEncodeError on Windows. Use ASCII alternatives like `[PASS]`, `[FAIL]`, `->` instead

## Version Control

- Write clear, descriptive commit messages
- Make atomic commits that represent single logical changes
- Reference issues/tasks in commit messages when applicable

## Debugging

- if you insert code that is only for debugging, mark it as such so that it can be easily idenitified and removed after the code has been fixed (e.g., by a `# DEBUG` trailing comment or `# BEGIN DEBUG`/`# END DEBUG` header and footer for longer code fragments).

## Documentation

- Maintain up-to-date README.md files in each major directory
- Document API endpoints with request/response examples
- Include setup and installation instructions
- Document configuration options and environment variables
- Keep inline comments focused on "why" rather than "what"
- In Javascript files, use TypeScript-compatible JSDOC annotations throughout for typing variables and documenting function parameters. Use the full power or typescript embedded in JSDoc, don't use generic types. Remember this is plain javascript, don't use Typescript directly.
- **Never fix markdown lint issues (formatting, table style, etc.) in files under `docs/history/`** (including `docs/history/implementation/`). These are historical/implementation-progress records written for agent consumption when resuming work, not published documentation — don't spend edits polishing their formatting.
- **User-facing documentation (README, CLAUDE.md's operational sections, `docs/*.md` architecture/setup guides, `.env.dist` comments, plugin UI/help text) must describe only the current status-quo behavior — do not reference prior behavior a feature migrated from ("previously X, now Y", "as of the Z migration", etc.).** The software is not yet publicly released, so there is no installed base that needs migration context; mentioning a superseded behavior only confuses a reader who never saw it. This rule applies for any `1.*` release; it no longer applies once the project reaches `2.0` or later (at that point documenting migrations for upgrading users becomes appropriate). This does **not** apply to `docs/history/` and `docs/superpowers/` records, which are explicitly historical/planning documents for agent consumption, not user-facing docs.

## Implementation progress documentation

- When implementing the master implementation plan,  create an document for each phase where you document what has been implemented and, after a phase is complete,  add short summary at the end of `master.md` and link to this document.
- If a step in a phase is complex, document that step separately. The master and the implementation documents should allow you to resume work in separate sessions any time.

## Security

- Never commit secrets, API keys, or credentials to version control
- Use environment variables for all sensitive configuration
- Validate and sanitize all user inputs
- Use parameterized queries to prevent injection attacks
- Keep dependencies updated to patch security vulnerabilities

## Performance

- Implement batch processing for large datasets
- Use async/await for I/O-bound operations
- Consider caching for frequently accessed data
- Profile code to identify bottlenecks before optimizing
- Document performance considerations for resource-intensive operations

## FastAPI: Never block the event loop

In FastAPI, **`async def` route handlers must not call synchronous blocking I/O** (e.g. synchronous Qdrant `scroll()`, `set_payload()`, file reads, or any other blocking call). Doing so freezes the entire asyncio event loop for the duration of the call — no other requests can be processed, active HTTP connections go silent, and clients drop them with `NetworkError` before the response is ever sent.

**Rule**: If a route handler only calls synchronous (non-awaitable) code, declare it as `def`, not `async def`. FastAPI automatically runs `def` handlers in a thread pool (`anyio.to_thread`), keeping the event loop free.

```python
# WRONG — blocks the event loop for every Qdrant call in the loop
async def batch_update_metadata(request: ..., vector_store = Depends(...)):
    for item in request.items:
        vector_store.update_item_metadata(...)   # synchronous Qdrant I/O

# CORRECT — FastAPI runs this in a thread pool
def batch_update_metadata(request: ..., vector_store = Depends(...)):
    for item in request.items:
        vector_store.update_item_metadata(...)
```

If a handler mixes `await` calls with blocking I/O, wrap the blocking parts in `asyncio.to_thread()`:

```python
async def mixed_handler(...):
    await some_async_operation()
    result = await asyncio.to_thread(vector_store.slow_sync_call, ...)
```

## Calling command line utilities

- Remember or check what platform you are running on to generate the right CLI commands (e.g. Windows PowerShell vs. Mac ZSH or Linux bash)
- When using python on the command line, always use `uv run python`
- For more complex tasks, create a python script in the `scripts` dir and run it.
