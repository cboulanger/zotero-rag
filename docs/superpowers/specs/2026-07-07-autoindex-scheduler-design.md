# Built-In Auto-Index Scheduler + Admin Controls — Design Spec

## 1. Goal

Replace the operational requirement of configuring an external OS cron job (`0 * * * * podman exec zotero-rag python bin/index_libraries.py ...`, per `docs/cron-indexing.md`) with a scheduler built into the backend itself, controlled by a single new environment variable. This removes a maintenance burden (a crontab entry that lives outside version control, outside the deploy scripts, and is easy to forget when standing up a new server) without changing any of the underlying indexing machinery (`CronIndexer`, `bin/index_libraries.py`, lock/status files all stay as they are).

Alongside this, add authenticated admin controls so the owner/admin of the server's authorizing Zotero group (`AUTHORIZED_GROUP_ID`) can pause/resume the scheduler and abort a stuck or runaway indexing run from the plugin or any HTTP client, without shell access to the host.

Two independent but related pieces:

- **A.** In-process scheduler that periodically triggers the same subprocess-spawn indexing path the on-demand `POST /api/autoindex/run` endpoint already uses.
- **B.** A reusable "is this caller an admin of the authorizing Zotero group" check, and three admin-only endpoints built on it: pause scheduler, resume scheduler, abort running index.

## 2. Scheduler core

### 2.1 New module: `backend/services/autoindex_scheduler.py`

```python
_STARTUP_DELAY_SECONDS = 60

async def run_scheduler_loop(settings: Settings) -> None:
    """Runs forever until cancelled. Ticks every AUTOINDEX_INTERVAL_MINUTES,
    triggering an unscoped (all-targets) indexing run via trigger_index_run().
    """
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if not read_scheduler_state(settings.data_path).get("paused", False):
                result = await trigger_index_run(settings)
                logger.info("Scheduler tick: %s", result)
            else:
                logger.debug("Scheduler tick skipped: paused by admin.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed unexpectedly; will retry next interval.")
        await asyncio.sleep(settings.autoindex_interval_minutes * 60)
```

Wrapping the tick body in `try/except Exception` (re-raising `CancelledError`) is deliberate: a single tick's failure (e.g. a transient exception in `trigger_index_run` itself, not the subprocess it spawns) must not kill the scheduler task permanently — the loop must keep ticking on the configured interval indefinitely.

### 2.2 Shared trigger function

Both the scheduler and `POST /api/autoindex/run` currently need "is a run allowed right now, and if so, spawn it" logic. Today only the endpoint has this (`_spawn_index_run` in `backend/api/autoindex.py`, called after an inline `read_live_status` check in `run_now`). This is extracted into a single shared function so the scheduler doesn't duplicate it:

```python
# backend/services/autoindex_scheduler.py

async def trigger_index_run(settings: Settings, fingerprint: Optional[str] = None) -> Literal["started", "already_running", "disabled"]:
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        return "disabled"
    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if live_status.get("running"):
        return "already_running"
    await _spawn_index_run(settings, fingerprint)
    return "started"


async def _spawn_index_run(settings: Settings, fingerprint: Optional[str]) -> None:
    log_path = settings.data_path / "logs" / "cron_indexer.log"
    script_path = _PROJECT_ROOT / "bin" / "index_libraries.py"
    args = [sys.executable, str(script_path)]
    if fingerprint:
        args += ["--fingerprint", fingerprint]

    def _open_log():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(log_path, "ab")

    logf = await asyncio.to_thread(_open_log)
    try:
        await asyncio.create_subprocess_exec(*args, stdout=logf, stderr=logf, cwd=str(_PROJECT_ROOT))
    finally:
        await asyncio.to_thread(logf.close)
```

`backend/api/autoindex.py`'s `run_now` handler is simplified to call `trigger_index_run(settings, fingerprint=fp)` and map the result to HTTP responses:
- `"disabled"` → unreachable in practice (the earlier `_store()` call already 503s), kept only for type completeness.
- `"already_running"` → `409` `"Indexing is already running on the server."` (same message as today).
- `"started"` → `{"started": True}` (unchanged).

