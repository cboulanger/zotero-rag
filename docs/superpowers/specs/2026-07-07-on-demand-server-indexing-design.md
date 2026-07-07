# On-Demand Server-Side Indexing Trigger — Design Spec

## 1. Goal

Two related, small changes to the auto-indexing feature built in the per-user-embedding-key work:

**A.** Let a user trigger a server-side index of *their own* libraries on demand (instead of waiting for the hourly cron), gated on:
   - all necessary keys being present and valid for the current embedding preset, and
   - no client-side or server-side indexing already being in progress.

**B.** When the user clicks "Index" in the main search dialog, if a server-side indexing run is currently in progress, open the auto-indexing status monitor dialog instead of starting a redundant/conflicting client-side indexing run.

Both changes reuse existing infrastructure: the `AutoIndexKeyStore`, `cron_status.json`/lock-file "is a run in progress" mechanism, `bin/index_libraries.py`, and the `autoindex-status` monitor dialog built previously.

## 2. Backend: `POST /api/autoindex/run`

New endpoint in `backend/api/autoindex.py`.

**Request:** no body — the caller is identified by the `X-Zotero-API-Key` header already required by the global auth middleware.

**Behavior:**
1. `_store()` — 503 if `AUTOINDEX_SECRET` is unset (existing helper).
2. Compute `fp = fingerprint(api_key)` from the request's own `X-Zotero-API-Key` header value (the same key used for auth, and the same key registered via `POST /api/autoindex/keys`).
3. If `fp` is not a registered entry in the store → `400` `"You have not registered for automatic indexing yet. Set it up in Preferences first."`
4. If `get_settings().get_hardware_preset().embedding.model_type == "remote"` (i.e. an embedding key is actually required — mirrors the check already in `autoindex_resolver.py`):
   - If the entry has no embedding key, or `embedding_key_status` isn't `"ok"` or `"unverified"` (or is `"rate_limited"` but still within its window) → `400` with a reason string mirroring `autoindex_resolver.py`'s existing reason messages ("Embedding API key is rate-limited until …", etc.)
5. `read_live_status(settings.data_path)` — if `result.get("running")` is `True` → `409` `"Indexing is already running on the server."`
6. Spawn `bin/index_libraries.py` as a detached subprocess:
   ```python
   log_path = settings.data_path / "logs" / "cron_indexer.log"
   log_path.parent.mkdir(parents=True, exist_ok=True)
   with open(log_path, "ab") as logf:
       await asyncio.create_subprocess_exec(
           sys.executable, str(_PROJECT_ROOT / "bin" / "index_libraries.py"),
           "--fingerprint", fp,
           stdout=logf, stderr=logf,
           cwd=str(_PROJECT_ROOT),
       )
   ```
   Fire-and-forget — the endpoint does not await process completion. Returns `{"started": True}`.

**Race note:** two near-simultaneous triggers (this endpoint racing itself, or racing the hourly cron) are handled by the subprocess's own lock acquisition in `CronIndexer._acquire_lock()` — see §3 below, which hardens this to be race-free rather than relying only on the pre-check in step 5.

## 3. Hardening `CronIndexer._acquire_lock()` against concurrent triggers

**Problem:** the existing lock acquisition (`backend/services/cron_indexer.py:166-193`) is a check-then-write race, not atomic:
```python
if self.lock_file.exists():
    ...raise AlreadyRunningError...
self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
```

Today the only caller is the hourly cron, so two runs overlapping is vanishingly rare. This feature turns indexer invocation into a low-latency, user-triggered HTTP action — two users (e.g. sharing a group library) clicking "Run now" within the same few milliseconds is now realistic, and both could see "no lock held," both write the lock file, and both proceed to index concurrently. If they share a library, this is a genuine read-modify-write race on that library's Qdrant metadata (`last_indexed_version`, `total_items_indexed`) and risks corrupted/duplicated chunks; even without a shared library it doubles concurrent embedding/Qdrant load during a window specifically meant to have at most one run active.

**Fix:** replace the manual PID-file check with the `filelock` package (already a dependency; already used by `AutoIndexKeyStore`), using its non-blocking `acquire(timeout=0)` — an OS-level advisory lock (`flock` on POSIX, `msvcrt` on Windows) that is atomic by construction and automatically released by the kernel if the holding process crashes.

```python
from filelock import FileLock, Timeout

class CronIndexer:
    def __init__(self, ...):
        ...
        self._file_lock: Optional[FileLock] = None

    def _acquire_lock(self) -> bool:
        """Atomically acquire the indexer's exclusive run lock.

        Returns True if a stale lock file (left behind by a process that
        crashed mid-run) was found — the caller uses this to force a full
        re-index of any slug that was mid-index when the crash happened.
        """
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        stale = self.lock_file.exists()  # check BEFORE FileLock touches the path
        self._file_lock = FileLock(str(self.lock_file))
        try:
            self._file_lock.acquire(timeout=0)
        except Timeout:
            raise AlreadyRunningError("Cron indexer already running (lock held by another process)")
        if stale:
            self.log.warning("Stale lock file found; previous run was interrupted.")
        self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
        return stale

    def _release_lock(self) -> None:
        try:
            if self._file_lock is not None:
                self._file_lock.release()
        except Exception as exc:
            self.log.warning("Could not release file lock: %s", exc)
        try:
            self.lock_file.unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning("Could not remove lock file: %s", exc)
```

`is_process_alive()` is no longer needed inside `_acquire_lock()` — the OS lock itself is the authoritative liveness check — but stays in use elsewhere (`read_live_status()` still uses it to detect a crashed run for status-reporting purposes, a separate concern from mutual exclusion). This closes the race for cron-vs-cron overlap too, not just the new on-demand trigger.

