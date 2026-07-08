# Built-In Auto-Index Scheduler + Admin Controls — Design Spec

## 1. Goal

Replace the operational requirement of configuring an external OS cron job (`0 * * * * podman exec zotero-rag python bin/index_libraries.py ...`, per `docs/cron-indexing.md`) with a scheduler built into the backend itself, controlled by a single new environment variable. This removes a maintenance burden (a crontab entry that lives outside version control, outside the deploy scripts, and is easy to forget when standing up a new server) without changing any of the underlying indexing machinery (`CronIndexer`, `bin/index_libraries.py`, lock/status files all stay as they are).

Alongside this, add authenticated admin controls so the owner/admin of the server's authorizing Zotero group (`AUTHORIZED_GROUP_ID`) can pause/resume the scheduler, abort a stuck or runaway indexing run (or just one job within it), trigger an immediate full run of every registered library ahead of the next scheduled tick, and see every library currently queued or indexing across all users (not just their own) — from the plugin or any HTTP client, without shell access to the host. The plugin's existing auto-index status dialog (the "cron monitor window") surfaces these controls, but only to callers the backend confirms are group admins.

Two independent but related pieces:

- **A.** In-process scheduler that periodically triggers the same subprocess-spawn indexing path the on-demand `POST /api/autoindex/run` endpoint already uses.
- **B.** A reusable "is this caller an admin of the authorizing Zotero group" check, and five admin-only endpoints built on it: pause scheduler, resume scheduler, run a full on-demand index now, abort the whole running process, and skip just one job within an active run. Admins also get an unfiltered, human-labeled view of every job in the current run via the existing status endpoint.

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

All five depend on `require_authorized_group_admin`:

- **`POST /api/autoindex/scheduler/pause`** — writes `{"paused": true}` to the state file (§3.5). Returns `{"paused": true}`.
- **`POST /api/autoindex/scheduler/resume`** — writes `{"paused": false}`. Returns `{"paused": false}`.
- **`POST /api/autoindex/scheduler/run-now`** — triggers an immediate, unscoped indexing run covering *every* registered library, i.e. exactly what the next scheduler tick would do (§2.2), just not waiting for it. Calls `trigger_index_run(settings)` with no fingerprint and maps the result the same way `run_now` already does: `"already_running"` → `409`, `"disabled"` → unreachable (kept for type completeness), `"started"` → `{"started": True}`. Deliberately does **not** reset the scheduler's own interval timer (§2.1) — an admin-triggered run and the next automatic tick are independent; if they land close together the second one simply observes `"already_running"` and skips, same as any two overlapping triggers today.

  This is distinct from the existing `POST /api/autoindex/run` (`backend/api/autoindex.py:165`), which any registered caller (not just admins) can already use to index *their own* libraries only, via `--fingerprint <fp>`. The new endpoint requires admin authorization precisely because it is unscoped — it indexes every user's registered libraries, not just the caller's.
- **`POST /api/autoindex/abort`** — reads `cron_status.json`; if `running` is not `True`, `409` `"No indexing run is currently active."`. Otherwise reads `pid`, calls `abort_process(pid)` (§3.6), and returns `{"aborted": true, "pid": pid}`. Kills the *entire* subprocess — the last-resort option when the process is hung and not cooperating (e.g. stuck in a network call, not reaching the per-item progress-callback checkpoint §3.7 relies on).
- **`POST /api/autoindex/scheduler/skip-slug`** — body `{"slug": "users/12345"}`. Cooperatively skips a single job within the *currently running* indexing process without killing it (§3.7). Reads `cron_status.json`; `409` `"No indexing run is currently active."` if nothing running; `404` `f"{slug!r} is not a pending or in-progress job in the active run."` if the slug isn't present in `status["slugs"]` or its status isn't `"pending"`/`"indexing"` (already `"done"`/`"error"`/`"skipped"` — nothing to skip). Otherwise writes the skip request to the control file and returns `{"skip_requested": true, "slug": slug}` — the caller observes the actual status flip to `"skipped"` on the next `GET /api/autoindex/status` poll, not synchronously in this response (see §3.7's latency note).

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

### 3.7 Per-slug cooperative skip (control file)

Killing the whole process (§3.6) is too blunt when only one library's job is misbehaving (e.g. one user's huge library or a slow embedding API) and every other job in the run is progressing fine. This adds a second, cooperative mechanism: a small control file the live `CronIndexer` subprocess polls at points it already visits, so no signal handling or new IPC transport is needed.