The scheduler calls `trigger_index_run(settings)` with no fingerprint (all resolvable targets, same as the OS-cron invocation today).

### 2.3 Wiring into `main.py`

In `lifespan()`, after the existing `VectorStore` startup block:

```python
scheduler_task: Optional[asyncio.Task] = None
if settings.autoindex_interval_minutes:
    scheduler_task = asyncio.create_task(run_scheduler_loop(settings))
    app.state.autoindex_scheduler_task = scheduler_task

yield

if scheduler_task is not None:
    scheduler_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler_task
```

No task is created at all when `AUTOINDEX_INTERVAL_MINUTES` is unset — existing OS-cron deployments are unaffected unless the operator opts in.

### 2.4 Configuration

New `Settings` field in `backend/config/settings.py`:

```python
autoindex_interval_minutes: Optional[int] = Field(
    default=None,
    gt=0,
    description="If set, the backend runs its own in-process scheduler that "
                "triggers an auto-index run every N minutes, instead of "
                "relying on an external OS cron job. Unset (default) leaves "
                "scheduling entirely to the operator (see docs/cron-indexing.md)."
)
```

Pydantic's `gt=0` constraint rejects `0`/negative values at settings-construction time (server refuses to start with a bad value, consistent with how other misconfigured settings already fail fast).

`.env.dist`, in the existing "Automatic (cron) Indexing" section, next to `AUTOINDEX_SECRET`:

```env
# Built-in scheduler: if set, the backend indexes automatically every N minutes
# instead of requiring an external cron job. Recommended: 60 (hourly).
# AUTOINDEX_INTERVAL_MINUTES=60
```

If `AUTOINDEX_INTERVAL_MINUTES` is set but `AUTOINDEX_SECRET` is not, the scheduler task still starts (so it activates automatically the moment a later redeploy adds the secret, without needing a second restart) — each tick's `trigger_index_run` simply returns `"disabled"` and logs at `DEBUG`, not `WARNING`, to avoid log spam on a deliberately half-configured instance.

### 2.5 Multi-worker behavior

No leader election is added. Every worker process runs its own scheduler loop; on each tick, every worker calls `trigger_index_run`, which checks `read_live_status()` before spawning — only the first worker to observe `running: false` on a given tick actually spawns a subprocess, the rest see `"already_running"` and skip. Production defaults to a single worker (`Dockerfile`/`docker-compose.yml` both default `UVICORN_WORKERS=1`, and multi-worker requires `QDRANT_URL` server mode per `.env.dist`); this only matters at all for that non-default configuration, and the cost there is a handful of extra `read_live_status()` file reads per tick, not extra subprocess spawns.

### 2.6 Observability

`GET /api/autoindex/status` and the unauthenticated `GET /` `cron_indexing` block both gain a `scheduler` sub-object:

```json
"cron_indexing": {
  "enabled": true,
  "scheduler": { "active": true, "interval_minutes": 60, "paused": false }
}
```

`active` reflects whether `AUTOINDEX_INTERVAL_MINUTES` is set (task was created) — independent of `enabled` (whether `AUTOINDEX_SECRET` is set) — so operators can tell "scheduler wired up but waiting on secret" apart from "not configured at all." `paused` reflects the persisted admin pause state (§3.4); absent/`false` when no state file exists.

## 3. Admin controls

### 3.1 Reusable group-admin check

New module `backend/zotero/group_roles.py`, following the existing `key_validator.py` style (`aiohttp`, `Zotero-API-Version: 3` header):

```python
ZOTERO_API_BASE = "https://api.zotero.org"

async def is_group_admin(user_id: int, group_id: int, api_key: str, base_url: str = ZOTERO_API_BASE) -> bool:
    """True if user_id is the owner or an admin of the given Zotero group.

    Relies on Zotero's own computed `meta.isAdmin` field on GET /groups/<id>,
    which is populated only when the request is authenticated with a key
    belonging to that user (confirmed live: an unauthenticated call to the
    same endpoint omits `meta.isAdmin` entirely). Fails closed (False) on any
    non-200 response, including 403/404 for a group the caller can't see.
    """
    async with aiohttp.ClientSession(headers={
        "Zotero-API-Version": "3",
        "Zotero-API-Key": api_key,
    }) as session:
        try:
            async with session.get(f"{base_url}/groups/{group_id}") as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
        except aiohttp.ClientError:
            return False
    return bool(data.get("meta", {}).get("isAdmin", False))
```