**Testing:** a new test in `backend/tests/test_cron_indexer.py` that starts two `CronIndexer` instances against the same lock file from two threads/processes and asserts exactly one succeeds and the other raises `AlreadyRunningError` — replacing reliance on the old mock-based `_acquire_lock` tests, which only exercised the sequential (non-racing) path.

## 4. Backend: `bin/index_libraries.py` scoping

New CLI argument:
```python
parser.add_argument(
    "--fingerprint",
    metavar="FP",
    default=None,
    help="Restrict indexing to the auto-index entry with this fingerprint (used by on-demand triggers).",
)
```

After `targets, key_issues = await resolve_targets(store)` (line ~101): if `args.fingerprint` is set, filter:
```python
if args.fingerprint:
    targets = {slug: t for slug, t in targets.items() if t["fingerprint"] == args.fingerprint}
    if not targets:
        log.error("No targets for fingerprint %s; nothing to index for this user.", args.fingerprint)
        return 1
```

No changes to `resolve_targets()`, `CronIndexer`, or the `cron_status.json` schema. `resolve_targets()` still re-validates every registered user's Zotero key on every run (scoped or not) — this is a deliberate simplification: the validation call is cheap (one HTTP call per registered key) and keeps `resolve_targets` free of new parameters; only the expensive part (actual indexing) is scoped to the triggering user.

**Side effect to note:** a scoped run overwrites `cron_status.json` with only the triggering user's slugs for that run (same as how each hourly cron run already replaces the previous run's contents). Other users' last-known status is not visible again until the next full hourly cron run. This matches the existing "status file = last run" semantics and needs no special handling.

## 5. Frontend: "Run now" button

**`plugin/src/autoindex-status.xhtml`:** add a button near the top:
```xml
<html:button id="zotero-rag-run-now">Run indexing now</html:button>
```

**`plugin/src/autoindex-status.js`:**
- `init()`: wire `click` on `#zotero-rag-run-now` to a new `runNow()` method.
- `runNow()`: disable the button, `POST` to `` `${this.plugin.backendURL}/api/autoindex/run` `` with `this.plugin.getAuthHeaders()`. On non-2xx, render the response body's `detail` via the existing banner/problem rendering, then re-enable the button. On success, do nothing further — the existing 5-second `fetchAndRender()` poll picks up the new `running: true` state.
- Extend the per-tick render logic (called from within `fetchAndRender()`) with `updateRunNowButtonState(data)`: disables `#zotero-rag-run-now` and changes its label to "Indexing in progress…" when `data.running === true` **or** `this.plugin.isClientIndexingActive()` is true; otherwise enables it with its normal label.

**Cross-window signal — `isClientIndexingActive()`:**
- `plugin/src/dialog.js`, in `init()`, immediately after the existing `this.plugin = window.arguments[0].plugin;` line, add:
  ```js
  this.plugin._dialogInstance = this;
  ```
  (mirrors the existing pattern where the plugin singleton tracks `_dialogWindow`/`_autoindexStatusWindow`/`_setupWizardWindow`, just pointing at the live dialog object rather than its window.)
- `plugin/src/zotero-rag.js`, on the `ZoteroRAGPlugin` class, add:
  ```js
  isClientIndexingActive() {
      return !!(this._dialogInstance && this._dialogInstance.isOperationInProgress);
  }
  ```

## 6. Frontend: Index-button redirect

`plugin/src/dialog.js`, at the top of `submitIndexOnly()`:

```js
async submitIndexOnly() {
    if (this.plugin && this.plugin.backendURL) {
        const serverRunning = await this.isServerIndexingRunning();
        if (serverRunning) {
            this.plugin.openAutoindexStatusDialog(this.window);
            return;
        }
    }
    // ...existing body unchanged
}
```

New helper:
```js
async isServerIndexingRunning() {
    try {
        const response = await fetch(`${this.plugin.backendURL}/api/autoindex/status`, {
            headers: this.plugin.getAuthHeaders(),
        });
        if (!response.ok) return false;
        const data = await response.json();
        return data.running === true;
    } catch (e) {
        return false; // fail open — a backend hiccup shouldn't block client-side indexing
    }
}
```

Scoped deliberately to `submitIndexOnly()` only — **not** the other two `checkAndMonitorIndexing()` call sites (`reindexLibrary`, and the pre-query auto-reindex flow), since those are implicit/automatic and silently redirecting them to a monitor dialog would be surprising to a user who didn't explicitly click "Index."

Fails open on fetch error or non-2xx, and is a no-op when auto-indexing isn't configured at all (`running` is simply absent/falsy).

**Known edge case (accepted, not fixed):** `running` is a single global flag — the whole system already has exactly one indexing lock. If User B's manual "Run now" is active, User A's "Index" click also redirects to the monitor dialog, which (via the existing per-caller filtering in `GET /api/autoindex/status`) shows empty progress for User A's own libraries until the lock is free. This is the existing single-global-lock architecture, not a new limitation introduced here.

## 7. Testing

- `backend/tests/test_autoindex_api.py`: `POST /api/autoindex/run` — success (mocked subprocess spawn), 400 (unregistered fingerprint), 400 (missing/invalid embedding key on a remote preset), success with no embedding key required (local preset), 409 (already running).
- Coverage for `bin/index_libraries.py`'s `--fingerprint` filtering logic.
- No plugin-side automated tests (consistent with the rest of this codebase's privileged-Zotero-global JS, which has no unit-test harness) — verified manually: register keys, click "Run now" in the monitor dialog and confirm progress bars update; click "Index" in the search dialog while a server run is active and confirm it opens the monitor dialog instead; confirm the "Run now" button disables while client-side indexing is active in the search dialog.