**Control file**: new `<data_path>/system/autoindex_control.json`, shape `{"skip_slug": Optional[str], "requested_at": Optional[str]}`, written atomically via the same `tempfile.mkstemp` + `os.replace` pattern as `_write_status` (`cron_indexer.py:216-231`) — both `json`/`os`/`tempfile`/`Path` are already imported there (`cron_indexer.py:16,18,20,23`). Helper pair added next to `read_scheduler_state`/`write_scheduler_state` (§3.5) in `autoindex_scheduler.py`:

```python
def read_control_state(data_path: Path) -> dict:
    control_path = data_path / "system" / "autoindex_control.json"
    try:
        return json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def write_control_state(data_path: Path, state: dict) -> None:
    ...  # same atomic tempfile + os.replace pattern

def clear_control_state(data_path: Path, matched_slug: str) -> None:
    """Clear the skip request only if it still targets matched_slug — avoids
    clobbering a newer, unrelated request that may have arrived in between."""
    current = read_control_state(data_path)
    if current.get("skip_slug") == matched_slug:
        write_control_state(data_path, {"skip_slug": None, "requested_at": None})
```

**New exception** in `cron_indexer.py`, next to `AlreadyRunningError` (`:51-52`):

```python
class SlugSkipRequested(Exception):
    """Raised to unwind out of indexing the current slug when an admin requests a skip."""
```

**Two checkpoints**, matching the two states a job can be in when an admin asks to skip it:

1. **Queued (`"pending"`)** — checked at the top of the `for slug_info in slug_infos:` loop (`cron_indexer.py:285`), before the slug is marked `"indexing"`:

```python
for slug_info in slug_infos:
    control = read_control_state(get_settings().data_path)
    if control.get("skip_slug") == slug_info.slug:
        status["slugs"][slug_info.slug] = {
            "status": "skipped",
            "skip_reason": "Skipped by admin request",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_status(status)
        clear_control_state(get_settings().data_path, matched_slug=slug_info.slug)
        continue
    status["slugs"][slug_info.slug] = {  # existing block, unchanged
        "status": "indexing", ...
    }
    ...
```

   This case is effectively instant — the check runs once per slug transition, not on a timer.

1. **Currently indexing** — checked inside `progress_callback` (`cron_indexer.py:383-390`), at the same cadence progress is already flushed to disk (`counter["n"] % self.progress_update_interval == 0`), so no new polling cadence is introduced:

```python
def progress_callback(current: int, total: int, chunks_added: int) -> None:
    counter["n"] += 1
    entry = status["slugs"][slug_info.slug]
    entry["items_processed"] = current
    entry["items_total"] = total
    entry["chunks_added"] = chunks_added
    if counter["n"] % self.progress_update_interval == 0:
        self._write_status(status)
        control = read_control_state(get_settings().data_path)
        if control.get("skip_slug") == slug_info.slug:
            raise SlugSkipRequested(slug_info.slug)
```

   `SlugSkipRequested` propagates up through `DocumentProcessor.index_library()` (the callback is invoked synchronously from inside its item loop) and is caught in `_index_slug()`'s existing `except` chain (`:423-451`), added *before* the generic `except Exception:` so it isn't swallowed as a plain error:

```python
except SlugSkipRequested:
    self.log.info("Skip requested by admin for %s; moving to next slug.", slug_info.slug)
    status["slugs"][slug_info.slug]["status"] = "skipped"
    status["slugs"][slug_info.slug]["skip_reason"] = "Skipped by admin request"
    self._write_status(status)
    clear_control_state(get_settings().data_path, matched_slug=slug_info.slug)
    return {"status": "skipped", "skip_reason": "Skipped by admin request"}
```

   Control then returns normally to the `for slug_info in slug_infos:` loop in `run()`, which proceeds to the next slug exactly as it does for today's error/rate-limit skip paths — no change to the outer loop's control flow.