Verified live against the real Zotero API (using the `ZOTERO_API_KEY` already in `.env`): `GET /groups/<id>` authenticated with the caller's own key returns `meta.isAdmin: true` for a group the caller owns *and* for one where the caller is only a listed admin (not the `data.owner`); the same call unauthenticated omits `meta.isAdmin` from the response entirely. This confirms `isAdmin` is computed relative to the authenticated caller and already encodes the owner-or-admin semantics this feature needs, with no manual comparison against `data.owner`/`data.admins` required.

### 3.2 Caching

`AdminRoleCache` in the same module, mirroring `ZoteroIdentityCache`'s (`backend/services/zotero_identity.py`) TTL-cache pattern: keyed by `(user_id, group_id)`, 300s TTL, so repeated polling from an admin UI doesn't hit the Zotero API on every request. No stale-serving-on-error behavior (unlike the identity cache) — an admin check should fail closed on a Zotero API hiccup rather than serve a possibly-stale "yes."

### 3.3 Authorization dependency

New dependency in `backend/dependencies.py`:

```python
async def require_authorized_group_admin(request: Request) -> Optional[ZoteroIdentity]:
    settings = get_settings()
    if is_loopback(settings):
        # Loopback deployments already skip the whole identity/gate mechanism
        # (see resolve_zotero_identity) on the premise that localhost access
        # implies shell access to the host anyway; admin controls follow the
        # same trust boundary rather than introducing a stricter one just for
        # these three routes.
        return None
    if not settings.authorized_group_id:
        raise HTTPException(status_code=503, detail="Admin controls require AUTHORIZED_GROUP_ID to be configured.")
    identity = request.state.zotero_identity  # always populated here: non-loopback + reached middleware
    api_key = request.headers.get("X-Zotero-API-Key", "")
    if not await get_admin_role_cache().is_admin(identity.user_id, settings.authorized_group_id, api_key):
        raise HTTPException(status_code=403, detail="This Zotero account is not an admin of the authorizing group.")
    return identity
```

Used only by the three new routes in §3.4 — every other existing `/api/*` route is unaffected. Note the 503 for a missing `AUTHORIZED_GROUP_ID` only applies to non-loopback deployments; a loopback dev server (`npm start` against `localhost`) can always pause/resume/abort, matching how it already bypasses the rest of the identity gate.

### 3.4 New endpoints (`backend/api/autoindex.py`)

All three depend on `require_authorized_group_admin`:

- **`POST /api/autoindex/scheduler/pause`** — writes `{"paused": true}` to the state file (§3.5). Returns `{"paused": true}`.
- **`POST /api/autoindex/scheduler/resume`** — writes `{"paused": false}`. Returns `{"paused": false}`.
- **`POST /api/autoindex/abort`** — reads `cron_status.json`; if `running` is not `True`, `409` `"No indexing run is currently active."`. Otherwise reads `pid`, calls `abort_process(pid)` (§3.6), and returns `{"aborted": true, "pid": pid}`.

### 3.5 Pause-state persistence

New file `<data_path>/system/autoindex_scheduler_state.json`, shape `{"paused": bool}`, written atomically via the same `tempfile.mkstemp` + `os.replace` pattern already used by `CronIndexer._write_status`. A small helper pair in `autoindex_scheduler.py`:

```python
def read_scheduler_state(data_path: Path) -> dict:
    state_path = data_path / "system" / "autoindex_scheduler_state.json"
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def write_scheduler_state(data_path: Path, state: dict) -> None:
    # same atomic tempfile + os.replace pattern as CronIndexer._write_status
    ...
```

Missing file (no admin has ever paused/resumed) is treated as `paused: false` — today's implicit "the scheduler always runs" behavior is the default. This state persists across restarts/redeploys, so a deliberate "stop indexing" during an incident survives the hotfix workflow described in `CLAUDE.md` (which restarts the container to apply changes).