**Latency**: for a currently-indexing slug, the skip takes effect on the *next* `progress_update_interval`-item boundary, not immediately — bounded by however long that many items take to process (same granularity the existing progress bar already updates at, so this isn't a new UX surprise). For a queued slug it's effectively immediate. This is stated explicitly in the endpoint's response (§3.4) rather than pretending the skip is synchronous.

**Whole-run abort (§3.6) still exists independently** — if the process is genuinely stuck (e.g. hung inside a single network call and never reaching a progress-callback checkpoint), the control file is never consulted and only `abort_process()` can recover it. Skip-slug is the "cooperative, no data loss for other jobs" path; abort is the "nothing else is working" path.

### 3.8 Admin-visibility signal on `GET /api/autoindex/status`

The plugin's cron monitor window (§4) needs to know, without an extra round trip, whether the caller should see the admin controls at all. `status()` (`backend/api/autoindex.py:109`) gains an `is_admin: bool` field, computed alongside the existing identity-scoped filtering (line 150 `if identity is not None:`):

```python
if identity is not None:
    if settings.authorized_group_id:
        api_key = request.headers.get("X-Zotero-API-Key", "")
        result["is_admin"] = await get_admin_role_cache().is_admin(
            identity.user_id, settings.authorized_group_id, api_key,
        )
    else:
        result["is_admin"] = False
    ...  # existing slugs/key_issues filtering
else:
    result["is_admin"] = True  # loopback: same trust-boundary bypass as require_authorized_group_admin
```

Reusing `AdminRoleCache` (§3.2, 300s TTL) here is what keeps this affordable: the dialog polls `/status` every 5s (`autoindex-status.js:76`), but that only costs one live Zotero API call per admin per 5 minutes, not per poll. `status()` must become `async def` (it's currently sync `def`) to `await` the cache lookup; the existing synchronous `read_live_status()`/key-store calls inside it are cheap local file reads, not blocking network I/O, so this doesn't reintroduce the event-loop-blocking problem described in `CLAUDE.md`'s FastAPI guidance.

`is_admin` is omitted from the unauthenticated `GET /` root endpoint's `cron_indexing` block — that block is deliberately minimal (§2.6) and has no per-caller identity to evaluate against.

### 3.9 All-jobs admin scope and human-readable job labels

By default `GET /api/autoindex/status` filters `slugs`/`key_issues` down to the caller's own targets (`backend/api/autoindex.py:150-160`) — that's still the right default for a non-admin. Admins additionally need to see every job in the run, labeled with something more useful than a raw slug like `"users/12345"`.

**`scope` query param**: `status(scope: Literal["own", "all"] = "own", ...)`. When `scope == "all"`:

- Requires the caller's already-computed `is_admin` (§3.8) to be `True` — raises `403` `"This Zotero account is not an admin of the authorizing group."` otherwise (same message `require_authorized_group_admin` uses, for consistency). Loopback callers (`is_admin` always `True`) can always use it.
- Skips the identity-scoped filtering entirely — `slugs` and `key_issues` are returned for every target in the run, not just the caller's own.

**Job labels**: each entry in `slugs` gains `library_name` and `owner_id`, joined from `registrations.json` via the existing `RegistrationService.get_all()` (`backend/services/registration_service.py:76-78`) and the slug↔backend-id conversion already used elsewhere in this same file (`cron_indexer.py:28` imports `slug_to_backend_id` from `backend/api/public_query.py:83-93`; `backend_id_to_slug` is the inverse, `:96-104`):

```python
def _job_label(slug: str, registrations: dict) -> tuple[str, Optional[int]]:
    try:
        backend_id = slug_to_backend_id(slug)
    except ValueError:
        return slug, None
    entry = registrations.get(backend_id)
    if not entry:
        return slug, None
    users = entry.get("users") or []
    owner_id = users[0]["user_id"] if users else None
    return entry.get("library_name", slug), owner_id
```

Applied only under `scope == "all"` (own-scope responses already identify "whose" data it is implicitly — the caller). Falls back to the raw slug with `owner_id: null` when there's no matching registration (e.g. a library someone registered for auto-indexing but never separately registered for RAG querying) — this is a real, expected case, not an error, so it degrades gracefully rather than 500ing.

**Known simplification**: `registrations.json` entries carry a `users` list, not a single owner (`registration_service.py:66-71`) — a group library can have multiple registered users with no canonical "owner." `users[0]` (first-registered) is used as a pragmatic stand-in; this is accurate for personal libraries (`users/{id}`, which have exactly one registered user by construction) and an arbitrary but deterministic tie-break for shared group libraries. Revisit if group-library admin oversight turns out to need per-user breakdown rather than a single label.

Example response fragment:

```json
"slugs": {
  "users/12345": { "status": "indexing", "items_processed": 40, "items_total": 120, "chunks_added": 88, "library_name": "My Research Library", "owner_id": 12345 },
  "groups/678":  { "status": "pending", "library_name": "Shared Lab Group", "owner_id": 98765 }
}
```

## 4. Admin UI in the plugin (cron monitor window)

The auto-index status dialog (`plugin/src/autoindex-status.js`, `plugin/src/autoindex-status.xhtml`) — opened via `ZoteroRAGPlugin.openAutoindexStatusDialog()` (`plugin/src/zotero-rag.js:1767`) — is the only existing UI surface for auto-index status, so the admin actions are added there rather than a new window. They stay hidden for everyone except callers the backend reports as `is_admin: true` (§3.8); the plugin never guesses admin status itself, only reflects what the server says.

### 4.1 Markup (`autoindex-status.xhtml`)

A new `admin-controls` block, hidden by default, sits between the existing `run-now-button` and the status banner:

```html
<div id="admin-controls" style="display:none;">
  <button id="admin-run-now-button" type="button" class="dialog-button">Run full index now (all libraries)</button>
  <button id="admin-pause-button" type="button" class="dialog-button">Pause scheduler</button>
  <button id="admin-resume-button" type="button" class="dialog-button" style="display:none;">Resume scheduler</button>
  <button id="admin-abort-button" type="button" class="dialog-button">Abort running index</button>
  <label id="admin-scope-toggle-label" class="admin-scope-toggle">
    <input id="admin-scope-toggle" type="checkbox"/> Show all users' jobs
  </label>
</div>
```

Pause/resume/abort are included here for completeness since §3 already specs their endpoints, but this task's driving requirement is `admin-run-now-button` plus the scope toggle and per-row skip button (§4.4); the other three reuse the identical show/hide and fetch pattern below.

### 4.2 Behavior (`autoindex-status.js`)

- `AutoIndexStatusResponse` typedef (`:26`) gains `@property {boolean} [is_admin]` and, per-slug, `@property {string} [library_name]` / `@property {number} [owner_id]` on `AutoIndexSlugStatus` (`:8-15`) — populated only in `scope=all` responses (§3.9).
- `render()` (`:119`) calls a new `updateAdminControlsVisibility(data)`, which sets `#admin-controls`'s `display` to `''` when `data.is_admin === true`, else `'none'` — evaluated on every poll, so admin status granted/revoked mid-session (e.g. removed from the group) takes effect within one 5s tick without requiring a dialog reopen. When admin controls are hidden this way, `adminScope` (§4.4) is also reset to `'own'` so a revoked admin doesn't get stuck requesting a scope the server will now 403.
- New `runNowAdmin()`, mirroring the existing `runNow()` (`:162`): disables `admin-run-now-button` and sets its text to `Starting…` immediately for feedback, `POST`s to `${backendURL}/api/autoindex/scheduler/run-now` with `plugin.getAuthHeaders()`, on non-`ok` response shows `body.detail` in the banner (a `403` here reads as `"This Zotero account is not an admin of the authorizing group."`, straight from `require_authorized_group_admin`), and on success calls `fetchAndRender()` immediately rather than waiting for the next poll — same pattern the existing `runNow()` uses.
- `updateRunNowButtonState()` (`:150`) already disables the caller's own "Run indexing now" button while `data.running`; `admin-run-now-button` is disabled under the same condition (a second admin-triggered run while one is active would just 409) plus reuses `this.plugin.isClientIndexingActive()` the same way.
- Pause/resume button visibility toggles based on `data.scheduler?.paused` (§2.6's status shape) once the scheduler's own status sub-object is wired up; out of scope to fully spec here since it's shared with §3.4's pause/resume endpoints, not specific to the run-now requirement.

### 4.3 No change to the existing "Run indexing now" button

The pre-existing `run-now-button` (self-scoped, `POST /api/autoindex/run`) is untouched — it remains visible to every registered caller regardless of admin status, since it only ever acts on the caller's own libraries. The new admin control is additive, not a replacement.

### 4.4 All-jobs toggle and per-job skip button

- New instance field `adminScope: 'own' | 'all' = 'own'` on `ZoteroRAGAutoIndexStatus` — starts on the caller's own jobs even for admins, matching the existing dialog's default view; an admin opts into the wider view explicitly.
- `#admin-scope-toggle`'s `change` listener sets `this.adminScope = checkbox.checked ? 'all' : 'own'` and calls `fetchAndRender()` immediately (don't wait for the next 5s tick).
- `fetchAndRender()` (`:83`) appends `?scope=all` to the status URL when `this.adminScope === 'all'`; omitted (defaulting server-side to `'own'`) otherwise.
- `renderLibraries()` (`:216-280`): the row label (`nameSpan.textContent`, currently just `slug`, `:239`) becomes `` `${info.library_name} (${info.owner_id ?? 'unknown owner'})` `` when `info.library_name` is present (i.e. only in `scope=all` responses per §3.9), falling back to the raw `slug` otherwise — no separate scope-tracking needed in this function, it just reacts to whether the field showed up in the payload.
- Each row gains a `Skip this job` button, rendered only when `this.plugin && data.is_admin` and `info.status` is `'pending'` or `'indexing'` (mirrors the server-side validity check in the endpoint, §3.4, so the button doesn't appear for jobs that can't actually be skipped). Click handler `skipSlug(slug)`: `POST`s `${backendURL}/api/autoindex/scheduler/skip-slug` with `{"slug": slug}` as JSON body, disables the button and sets its text to `Skipping…` for feedback, on non-`ok` shows `body.detail` in the banner (404/409 per §3.4), and on success calls `fetchAndRender()` immediately — same feedback-then-refetch pattern as `runNow()`/`runNowAdmin()`. Per §3.7's latency note, the row may still show `"indexing"` for one more poll or two before flipping to `"skipped"` — this isn't treated as an error, just normal cooperative-skip latency.

## 5. Docs updates

- **`docs/cron-indexing.md`**: rewrite "Setting Up a Scheduled Job" to present `AUTOINDEX_INTERVAL_MINUTES` as the primary path for both local installs and containers (no crontab entry needed). Keep a short "Alternative: external scheduler" subsection for anyone who wants per-slug control via `--fingerprint`/`--force` outside the built-in scheduler's unscoped ticks. Add a new "Admin Controls" section documenting the five admin endpoints (pause, resume, run-now, abort, skip-slug), the distinction between whole-process abort and per-job skip (§3.7), the `?scope=all` status parameter and job-label join (§3.9), the `AUTHORIZED_GROUP_ID` requirement, the owner-or-admin semantics, and that all five are also reachable from the plugin's auto-index status dialog for admins.
- **`CLAUDE.md`**: in "Debugging the cron indexer," replace the `/etc/cron.d/zotero-rag-indexer` enable/disable `sed` snippets with "set/unset `AUTOINDEX_INTERVAL_MINUTES` in the deploy env file and restart the service" (or, for a live server, `curl -X POST .../api/autoindex/scheduler/pause` as an admin, for a change that doesn't require a restart, or `curl -X POST .../api/autoindex/scheduler/run-now` to force an immediate full run instead of waiting for the next tick). Everything else in that section (log file location, lock file, `key_issues`, memory tuning) is unchanged since it's still produced by the same `index_libraries.py`/`CronIndexer` code path regardless of what triggers it.

## 6. Testing

- `backend/tests/test_settings_access_gate.py` or a new settings test: `autoindex_interval_minutes` validator rejects `0`/negative, accepts positive ints, defaults to `None`.
- New `backend/tests/test_autoindex_scheduler.py`:
  - `trigger_index_run` returns `"disabled"` / `"already_running"` / `"started"` correctly (mock `AutoIndexKeyStore.enabled`, `read_live_status`, and the subprocess spawn).
  - `run_scheduler_loop` performs one tick and then can be cancelled cleanly (short interval, mock `asyncio.sleep` or patch `_STARTUP_DELAY_SECONDS`/interval to near-zero); a tick that raises does not stop subsequent ticks.
  - `read_scheduler_state`/`write_scheduler_state` round-trip; missing file reads as `{"paused": False}`-equivalent default.
  - `read_control_state`/`write_control_state`/`clear_control_state` round-trip; `clear_control_state` is a no-op when `matched_slug` no longer matches the stored `skip_slug` (a newer request arrived in between); missing file reads as `{}`.
- New/extended `backend/tests/test_cron_indexer.py` (or wherever `CronIndexer` is already tested):
  - A slug whose control-file `skip_slug` matches before it starts is marked `"skipped"`/`"Skipped by admin request"` without `_index_slug` ever being called (queued-skip checkpoint, §3.7 point 1).
  - `progress_callback` raising `SlugSkipRequested` when the control file matches the in-progress slug results in `_index_slug` returning `{"status": "skipped", ...}` and the control file being cleared; the outer `run()` loop proceeds to the next `slug_info` normally (mock `read_control_state` to flip on a given call count to simulate the request landing mid-run).
  - A skip request for a slug *not* in the current run, or arriving after that slug already finished, has no effect (never matches in either checkpoint).
- New `backend/tests/test_group_roles.py`:
  - `is_group_admin` returns `True` for a mocked `meta.isAdmin: true` response, `False` for `isAdmin: false`/absent, `False` on non-200 and on `aiohttp.ClientError`.
  - `AdminRoleCache` serves a cached result within its TTL without a second HTTP call, and expires after.
- `backend/tests/test_autoindex_api.py`:
  - `require_authorized_group_admin`: 503 when `AUTHORIZED_GROUP_ID` unset, 403 when not admin, passes through when `is_group_admin` returns `True` (mocked).
  - `POST /api/autoindex/scheduler/pause` / `.../resume`: state file round-trip, non-admin gets 403.
  - `POST /api/autoindex/scheduler/run-now`: non-admin gets 403; admin gets `{"started": True}` and `trigger_index_run` is called with no fingerprint (mocked, asserting the call omits the scoped `fp` that `POST /api/autoindex/run` passes); `409` when a run is already active.
  - `POST /api/autoindex/abort`: 409 when nothing running, success path calls `abort_process` with the PID from `cron_status.json` (mocked).
  - `POST /api/autoindex/scheduler/skip-slug`: non-admin gets 403; 409 when nothing running; 404 when the slug isn't in the active run or isn't `"pending"`/`"indexing"` (already `"done"`); admin request for a valid pending/indexing slug writes the control file (mocked `write_control_state`) and returns `{"skip_requested": true, "slug": ...}`.
  - `GET /api/autoindex/status`: `is_admin` is `True` on loopback (no identity), reflects the mocked `AdminRoleCache.is_admin` result when identity is present and `AUTHORIZED_GROUP_ID` is set, and is `False` when `AUTHORIZED_GROUP_ID` is unset even with a valid identity.
  - `GET /api/autoindex/status?scope=all`: 403 for a non-admin identity; for an admin, returns unfiltered `slugs`/`key_issues` across all targets (not just the caller's), each `slugs` entry augmented with `library_name`/`owner_id` from a mocked `RegistrationService.get_all()`; a slug with no matching registration falls back to the raw slug string and `owner_id: null` instead of erroring; `scope=own` (or omitted) is unaffected by any of this (regression check against the existing filtering behavior).
- Plugin-side: no Node test-runner coverage planned for DOM-heavy dialog behavior (consistent with the rest of `autoindex-status.js`, which has none today). Manual verification instead:
  - With an admin key, open the auto-index status dialog and confirm `#admin-controls` is visible and "Run full index now" starts a run and disables itself while running; with a non-admin registered key, confirm the block stays hidden; forcing a `403` (e.g. by temporarily removing the caller from the group) should hide the block on the next 5s poll without a full dialog reopen.
  - Toggle "Show all users' jobs" as an admin during an active multi-user run: confirm rows for other users' libraries appear, each labeled `Library Name (owner id)` rather than a raw slug; toggle off and confirm the view reverts to just the admin's own jobs.
  - Click "Skip this job" on a row that is `"indexing"`: confirm the button disables immediately, and within a few 5s polls the row's status flips to `"skipped"`; confirm the button does not render at all on rows already `"done"`/`"error"`/`"skipped"`.