### 3.6 Abort mechanism

New helper in `backend/services/cron_indexer.py`, next to the existing `is_process_alive()`:

```python
def abort_process(pid: int) -> bool:
    """Send a termination signal to a running cron-indexer process.

    Returns False if the process was already gone. Uses the same POSIX/Windows
    branching as is_process_alive(): SIGTERM on POSIX (kernel releases the
    process's flock automatically, exactly as on a crash), TerminateProcess
    via os.kill(pid, signal.SIGTERM) on Windows (Python maps this to
    TerminateProcess for non-Python-created handles).
    """
    if not is_process_alive(pid):
        return False
    os.kill(pid, signal.SIGTERM)
    return True
```

No new crash-recovery logic is needed: a terminated run leaves the lock file in exactly the same state as a crash (holding process gone, flock released by the kernel). The existing stale-lock takeover in `CronIndexer._acquire_lock()` already detects this on the *next* run and forces a full re-index of whichever slug was `"indexing"` when the process died — an aborted run is handled identically to today's crash-recovery path, no special-casing required. `read_live_status()` similarly already reports `running: false, crashed: true` once the aborted PID is no longer alive.

## 4. Docs updates

- **`docs/cron-indexing.md`**: rewrite "Setting Up a Scheduled Job" to present `AUTOINDEX_INTERVAL_MINUTES` as the primary path for both local installs and containers (no crontab entry needed). Keep a short "Alternative: external scheduler" subsection for anyone who wants per-slug control via `--fingerprint`/`--force` outside the built-in scheduler's unscoped ticks. Add a new "Admin Controls" section documenting the three admin endpoints, the `AUTHORIZED_GROUP_ID` requirement, and the owner-or-admin semantics.
- **`CLAUDE.md`**: in "Debugging the cron indexer," replace the `/etc/cron.d/zotero-rag-indexer` enable/disable `sed` snippets with "set/unset `AUTOINDEX_INTERVAL_MINUTES` in the deploy env file and restart the service" (or, for a live server, `curl -X POST .../api/autoindex/scheduler/pause` as an admin, for a change that doesn't require a restart). Everything else in that section (log file location, lock file, `key_issues`, memory tuning) is unchanged since it's still produced by the same `index_libraries.py`/`CronIndexer` code path regardless of what triggers it.

## 5. Testing

- `backend/tests/test_settings_access_gate.py` or a new settings test: `autoindex_interval_minutes` validator rejects `0`/negative, accepts positive ints, defaults to `None`.
- New `backend/tests/test_autoindex_scheduler.py`:
  - `trigger_index_run` returns `"disabled"` / `"already_running"` / `"started"` correctly (mock `AutoIndexKeyStore.enabled`, `read_live_status`, and the subprocess spawn).
  - `run_scheduler_loop` performs one tick and then can be cancelled cleanly (short interval, mock `asyncio.sleep` or patch `_STARTUP_DELAY_SECONDS`/interval to near-zero); a tick that raises does not stop subsequent ticks.
  - `read_scheduler_state`/`write_scheduler_state` round-trip; missing file reads as `{"paused": False}`-equivalent default.
- New `backend/tests/test_group_roles.py`:
  - `is_group_admin` returns `True` for a mocked `meta.isAdmin: true` response, `False` for `isAdmin: false`/absent, `False` on non-200 and on `aiohttp.ClientError`.
  - `AdminRoleCache` serves a cached result within its TTL without a second HTTP call, and expires after.
- `backend/tests/test_autoindex_api.py`:
  - `require_authorized_group_admin`: 503 when `AUTHORIZED_GROUP_ID` unset, 403 when not admin, passes through when `is_group_admin` returns `True` (mocked).
  - `POST /api/autoindex/scheduler/pause` / `.../resume`: state file round-trip, non-admin gets 403.
  - `POST /api/autoindex/abort`: 409 when nothing running, success path calls `abort_process` with the PID from `cron_status.json` (mocked).
- No plugin-side automated tests needed for this backend-only spec; if a later plan adds admin UI to the plugin, manual verification (pause/resume buttons, abort button, confirming a non-admin key gets a clear error) would be called for at that point.
