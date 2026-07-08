# Built-In Auto-Index Scheduler + Admin Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the external OS cron job for auto-indexing with an in-process scheduler, and give owners/admins of the server's authorizing Zotero group (`AUTHORIZED_GROUP_ID`) a full admin control surface — pause/resume the scheduler, trigger an immediate full run, abort the whole process or just one job, and see every user's jobs (not just their own) with human-readable labels — reachable both over HTTP and from the plugin's existing auto-index status dialog.

**Architecture:** A new `backend/services/autoindex_scheduler.py` module owns the scheduler loop and the shared "trigger a run" logic already partially duplicated between the cron script and the on-demand endpoint. A new `backend/zotero/group_roles.py` module provides a cached "is this caller a group admin" check reused by a new `require_authorized_group_admin` FastAPI dependency. `backend/services/cron_indexer.py` gains a small JSON control file it polls at two existing checkpoints (the per-slug loop boundary, and the existing progress-callback flush) so a live indexing subprocess can cooperatively skip one job without being killed. Five new admin endpoints and two additive query/response fields on the existing status endpoint live in `backend/api/autoindex.py`. The plugin's `autoindex-status.js`/`.xhtml` dialog gets admin-only controls that stay hidden unless the backend reports `is_admin: true`.

**Tech Stack:** FastAPI (async routes, `Depends`), Pydantic Settings, `aiohttp` (Zotero API calls), `filelock`/atomic `os.replace` JSON state files (existing pattern), Python `unittest` (`unittest.TestCase` / `unittest.IsolatedAsyncioTestCase`), `aioresponses` for mocking `aiohttp` calls, vanilla DOM JS in the Zotero plugin (no framework).

**Spec:** `docs/superpowers/specs/2026-07-07-autoindex-scheduler-design.md`

**Deliberate deviation from the spec, called out up front:** §3.7 of the spec places `read_control_state`/`write_control_state`/`clear_control_state` in `autoindex_scheduler.py`. That creates a circular import: `autoindex_scheduler.py` needs `read_live_status` from `cron_indexer.py` (for `trigger_index_run`), and `cron_indexer.py` would need the control-state helpers back from `autoindex_scheduler.py` (for the skip checkpoints) — a genuine cycle, not just an ordering nuisance. This plan instead defines the control-state helpers directly in `cron_indexer.py`, right next to the existing `read_live_status`/`_write_status` (which already owns all the other run-state file I/O). `backend/api/autoindex.py` imports `write_control_state` from `cron_indexer.py` for the new skip-slug endpoint. Everything else in the spec is implemented as written.

---

## File Structure

**Create:**
- `backend/services/autoindex_scheduler.py` — scheduler loop, shared trigger function, scheduler pause-state helpers.
- `backend/zotero/group_roles.py` — `is_group_admin`, `AdminRoleCache`.
- `backend/tests/test_autoindex_scheduler.py`
- `backend/tests/test_group_roles.py`

**Modify:**
- `backend/config/settings.py` — new `autoindex_interval_minutes` field.
- `backend/services/cron_indexer.py` — `SlugSkipRequested` exception, `abort_process()`, control-state helpers, two skip checkpoints.
- `backend/dependencies.py` — new `require_authorized_group_admin` dependency.
- `backend/api/autoindex.py` — refactor `run_now`, five new admin endpoints, `is_admin` + `scope=all` + job labels + `scheduler` sub-object on `GET /api/autoindex/status`.
- `backend/main.py` — wire the scheduler task into `lifespan()`; add `scheduler` sub-object to `GET /`.
- `backend/tests/test_autoindex_api.py` — update two subprocess-patch targets after the `run_now` refactor; new tests for the admin endpoints and status fields.
- `backend/tests/test_cron_indexer.py` — new tests for the skip checkpoints and `abort_process`.
- `.env.dist` — document `AUTOINDEX_INTERVAL_MINUTES`.
- `docs/cron-indexing.md` — scheduler as the primary path; new "Admin Controls" section.
- `CLAUDE.md` — update the "Debugging the cron indexer" enable/disable snippets.
- `plugin/src/autoindex-status.xhtml` — admin-controls markup + CSS.
- `plugin/src/autoindex-status.js` — admin visibility, five admin actions, scope toggle, per-row skip button, job labels.

---

### Task 1: Settings field `autoindex_interval_minutes`

**Files:**
- Modify: `backend/config/settings.py:135-140`
- Test: Create `backend/tests/test_autoindex_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_autoindex_scheduler.py`:

```python
"""Unit tests for backend.services.autoindex_scheduler."""

import unittest

from pydantic import ValidationError

from backend.config.settings import Settings


class SettingsValidatorTest(unittest.TestCase):
    def test_autoindex_interval_minutes_defaults_none(self):
        self.assertIsNone(Settings().autoindex_interval_minutes)

    def test_autoindex_interval_minutes_accepts_positive_int(self):
        s = Settings(autoindex_interval_minutes=60)
        self.assertEqual(s.autoindex_interval_minutes, 60)

    def test_autoindex_interval_minutes_rejects_zero(self):
        with self.assertRaises(ValidationError):
            Settings(autoindex_interval_minutes=0)

    def test_autoindex_interval_minutes_rejects_negative(self):
        with self.assertRaises(ValidationError):
            Settings(autoindex_interval_minutes=-5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_scheduler -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'autoindex_interval_minutes'`

- [ ] **Step 3: Add the field**

In `backend/config/settings.py`, insert immediately after the `autoindex_keys_path` field (which ends at line 139, right before the blank line and `qdrant_url` field at line 141):

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_scheduler -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_autoindex_scheduler.py
git commit -m "feat: add AUTOINDEX_INTERVAL_MINUTES setting"
```

---

### Task 2: `cron_indexer.py` — skip exception, abort helper, control-state file I/O

**Files:**
- Modify: `backend/services/cron_indexer.py`
- Test: `backend/tests/test_cron_indexer.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_cron_indexer.py` (new top-level classes, after the existing imports — add `write_control_state`, `read_control_state`, `abort_process` to the import block at the top of the file):

```python
from backend.services.cron_indexer import (
    AlreadyRunningError,
    CronIndexer,
    SlugInfo,
    abort_process,
    clear_control_state,
    is_process_alive,
    read_control_state,
    read_live_status,
    write_control_state,
)
```

Then append these classes at the end of the file (before the final `if __name__ == "__main__":` block if one exists — this file has none, so append at end of file):

```python
class TestAbortProcess(unittest.TestCase):
    def test_returns_false_when_process_already_gone(self):
        with patch("backend.services.cron_indexer.is_process_alive", return_value=False):
            self.assertFalse(abort_process(999999))

    def test_sends_sigterm_when_alive(self):
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True), \
             patch("backend.services.cron_indexer.os.kill") as mock_kill:
            result = abort_process(1234)
        self.assertTrue(result)
        import signal
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)


class TestControlState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_file_reads_empty_dict(self):
        self.assertEqual(read_control_state(self.tmp), {})

    def test_round_trip(self):
        write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})
        self.assertEqual(read_control_state(self.tmp), {"skip_slug": "users/1", "requested_at": "now"})

    def test_clear_removes_matching_request(self):
        write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})
        clear_control_state(self.tmp, matched_slug="users/1")
        self.assertIsNone(read_control_state(self.tmp).get("skip_slug"))

    def test_clear_is_noop_when_slug_no_longer_matches(self):
        """A newer request for a different slug must not be clobbered by a
        stale clear for the slug that was skipped earlier."""
        write_control_state(self.tmp, {"skip_slug": "groups/2", "requested_at": "later"})
        clear_control_state(self.tmp, matched_slug="users/1")
        self.assertEqual(read_control_state(self.tmp).get("skip_slug"), "groups/2")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_cron_indexer -v`
Expected: FAIL with `ImportError: cannot import name 'abort_process'` (and friends)

- [ ] **Step 3: Add `import signal` and the `SlugSkipRequested` exception**

In `backend/services/cron_indexer.py`, add to the imports (after `import os` at line 18):

```python
import os
import signal
```

Add the exception class right after `AlreadyRunningError` (lines 51-52):

```python
class AlreadyRunningError(Exception):
    """Raised when another cron indexer process is already running."""


class SlugSkipRequested(Exception):
    """Raised to unwind out of indexing the current slug when an admin requests a skip."""
```

- [ ] **Step 4: Add `abort_process()` next to `is_process_alive()`**

Insert after `is_process_alive()` ends (line 84), before `read_live_status()` (line 87):

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

- [ ] **Step 5: Add control-state helpers next to `read_live_status()`**

Insert after `read_live_status()` ends (line 102), before `class CronIndexer:` (line 105):

```python
def read_control_state(data_path: Path) -> dict:
    """Return the current admin skip-slug control request, or {} if none."""
    control_path = data_path / "system" / "autoindex_control.json"
    try:
        return json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_control_state(data_path: Path, state: dict) -> None:
    """Atomically write the control-state JSON file (same pattern as CronIndexer._write_status)."""
    control_path = data_path / "system" / "autoindex_control.json"
    control_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=control_path.parent, suffix=".tmp", prefix="autoindex_control_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, control_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def clear_control_state(data_path: Path, matched_slug: str) -> None:
    """Clear the skip request only if it still targets matched_slug — avoids
    clobbering a newer, unrelated request that may have arrived in between."""
    current = read_control_state(data_path)
    if current.get("skip_slug") == matched_slug:
        write_control_state(data_path, {"skip_slug": None, "requested_at": None})
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_cron_indexer -v`
Expected: PASS (all tests, including the pre-existing ones — this step only adds code, it doesn't touch `run()`/`_index_slug()` yet)

- [ ] **Step 7: Commit**

```bash
git add backend/services/cron_indexer.py backend/tests/test_cron_indexer.py
git commit -m "feat: add abort_process and skip-slug control-state file I/O to CronIndexer"
```

---

### Task 3: `cron_indexer.py` — wire the two skip checkpoints into `run()`/`_index_slug()`

**Files:**
- Modify: `backend/services/cron_indexer.py:285-296,383-390,446`
- Test: `backend/tests/test_cron_indexer.py`

- [ ] **Step 1: Write the failing tests**

Add a helper import at the top of `backend/tests/test_cron_indexer.py` (extend the existing import block from Task 2 to also include `SlugSkipRequested`):

```python
from backend.services.cron_indexer import (
    AlreadyRunningError,
    CronIndexer,
    SlugInfo,
    SlugSkipRequested,
    abort_process,
    clear_control_state,
    is_process_alive,
    read_control_state,
    read_live_status,
    write_control_state,
)
```

Append this class (it needs `get_settings` patched to point `data_path` at the same temp dir the indexer's own status/lock files use — the same `self.tmp` passed to `_make_indexer`):

```python
class TestSkipSlug(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    async def test_queued_slug_skipped_before_indexing_starts(self):
        """A skip request for a not-yet-started slug marks it skipped without
        ever calling index_library for it."""
        indexer = _make_indexer(["users/1", "groups/2"], self.tmp)
        write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value={"items_processed": 5, "chunks_added": 10})
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "skipped")
        self.assertEqual(status["slugs"]["users/1"]["skip_reason"], "Skipped by admin request")
        self.assertEqual(status["slugs"]["groups/2"]["status"], "done")
        mock_proc_instance.index_library.assert_awaited_once()  # only groups/2 was ever indexed

    async def test_in_progress_slug_skipped_via_progress_callback(self):
        """A skip request matching the currently-indexing slug raises
        SlugSkipRequested from inside progress_callback; _index_slug catches
        it and marks the slug skipped instead of propagating."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.progress_update_interval = 1  # check the control file on every callback

        async def fake_index_library(**kwargs):
            cb = kwargs.get("progress_callback")
            write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})
            cb(5, 20, 50)  # triggers the control-file check at this interval
            return {"items_processed": 20, "chunks_added": 100}  # unreachable if the skip raises correctly

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(side_effect=fake_index_library)
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "skipped")
        self.assertEqual(status["slugs"]["users/1"]["skip_reason"], "Skipped by admin request")
        self.assertIsNone(read_control_state(self.tmp).get("skip_slug"))  # cleared after being consumed

    async def test_skip_request_for_unrelated_slug_has_no_effect(self):
        """A skip request naming a slug that isn't in this run never matches
        either checkpoint."""
        indexer = _make_indexer(["users/1"], self.tmp)
        write_control_state(self.tmp, {"skip_slug": "groups/999", "requested_at": "now"})

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value={"items_processed": 5, "chunks_added": 10})
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "done")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_cron_indexer.TestSkipSlug -v`
Expected: FAIL — `test_queued_slug_skipped_before_indexing_starts` and `test_in_progress_slug_skipped_via_progress_callback` fail because `status["slugs"]["users/1"]["status"]` is `"done"`, not `"skipped"` (the checkpoints don't exist yet).

- [ ] **Step 3: Add the queued-slug checkpoint in `run()`**

In `backend/services/cron_indexer.py`, replace the loop body opening (lines 285-293):

```python
            for slug_info in slug_infos:
                status["slugs"][slug_info.slug] = {
                    "status": "indexing",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "items_processed": 0,
                    "items_total": 0,
                    "chunks_added": 0,
                }
                self._write_status(status)
                self.log.info("Indexing %s (library_id=%s)", slug_info.slug, slug_info.library_id)
```

with:

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

                status["slugs"][slug_info.slug] = {
                    "status": "indexing",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "items_processed": 0,
                    "items_total": 0,
                    "chunks_added": 0,
                }
                self._write_status(status)
                self.log.info("Indexing %s (library_id=%s)", slug_info.slug, slug_info.library_id)
```

- [ ] **Step 4: Add the mid-slug checkpoint in `progress_callback`**

Replace the `progress_callback` function inside `_index_slug()` (lines 383-390):

```python
        def progress_callback(current: int, total: int, chunks_added: int) -> None:
            counter["n"] += 1
            entry = status["slugs"][slug_info.slug]
            entry["items_processed"] = current
            entry["items_total"] = total
            entry["chunks_added"] = chunks_added
            if counter["n"] % self.progress_update_interval == 0:
                self._write_status(status)
```

with:

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

- [ ] **Step 5: Catch `SlugSkipRequested` in `_index_slug`'s except chain**

In `_index_slug()`, insert a new `except` branch immediately before the final `except Exception as exc:` (line 446), so it is checked before the generic handler:

```python
        except SlugSkipRequested:
            self.log.info("Skip requested by admin for %s; moving to next slug.", slug_info.slug)
            status["slugs"][slug_info.slug]["status"] = "skipped"
            status["slugs"][slug_info.slug]["skip_reason"] = "Skipped by admin request"
            self._write_status(status)
            clear_control_state(get_settings().data_path, matched_slug=slug_info.slug)
            return {"status": "skipped", "skip_reason": "Skipped by admin request"}
        except Exception as exc:
            self.log.error("Error indexing %s: %s", slug_info.slug, exc, exc_info=True)
            status["slugs"][slug_info.slug]["status"] = "error"
            status["slugs"][slug_info.slug]["error"] = str(exc)
            self._write_status(status)
            return {"status": "error", "error": str(exc)}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_cron_indexer -v`
Expected: PASS (all tests, including the full pre-existing suite — the checkpoints only trigger when the control file actually matches the current slug, so untouched runs behave exactly as before)

- [ ] **Step 7: Commit**

```bash
git add backend/services/cron_indexer.py backend/tests/test_cron_indexer.py
git commit -m "feat: cooperative per-slug skip checkpoints in CronIndexer.run()"
```

---

### Task 4: `autoindex_scheduler.py` — shared trigger function

**Files:**
- Create: `backend/services/autoindex_scheduler.py`
- Test: `backend/tests/test_autoindex_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_autoindex_scheduler.py` (add imports at the top first):

```python
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from backend.services.autoindex_scheduler import (
    read_scheduler_state,
    trigger_index_run,
    write_scheduler_state,
)
```

```python
class TriggerIndexRunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(data_path=self.tmp, autoindex_secret=None)

    async def test_returns_disabled_when_secret_unset(self):
        result = await trigger_index_run(self.settings)
        self.assertEqual(result, "disabled")

    async def test_returns_already_running(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={"running": True}):
            result = await trigger_index_run(self.settings)
        self.assertEqual(result, "already_running")

    async def test_spawns_subprocess_when_not_running(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            result = await trigger_index_run(self.settings)
        self.assertEqual(result, "started")
        mock_spawn.assert_awaited_once()

    async def test_unscoped_run_omits_fingerprint_flag(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            await trigger_index_run(self.settings)
        self.assertNotIn("--fingerprint", mock_spawn.await_args.args)

    async def test_scoped_run_includes_fingerprint_flag(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            await trigger_index_run(self.settings, fingerprint="fp-abc")
        args = mock_spawn.await_args.args
        self.assertIn("--fingerprint", args)
        self.assertIn("fp-abc", args)


class SchedulerStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_file_reads_empty_dict(self):
        self.assertEqual(read_scheduler_state(self.tmp), {})

    def test_round_trip(self):
        write_scheduler_state(self.tmp, {"paused": True})
        self.assertEqual(read_scheduler_state(self.tmp), {"paused": True})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_scheduler -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.autoindex_scheduler'`

- [ ] **Step 3: Create the module**

Create `backend/services/autoindex_scheduler.py`:

```python
"""In-process scheduler and pause-state control plane for auto-indexing.

Provides the shared "trigger an indexing run" logic used by both the
in-process scheduler loop (run_scheduler_loop) and the on-demand
POST /api/autoindex/run and POST /api/autoindex/scheduler/run-now endpoints.

Skip-slug control-state helpers live in backend.services.cron_indexer instead
of here, to avoid a circular import: this module needs read_live_status from
cron_indexer, and cron_indexer needs the skip-slug helpers — putting both
directions in the same pair of modules would create a cycle.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Literal, Optional

from backend.config.settings import Settings
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.cron_indexer import read_live_status

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_DELAY_SECONDS = 60


async def trigger_index_run(settings: Settings, fingerprint: Optional[str] = None) -> Literal["started", "already_running", "disabled"]:
    """Start a server-side indexing run if one isn't already active.

    fingerprint=None triggers an unscoped run covering every resolvable
    target (used by the scheduler and the admin run-now endpoint); a
    fingerprint scopes the run to that entry's own targets (used by the
    on-demand POST /api/autoindex/run endpoint).
    """
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


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a small JSON state file (Windows-safe via os.replace).

    Mirrors CronIndexer._write_status's pattern (backend/services/cron_indexer.py).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_scheduler_state(data_path: Path) -> dict:
    """Missing file (no admin has ever paused/resumed) reads as {} — the
    caller treats that as paused=False, today's implicit always-runs default."""
    state_path = data_path / "system" / "autoindex_scheduler_state.json"
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_scheduler_state(data_path: Path, state: dict) -> None:
    _atomic_write_json(data_path / "system" / "autoindex_scheduler_state.json", state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_scheduler -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/autoindex_scheduler.py backend/tests/test_autoindex_scheduler.py
git commit -m "feat: add autoindex_scheduler module with shared trigger_index_run"
```

---

### Task 5: `autoindex_scheduler.py` — `run_scheduler_loop`

**Files:**
- Modify: `backend/services/autoindex_scheduler.py`
- Test: `backend/tests/test_autoindex_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Add to the top imports in `backend/tests/test_autoindex_scheduler.py`:

```python
import asyncio

from backend.services.autoindex_scheduler import (
    _STARTUP_DELAY_SECONDS,
    read_scheduler_state,
    run_scheduler_loop,
    trigger_index_run,
    write_scheduler_state,
)
```

Append:

```python
class RunSchedulerLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_tick_then_cancel(self):
        """One tick fires after the startup delay, then the loop can be
        cancelled cleanly via the next sleep call."""
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock(return_value="started")) as mock_trigger:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        mock_trigger.assert_awaited_once()
        self.assertEqual(calls, [_STARTUP_DELAY_SECONDS, settings.autoindex_interval_minutes * 60])

    async def test_tick_exception_does_not_stop_loop(self):
        """A tick that raises is logged and swallowed, not propagated —
        proven by reaching the second sleep call."""
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        self.assertEqual(len(calls), 2)

    async def test_paused_scheduler_skips_trigger(self):
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        write_scheduler_state(settings.data_path, {"paused": True})
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock()) as mock_trigger:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        mock_trigger.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_scheduler.RunSchedulerLoopTest -v`
Expected: FAIL with `ImportError: cannot import name 'run_scheduler_loop'`

- [ ] **Step 3: Add `run_scheduler_loop`**

In `backend/services/autoindex_scheduler.py`, add after `trigger_index_run` (and before `_spawn_index_run`, or anywhere in the module — placed here to read top-to-bottom as "public API first"):

```python
async def run_scheduler_loop(settings: Settings) -> None:
    """Runs forever until cancelled. Ticks every AUTOINDEX_INTERVAL_MINUTES,
    triggering an unscoped (all-targets) indexing run via trigger_index_run().

    The tick body is wrapped in try/except Exception (re-raising
    CancelledError) deliberately: a single tick's failure (e.g. a transient
    exception in trigger_index_run itself, not the subprocess it spawns)
    must not kill the scheduler task permanently — the loop must keep
    ticking on the configured interval indefinitely.
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_scheduler -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/services/autoindex_scheduler.py backend/tests/test_autoindex_scheduler.py
git commit -m "feat: add run_scheduler_loop"
```

---

### Task 6: Refactor `POST /api/autoindex/run` to use `trigger_index_run`

**Files:**
- Modify: `backend/api/autoindex.py:1-34,165-199,220-235`
- Modify: `backend/tests/test_autoindex_api.py:164,180`

- [ ] **Step 1: Update the two existing tests that patch the old subprocess-spawn location**

In `backend/tests/test_autoindex_api.py`, both occurrences of the following string need their patch target changed (they currently patch `backend.api.autoindex.asyncio.create_subprocess_exec`, which will no longer be called — the spawn now happens inside `backend.services.autoindex_scheduler`):

Find (appears twice, at the current lines 164 and 180):
```python
        with patch("backend.api.autoindex.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
```

Replace both with:
```python
        with patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: FAIL — `test_run_succeeds_on_local_preset_without_embedding_key` and `test_run_succeeds_with_valid_embedding_key_on_remote_preset` fail because `run_now` still calls the old, unpatched `backend.api.autoindex.asyncio.create_subprocess_exec` internally, so a real subprocess spawn is attempted (or the mock is simply never hit and the assertion on `mock_spawn` fails).

- [ ] **Step 3: Refactor `backend/api/autoindex.py`**

Replace the module docstring and imports (lines 1-34) with:

```python
"""Auto-index key endpoints.

POST   /api/autoindex/keys                — submit a read-only key (validated, stored encrypted)
DELETE /api/autoindex/keys                — remove a key
GET    /api/autoindex/keys                — list the caller's own key metadata (no plaintext)
GET    /api/autoindex/status              — live cron-run progress; admins may pass ?scope=all
POST   /api/autoindex/run                 — on-demand run scoped to the caller's own libraries
POST   /api/autoindex/scheduler/pause     — pause the built-in scheduler (admin only)
POST   /api/autoindex/scheduler/resume    — resume the built-in scheduler (admin only)
POST   /api/autoindex/scheduler/run-now   — immediate unscoped run of every library (admin only)
POST   /api/autoindex/scheduler/skip-slug — cooperatively skip one job in the active run (admin only)
POST   /api/autoindex/abort               — kill the entire running indexing process (admin only)

All endpoints are protected by the global Zotero-key auth middleware (X-Zotero-API-Key). When
AUTOINDEX_SECRET is unset the feature is disabled and the key endpoints return 503. The
scheduler/abort/skip-slug endpoints additionally require the caller to be an owner/admin of
AUTHORIZED_GROUP_ID — see backend.dependencies.require_authorized_group_admin.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.dependencies import get_zotero_identity
from backend.services.autoindex_key_store import AutoIndexKeyStore, fingerprint
from backend.services.autoindex_resolver import is_embedding_key_usable
from backend.services.autoindex_scheduler import trigger_index_run
from backend.services.cron_indexer import read_live_status
from backend.services.embedding_key_validator import validate_embedding_key
from backend.services.zotero_identity import ZoteroIdentity
from backend.zotero.key_validator import validate_key

router = APIRouter()
logger = logging.getLogger(__name__)
```

(This removes the now-unused `import sys` and `from pathlib import Path` — both were only needed by `_spawn_index_run`, which moves out of this file in this task. The admin-related imports are added in later tasks, not here, to keep this diff focused on the refactor.)

Replace the `run_now` handler body (lines 165-199) — keep the docstring and the pre-flight checks, replace only the spawn logic:

```python
@router.post("/autoindex/run", summary="Trigger an on-demand indexing run for the caller's own libraries")
async def run_now(request: Request) -> dict:
    """Start a server-side indexing run scoped to the caller's own libraries.

    Spawns bin/index_libraries.py --fingerprint <fp> as a detached subprocess —
    the same script the hourly cron runs — so the caller's own registered
    entry is indexed without waiting for the next cron tick. Refuses to start
    if the caller isn't registered, is missing a usable embedding key (when
    the configured preset requires one), or a run is already in progress.
    """
    store = _store()
    api_key = request.headers.get("X-Zotero-API-Key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Zotero-API-Key header.")
    fp = fingerprint(api_key)

    own = await asyncio.to_thread(_find_own_entry, store, fp)
    if own is None:
        raise HTTPException(
            status_code=400,
            detail="You have not registered for automatic indexing yet. Set it up in Preferences first.",
        )

    settings = get_settings()
    if settings.get_hardware_preset().embedding.model_type == "remote":
        reason = _embedding_key_block_reason(own)
        if reason:
            raise HTTPException(status_code=400, detail=reason)

    result = await trigger_index_run(settings, fingerprint=fp)
    if result == "already_running":
        raise HTTPException(status_code=409, detail="Indexing is already running on the server.")
    return {"started": True}
```

Delete `_spawn_index_run` entirely (it moved to `autoindex_scheduler.py` in Task 4) — remove these lines from the end of the file:

```python
async def _spawn_index_run(settings, fp: str) -> None:
    log_path = settings.data_path / "logs" / "cron_indexer.log"
    script_path = _PROJECT_ROOT / "bin" / "index_libraries.py"

    def _open_log():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(log_path, "ab")

    logf = await asyncio.to_thread(_open_log)
    try:
        await asyncio.create_subprocess_exec(
            sys.executable, str(script_path), "--fingerprint", fp,
            stdout=logf, stderr=logf, cwd=str(_PROJECT_ROOT),
        )
    finally:
        await asyncio.to_thread(logf.close)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: PASS (all tests — including `test_run_rejects_when_already_running`, which needs no code change since `trigger_index_run` preserves the same `read_live_status`-based already-running check)

Also run the full suite to catch any other stale reference:
Run: `uv run python -m unittest backend.tests.test_autoindex_authorization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "refactor: run_now uses shared trigger_index_run"
```

---

### Task 7: Wire the scheduler task into `main.py`'s `lifespan()`

**Files:**
- Modify: `backend/main.py:1-19,81-124`

- [ ] **Step 1: Add imports**

At the top of `backend/main.py`, add `contextlib` and `Optional` (the file currently imports `from contextlib import asynccontextmanager` at line 6, and does not import `typing`):

```python
import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import Optional
```

- [ ] **Step 2: Start and stop the scheduler task in `lifespan()`**

Replace the `lifespan` function body's tail (from the `yield` at line 118 to the end at line 124):

```python
    yield

    logger.info("Shutting down Zotero RAG backend")
    save_item_cache(_cache_path)
    if getattr(app.state, "vector_store", None) is not None:
        app.state.vector_store.close()
        logger.info("VectorStore closed")
```

with a version that starts the scheduler task before `yield` and cancels it after, matching the existing local-import convention this file already uses for auto-index code (see `root()`'s `from backend.services.autoindex_key_store import AutoIndexKeyStore`):

```python
    scheduler_task: Optional[asyncio.Task] = None
    if settings.autoindex_interval_minutes:
        from backend.services.autoindex_scheduler import run_scheduler_loop
        scheduler_task = asyncio.create_task(run_scheduler_loop(settings))
        app.state.autoindex_scheduler_task = scheduler_task
        logger.info(f"Auto-index scheduler started (every {settings.autoindex_interval_minutes} min)")

    yield

    if scheduler_task is not None:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task

    logger.info("Shutting down Zotero RAG backend")
    save_item_cache(_cache_path)
    if getattr(app.state, "vector_store", None) is not None:
        app.state.vector_store.close()
        logger.info("VectorStore closed")
```

- [ ] **Step 3: Verify with the container smoke test**

This changes `backend/main.py`'s startup path, which `CLAUDE.md` explicitly calls out as requiring the container smoke test (not a unit test — spinning up the full app lifespan cleanly is what needs verifying here).

Run: `uv run pytest -m container -v -s`
Expected: PASS (skipped automatically if neither podman nor docker is available; if it runs, it must pass)

Also do a quick manual sanity check without a container:
Run: `AUTOINDEX_INTERVAL_MINUTES=60 uv run python -c "from backend.main import app; print('app import OK')"`
Expected: prints `app import OK` with no exceptions (proves the new import path and settings field don't break module import)

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat: start in-process auto-index scheduler when AUTOINDEX_INTERVAL_MINUTES is set"
```

---

### Task 8: `group_roles.py` — `is_group_admin` + `AdminRoleCache`

**Files:**
- Create: `backend/zotero/group_roles.py`
- Create: `backend/tests/test_group_roles.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_group_roles.py`:

```python
"""Unit tests for backend.zotero.group_roles."""

import unittest

import aiohttp
from aioresponses import aioresponses

from backend.zotero.group_roles import AdminRoleCache, ZOTERO_API_BASE, is_group_admin


class IsGroupAdminTest(unittest.IsolatedAsyncioTestCase):
    async def test_true_when_meta_is_admin_true(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertTrue(result)

    async def test_false_when_meta_is_admin_false_or_absent(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {}})
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertFalse(result)

    async def test_false_on_non_200(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", status=403)
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertFalse(result)

    async def test_false_on_client_error(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", exception=aiohttp.ClientConnectionError("boom"))
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertFalse(result)


class AdminRoleCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_serves_cached_result_within_ttl(self):
        """Only one response is registered — if the cache failed to hold and
        a second real HTTP call were made, aioresponses would raise for the
        unmatched request, failing this test."""
        cache = AdminRoleCache(ttl_seconds=60)
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            first = await cache.is_admin(1, 999, "KEY")
            second = await cache.is_admin(1, 999, "KEY")
        self.assertTrue(first)
        self.assertTrue(second)

    async def test_expires_after_ttl(self):
        """ttl_seconds=0 means every call is a fresh lookup — both registered
        responses must be consumed."""
        cache = AdminRoleCache(ttl_seconds=0)
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            first = await cache.is_admin(1, 999, "KEY")
            second = await cache.is_admin(1, 999, "KEY")
        self.assertTrue(first)
        self.assertTrue(second)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_group_roles -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.zotero.group_roles'`

- [ ] **Step 3: Create the module**

Create `backend/zotero/group_roles.py`:

```python
"""Zotero group admin/owner check, used to gate admin-only auto-index controls.

Relies on Zotero's own computed `meta.isAdmin` field on GET /groups/<id>,
which the API populates only when the request is authenticated with a key
belonging to the caller being checked — see is_group_admin's docstring.
"""

import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"
CACHE_TTL_SECONDS = 300


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


class AdminRoleCache:
    """In-memory TTL cache of is_group_admin results, keyed by (user_id, group_id).

    No stale-serving-on-error behavior (unlike ZoteroIdentityCache) — an
    admin check should fail closed on a Zotero API hiccup rather than serve
    a possibly-stale "yes".
    """

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[int, int], tuple[float, bool]] = {}

    async def is_admin(self, user_id: int, group_id: int, api_key: str) -> bool:
        key = (user_id, group_id)
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        result = await is_group_admin(user_id, group_id, api_key)
        self._entries[key] = (now, result)
        return result

    def clear(self) -> None:
        """Test helper: drop all cached entries."""
        self._entries.clear()


_cache = AdminRoleCache()


def get_admin_role_cache() -> AdminRoleCache:
    """Return the process-wide admin-role cache used by admin-gated routes."""
    return _cache


def reset_admin_role_cache() -> None:
    """Test helper: clear the process-wide cache between test cases."""
    _cache.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_group_roles -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/zotero/group_roles.py backend/tests/test_group_roles.py
git commit -m "feat: add is_group_admin check and AdminRoleCache"
```

---

### Task 9: `require_authorized_group_admin` dependency

**Files:**
- Modify: `backend/dependencies.py:1-71`

No isolated test in this task — `require_authorized_group_admin` has no route to hit yet. Its 503/403/pass-through behavior is exercised end-to-end in Task 10, once the first admin routes exist.

- [ ] **Step 1: Add the import**

In `backend/dependencies.py`, add to the existing import block (after line 20's `from backend.services.zotero_identity import ZoteroIdentity, get_identity_cache`):

```python
from backend.services.zotero_identity import ZoteroIdentity, get_identity_cache
from backend.zotero.group_roles import get_admin_role_cache
```

- [ ] **Step 2: Add the dependency function**

Insert after `get_zotero_identity` (which ends at line 70), before `get_client_api_keys` (line 73):

```python
async def require_authorized_group_admin(request: Request) -> Optional[ZoteroIdentity]:
    """FastAPI dependency gating admin-only auto-index control routes.

    Loopback deployments bypass this (same trust boundary as
    resolve_zotero_identity's Part 4 exception) — localhost access already
    implies shell access to the host. Everywhere else, AUTHORIZED_GROUP_ID
    must be configured and the caller's Zotero key must belong to an
    owner/admin of that group (backend.zotero.group_roles.is_group_admin).
    """
    settings = get_settings()
    if is_loopback(settings):
        return None
    if not settings.authorized_group_id:
        raise HTTPException(status_code=503, detail="Admin controls require AUTHORIZED_GROUP_ID to be configured.")
    identity = request.state.zotero_identity
    api_key = request.headers.get("X-Zotero-API-Key", "")
    if not await get_admin_role_cache().is_admin(identity.user_id, settings.authorized_group_id, api_key):
        raise HTTPException(status_code=403, detail="This Zotero account is not an admin of the authorizing group.")
    return identity
```

- [ ] **Step 3: Sanity-check the module still imports cleanly**

Run: `uv run python -c "import backend.dependencies; print('OK')"`
Expected: prints `OK`

- [ ] **Step 4: Commit**

```bash
git add backend/dependencies.py
git commit -m "feat: add require_authorized_group_admin dependency"
```

---

### Task 10: Admin endpoints — pause + resume

**Files:**
- Modify: `backend/api/autoindex.py`
- Test: `backend/tests/test_autoindex_api.py`

This is the first task that actually exercises `require_authorized_group_admin`, so its tests cover the 503/403/pass-through paths described in the spec's testing section.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_autoindex_api.py`'s imports:

```python
from backend.dependencies import require_authorized_group_admin
from backend.services.autoindex_scheduler import read_scheduler_state
from backend.services.zotero_identity import ZoteroIdentity
```

Append a new test class at the end of the file (before `if __name__ == "__main__":`):

```python
class AdminSchedulerControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        s.authorized_group_id = 999
        self.client = TestClient(app)

    def tearDown(self):
        from backend.main import app as main_app
        main_app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()

    def _override_admin(self, identity):
        from backend.main import app as main_app
        main_app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_pause_requires_authorized_group_id(self):
        get_settings().authorized_group_id = None
        r = self.client.post("/api/autoindex/scheduler/pause", headers={"X-Zotero-API-Key": "K"})
        self.assertEqual(r.status_code, 503)

    def test_pause_rejects_non_admin(self):
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/autoindex/scheduler/pause",
                headers={"X-Zotero-API-Key": "K"},
            )
        # Non-loopback + no real identity resolved by the auth middleware in
        # this unit test yields a 401 before reaching the admin check at all
        # unless identity resolution is also mocked; assert the request is
        # rejected either way (401 unauthenticated or 403 not-admin), never 200.
        self.assertIn(r.status_code, (401, 403))

    def test_pause_admin_writes_state(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/autoindex/scheduler/pause")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"paused": True})
        self.assertTrue(read_scheduler_state(get_settings().data_path)["paused"])

    def test_resume_admin_writes_state(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/autoindex/scheduler/pause")
        r = self.client.post("/api/autoindex/scheduler/resume")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"paused": False})
        self.assertFalse(read_scheduler_state(get_settings().data_path)["paused"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest -v`
Expected: FAIL with 404s — the routes don't exist yet.

- [ ] **Step 3: Add the two endpoints**

In `backend/api/autoindex.py`, extend the imports:

```python
from backend.dependencies import get_zotero_identity, require_authorized_group_admin
from backend.services.autoindex_scheduler import trigger_index_run, write_scheduler_state
```

Add the endpoints after `run_now` (and before `_find_own_entry`):

```python
@router.post("/autoindex/scheduler/pause", summary="Pause the built-in scheduler (admin only)")
async def pause_scheduler(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    await asyncio.to_thread(write_scheduler_state, settings.data_path, {"paused": True})
    return {"paused": True}


@router.post("/autoindex/scheduler/resume", summary="Resume the built-in scheduler (admin only)")
async def resume_scheduler(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    await asyncio.to_thread(write_scheduler_state, settings.data_path, {"paused": False})
    return {"paused": False}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest -v`
Expected: PASS

Run the full file to confirm no regressions:
Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add admin scheduler pause/resume endpoints"
```

---

### Task 11: Admin endpoint — run-now

**Files:**
- Modify: `backend/api/autoindex.py`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `AdminSchedulerControlsTest` in `backend/tests/test_autoindex_api.py`:

```python
    def test_run_now_admin_starts_unscoped_run(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        with patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            r = self.client.post("/api/autoindex/scheduler/run-now")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        mock_spawn.assert_awaited_once()
        self.assertNotIn("--fingerprint", mock_spawn.await_args.args)  # unscoped: every registered library

    def test_run_now_admin_rejects_when_already_running(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 1}), encoding="utf-8")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/run-now")
        self.assertEqual(r.status_code, 409)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest.test_run_now_admin_starts_unscoped_run -v`
Expected: FAIL with 404 — route doesn't exist yet.

- [ ] **Step 3: Add the endpoint**

In `backend/api/autoindex.py`, add after `resume_scheduler`:

```python
@router.post(
    "/autoindex/scheduler/run-now",
    summary="Trigger an immediate full indexing run for every registered library (admin only)",
)
async def run_now_admin(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    result = await trigger_index_run(settings)
    if result == "already_running":
        raise HTTPException(status_code=409, detail="Indexing is already running on the server.")
    return {"started": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add admin run-now endpoint for unscoped full index"
```

---

### Task 12: Admin endpoint — abort

**Files:**
- Modify: `backend/api/autoindex.py`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `AdminSchedulerControlsTest`:

```python
    def test_abort_rejects_when_nothing_running(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/autoindex/abort")
        self.assertEqual(r.status_code, 409)

    def test_abort_calls_abort_process_with_pid(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 4242}), encoding="utf-8")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True), \
             patch("backend.api.autoindex.abort_process", return_value=True) as mock_abort:
            r = self.client.post("/api/autoindex/abort")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"aborted": True, "pid": 4242})
        mock_abort.assert_called_once_with(4242)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest.test_abort_rejects_when_nothing_running -v`
Expected: FAIL with 404 — route doesn't exist yet.

- [ ] **Step 3: Add the endpoint**

In `backend/api/autoindex.py`, extend the `backend.services.cron_indexer` import:

```python
from backend.services.cron_indexer import abort_process, read_live_status
```

Add the endpoint after `run_now_admin`:

```python
@router.post("/autoindex/abort", summary="Abort the entire running indexing process (admin only)")
async def abort_run(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if not live_status.get("running"):
        raise HTTPException(status_code=409, detail="No indexing run is currently active.")
    pid = live_status["pid"]
    aborted = await asyncio.to_thread(abort_process, pid)
    return {"aborted": aborted, "pid": pid}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add admin abort endpoint"
```

---

### Task 13: Admin endpoint — skip-slug

**Files:**
- Modify: `backend/api/autoindex.py`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `AdminSchedulerControlsTest`:

```python
    def _seed_running_status(self, slugs: dict) -> None:
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(
            json.dumps({"running": True, "pid": 1, "slugs": slugs}), encoding="utf-8",
        )

    def test_skip_slug_rejects_when_nothing_running(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 409)

    def test_skip_slug_rejects_unknown_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "indexing"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "groups/999"})
        self.assertEqual(r.status_code, 404)

    def test_skip_slug_rejects_already_done_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "done"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 404)

    def test_skip_slug_writes_control_state_for_indexing_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "indexing"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"skip_requested": True, "slug": "users/1"})
        from backend.services.cron_indexer import read_control_state
        self.assertEqual(read_control_state(get_settings().data_path)["skip_slug"], "users/1")

    def test_skip_slug_accepts_pending_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "pending"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 200)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest.test_skip_slug_rejects_when_nothing_running -v`
Expected: FAIL with 404 — route doesn't exist yet.

- [ ] **Step 3: Add the endpoint**

In `backend/api/autoindex.py`, extend the `backend.services.cron_indexer` import once more:

```python
from backend.services.cron_indexer import abort_process, read_live_status, write_control_state
```

Add a small request model near the top, next to `KeyRequest`:

```python
class SkipSlugRequest(BaseModel):
    slug: str
```

Add the endpoint after `abort_run`:

```python
@router.post(
    "/autoindex/scheduler/skip-slug",
    summary="Cooperatively skip one job in the active run without killing the process (admin only)",
)
async def skip_slug(
    body: SkipSlugRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> dict:
    settings = get_settings()
    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if not live_status.get("running"):
        raise HTTPException(status_code=409, detail="No indexing run is currently active.")
    slug_state = live_status.get("slugs", {}).get(body.slug)
    if slug_state is None or slug_state.get("status") not in ("pending", "indexing"):
        raise HTTPException(
            status_code=404,
            detail=f"{body.slug!r} is not a pending or in-progress job in the active run.",
        )
    from datetime import datetime, timezone
    await asyncio.to_thread(
        write_control_state, settings.data_path,
        {"skip_slug": body.slug, "requested_at": datetime.now(timezone.utc).isoformat()},
    )
    return {"skip_requested": True, "slug": body.slug}
```

(`datetime`/`timezone` are imported locally inside the function, matching this file's established convention of local imports for auto-index-adjacent one-off needs rather than adding another top-level import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.AdminSchedulerControlsTest -v`
Expected: PASS

Run the full autoindex API test file once more to confirm nothing regressed across all five admin endpoints:
Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add admin skip-slug endpoint"
```

---

### Task 14: `GET /api/autoindex/status` — `is_admin` field

**Files:**
- Modify: `backend/api/autoindex.py:109-127`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Append a new test class to `backend/tests/test_autoindex_api.py`:

```python
class StatusAdminFieldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        self.client = TestClient(app)

    def tearDown(self):
        from backend.main import app as main_app
        main_app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()

    def _set_identity(self, identity):
        import backend.dependencies as dependencies
        from backend.main import app as main_app
        main_app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_is_admin_true_on_loopback(self):
        self._set_identity(None)
        r = self.client.get("/api/autoindex/status")
        self.assertTrue(r.json()["is_admin"])

    def test_is_admin_false_without_authorized_group_id(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = None
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/autoindex/status")
        self.assertFalse(r.json()["is_admin"])

    def test_is_admin_reflects_cache_result(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status")
        self.assertTrue(r.json()["is_admin"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.StatusAdminFieldTest -v`
Expected: FAIL with `KeyError: 'is_admin'`

- [ ] **Step 3: Rewrite `status()`**

In `backend/api/autoindex.py`, extend imports:

```python
from backend.zotero.group_roles import get_admin_role_cache
```

Replace the `status` handler (lines 109-162) with:

```python
@router.get("/autoindex/status", summary="Live auto-index cron-run progress")
async def status(
    request: Request,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """Return the live status of the auto-index cron run.

    Unlike the ``/`` root endpoint (which exposes only ``enabled``), this
    authenticated endpoint surfaces the number of registered keys and the last
    run's full progress: whether a run is currently ``running`` (or ``crashed``),
    per-slug counts, timestamps and any ``key_issues`` recorded during the run.
    When the feature is disabled (``AUTOINDEX_SECRET`` unset) ``keys_registered``
    is ``0`` and ``disabled_reason`` explains why. Run-specific fields are absent
    until the first cron run writes a status file.

    On loopback deployments (identity=None) returns full detail unfiltered,
    matching the single-trusted-local-user model used throughout this
    backend's Zotero-key auth. On a gated remote deployment, ``slugs`` is
    filtered to the caller's own readable targets and ``key_issues`` to
    entries matching the caller's own username — this endpoint previously
    leaked every user's real username and every library's slug/stats to any
    caller who merely passed the instance-wide access gate.

    Also reports ``is_admin``: True on loopback, True/False (via the cached
    Zotero group-admin check) when AUTHORIZED_GROUP_ID is configured, False
    when it isn't — the plugin uses this to decide whether to show admin
    controls, without a separate round trip.
    """
    settings = get_settings()
    result: dict = {}
    try:
        store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        result["enabled"] = store.enabled
        if store.enabled:
            result["keys_registered"] = len(await asyncio.to_thread(store.list_metadata))
        else:
            result["keys_registered"] = 0
            result["disabled_reason"] = "AUTOINDEX_SECRET is not set"
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        result["enabled"] = False
        result["keys_registered"] = 0
        result["disabled_reason"] = f"key store error: {exc}"

    try:
        result.update(await asyncio.to_thread(read_live_status, settings.data_path))
    except Exception as exc:
        logger.warning("Failed to read cron status file: %s", exc)

    if identity is not None:
        if settings.authorized_group_id:
            api_key = request.headers.get("X-Zotero-API-Key", "")
            result["is_admin"] = await get_admin_role_cache().is_admin(
                identity.user_id, settings.authorized_group_id, api_key,
            )
        else:
            result["is_admin"] = False
        if "slugs" in result:
            result["slugs"] = {
                slug: info for slug, info in result["slugs"].items()
                if slug in identity.targets
            }
        if "key_issues" in result:
            result["key_issues"] = [
                issue for issue in result["key_issues"]
                if issue.get("user") == identity.username
            ]
    else:
        result["is_admin"] = True  # loopback: same trust-boundary bypass as require_authorized_group_admin

    return result
```

Note this step deliberately does not yet add `?scope=all` or job labels — that's Task 15. It also deliberately wraps `store.list_metadata()` and `read_live_status()` in `asyncio.to_thread`, per `CLAUDE.md`'s "never block the event loop in an `async def` route" rule — `status()` is now `async def` because it needs to `await get_admin_role_cache().is_admin(...)`, a real network call on a cache miss.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: PASS (full file — this confirms the pre-existing filtering tests in `AutoIndexApiTest` still pass with the rewritten, now-`async`, `status()`)

Run: `uv run python -m unittest backend.tests.test_autoindex_authorization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add is_admin field to GET /api/autoindex/status"
```

---

### Task 15: `GET /api/autoindex/status` — `?scope=all` + job labels

**Files:**
- Modify: `backend/api/autoindex.py`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `StatusAdminFieldTest`:

```python
    def _seed_status(self, slugs: dict) -> None:
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(
            json.dumps({"running": False, "slugs": slugs}), encoding="utf-8",
        )

    def _seed_registrations(self, data: dict) -> None:
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "registrations.json").write_text(json.dumps(data), encoding="utf-8")
        get_settings().registrations_path = system_dir / "registrations.json"

    def test_scope_all_rejects_non_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 403)

    def test_scope_all_rejects_when_authorized_group_id_unset(self):
        """Without AUTHORIZED_GROUP_ID configured, is_admin is False even with
        a non-loopback identity override, so scope=all must still 403 —
        there's no admin to be."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "indexing"},
        })
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 403)

    def test_scope_all_admin_sees_every_slug_with_labels(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "indexing"},
        })
        self._seed_registrations({
            "u1": {"library_name": "Alice's Library", "users": [{"user_id": 1, "username": "alice"}]},
            "u2": {"library_name": "Bob's Library", "users": [{"user_id": 2, "username": "bob"}]},
        })
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(set(data["slugs"].keys()), {"users/1", "users/2"})
        self.assertEqual(data["slugs"]["users/1"]["library_name"], "Alice's Library")
        self.assertEqual(data["slugs"]["users/1"]["owner_id"], 1)
        self.assertEqual(data["slugs"]["users/2"]["library_name"], "Bob's Library")
        self.assertEqual(data["slugs"]["users/2"]["owner_id"], 2)

    def test_scope_all_falls_back_to_raw_slug_when_no_registration(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._seed_status({"groups/555": {"status": "pending"}})
        self._seed_registrations({})  # no matching registration for groups/555
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slugs"]["groups/555"]["library_name"], "groups/555")
        self.assertIsNone(data["slugs"]["groups/555"]["owner_id"])

    def test_scope_own_unaffected_by_registrations(self):
        """Regression check: the default (own) scope must not gain
        library_name/owner_id fields or change its filtering behavior."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "done"},
        })
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        r = self.client.get("/api/autoindex/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(set(data["slugs"].keys()), {"users/1"})
        self.assertNotIn("library_name", data["slugs"]["users/1"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.StatusAdminFieldTest -v`
Expected: FAIL — `scope=all` currently 422s (unknown query param is actually just ignored by FastAPI unless declared, so more precisely: the tests checking for `library_name`/`owner_id`/403 fail since `scope` isn't handled at all yet)

- [ ] **Step 3: Add `scope` param and job labels**

In `backend/api/autoindex.py`, extend imports:

```python
from typing import Literal, Optional
...
from backend.api.public_query import slug_to_backend_id
from backend.services.registration_service import RegistrationService
```

Change the `status` signature and the tail of the function (everything from the `if identity is not None:` block onward):

```python
@router.get("/autoindex/status", summary="Live auto-index cron-run progress")
async def status(
    request: Request,
    scope: Literal["own", "all"] = "own",
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """... (docstring from Task 14, extended with:)

    Admins (see require_authorized_group_admin) may pass ?scope=all to see
    every job in the run, not just their own, with each job labeled with its
    library name and owner id (joined from registrations.json). Non-admins
    passing scope=all get a 403.
    """
    settings = get_settings()
    result: dict = {}
    try:
        store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        result["enabled"] = store.enabled
        if store.enabled:
            result["keys_registered"] = len(await asyncio.to_thread(store.list_metadata))
        else:
            result["keys_registered"] = 0
            result["disabled_reason"] = "AUTOINDEX_SECRET is not set"
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        result["enabled"] = False
        result["keys_registered"] = 0
        result["disabled_reason"] = f"key store error: {exc}"

    try:
        result.update(await asyncio.to_thread(read_live_status, settings.data_path))
    except Exception as exc:
        logger.warning("Failed to read cron status file: %s", exc)

    if identity is not None:
        if settings.authorized_group_id:
            api_key = request.headers.get("X-Zotero-API-Key", "")
            result["is_admin"] = await get_admin_role_cache().is_admin(
                identity.user_id, settings.authorized_group_id, api_key,
            )
        else:
            result["is_admin"] = False
    else:
        result["is_admin"] = True  # loopback: same trust-boundary bypass as require_authorized_group_admin

    if scope == "all":
        if not result["is_admin"]:
            raise HTTPException(status_code=403, detail="This Zotero account is not an admin of the authorizing group.")
        if "slugs" in result:
            registrations = await asyncio.to_thread(RegistrationService(settings.registrations_path).get_all)
            for slug, info in result["slugs"].items():
                library_name, owner_id = _job_label(slug, registrations)
                info["library_name"] = library_name
                info["owner_id"] = owner_id
    elif identity is not None:
        if "slugs" in result:
            result["slugs"] = {
                slug: info for slug, info in result["slugs"].items()
                if slug in identity.targets
            }
        if "key_issues" in result:
            result["key_issues"] = [
                issue for issue in result["key_issues"]
                if issue.get("user") == identity.username
            ]

    return result


def _job_label(slug: str, registrations: dict) -> tuple[str, Optional[int]]:
    """Join a slug to its human-readable library name and owner id via registrations.json.

    Falls back to the raw slug / owner_id=None when there's no matching
    registration (e.g. a library registered for auto-indexing but never
    separately registered for RAG querying) — a real, expected case, not
    an error. registrations.json entries carry a `users` list, not a single
    owner; users[0] (first-registered) is used as a pragmatic stand-in —
    exact for personal libraries (users/{id}, which have exactly one
    registered user by construction), an arbitrary but deterministic
    tie-break for shared group libraries.
    """
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api.StatusAdminFieldTest -v`
Expected: PASS

Run the full backend test suite once to confirm no regressions anywhere touching `status()`:
Run: `uv run python -m unittest backend.tests.test_autoindex_api backend.tests.test_autoindex_authorization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add ?scope=all admin view with library name/owner labels to status endpoint"
```

---

### Task 16: Observability — `scheduler` sub-object on `GET /api/autoindex/status` and `GET /`

**Files:**
- Modify: `backend/api/autoindex.py`
- Modify: `backend/main.py:250-264`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `StatusAdminFieldTest`:

```python
    def test_scheduler_subobject_reflects_settings_and_pause_state(self):
        get_settings().autoindex_interval_minutes = 60
        from backend.services.autoindex_scheduler import write_scheduler_state
        write_scheduler_state(get_settings().data_path, {"paused": True})
        self._set_identity(None)
        r = self.client.get("/api/autoindex/status")
        scheduler = r.json()["scheduler"]
        self.assertTrue(scheduler["active"])
        self.assertEqual(scheduler["interval_minutes"], 60)
        self.assertTrue(scheduler["paused"])

    def test_scheduler_subobject_inactive_when_interval_unset(self):
        get_settings().autoindex_interval_minutes = None
        self._set_identity(None)
        r = self.client.get("/api/autoindex/status")
        scheduler = r.json()["scheduler"]
        self.assertFalse(scheduler["active"])
        self.assertFalse(scheduler["paused"])  # no state file written -> defaults False
```

Add a new small test to `AutoIndexApiTest` (in the same file, exercising the unauthenticated root endpoint):

```python
    def test_root_reports_scheduler_subobject(self):
        get_settings().autoindex_interval_minutes = 30
        r = self.client.get("/")
        scheduler = r.json()["cron_indexing"]["scheduler"]
        self.assertTrue(scheduler["active"])
        self.assertEqual(scheduler["interval_minutes"], 30)
        self.assertFalse(scheduler["paused"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: FAIL with `KeyError: 'scheduler'` on the three new tests.

- [ ] **Step 3: Add the sub-object to `status()`**

In `backend/api/autoindex.py`, extend imports:

```python
from backend.services.autoindex_scheduler import read_scheduler_state, trigger_index_run, write_scheduler_state
```

Insert this block into `status()`, right after the `try/except` that sets `result["enabled"]`/`result["keys_registered"]` and before the `try:` that calls `read_live_status`:

```python
    scheduler_state = await asyncio.to_thread(read_scheduler_state, settings.data_path)
    result["scheduler"] = {
        "active": bool(settings.autoindex_interval_minutes),
        "interval_minutes": settings.autoindex_interval_minutes,
        "paused": scheduler_state.get("paused", False),
    }
```

- [ ] **Step 4: Add the sub-object to `GET /` in `main.py`**

In `backend/main.py`, replace the tail of `root()` (lines 254-262):

```python
    from backend.services.autoindex_key_store import AutoIndexKeyStore
    try:
        store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        enabled = store.enabled
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        enabled = False

    response["cron_indexing"] = {"enabled": enabled}
```

with:

```python
    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.services.autoindex_scheduler import read_scheduler_state
    try:
        store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        enabled = store.enabled
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        enabled = False

    response["cron_indexing"] = {
        "enabled": enabled,
        "scheduler": {
            "active": bool(settings.autoindex_interval_minutes),
            "interval_minutes": settings.autoindex_interval_minutes,
            "paused": read_scheduler_state(settings.data_path).get("paused", False),
        },
    }
```

(`root()` is a sync `def`, not `async def` — FastAPI already runs it in a thread pool, so the direct, unwrapped `read_scheduler_state` call here is fine and consistent with the rest of this handler's existing unwrapped `AutoIndexKeyStore`/`vector_store.get_collection_info()` calls.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_autoindex_api -v`
Expected: PASS (full file)

- [ ] **Step 6: Commit**

```bash
git add backend/api/autoindex.py backend/main.py backend/tests/test_autoindex_api.py
git commit -m "feat: surface scheduler active/interval/paused state on status and root endpoints"
```

---

### Task 17: Plugin — admin-controls markup + CSS

**Files:**
- Modify: `plugin/src/autoindex-status.xhtml`

No automated test — this repo has no JS/DOM test coverage for this dialog (confirmed: `autoindex-status.js` has none today). Verified manually in Task 20, once all the JS behavior exists to actually exercise the markup.

- [ ] **Step 1: Add the admin-controls block**

In `plugin/src/autoindex-status.xhtml`, replace:

```html
  <button id="run-now-button" type="button" class="dialog-button">Run indexing now</button>
  <div id="run-banner">Loading status…</div>
```

with:

```html
  <button id="run-now-button" type="button" class="dialog-button">Run indexing now</button>
  <div id="admin-controls" style="display:none;">
    <button id="admin-run-now-button" type="button" class="dialog-button">Run full index now (all libraries)</button>
    <button id="admin-pause-button" type="button" class="dialog-button">Pause scheduler</button>
    <button id="admin-resume-button" type="button" class="dialog-button" style="display:none;">Resume scheduler</button>
    <button id="admin-abort-button" type="button" class="dialog-button">Abort running index</button>
    <label id="admin-scope-toggle-label" class="admin-scope-toggle">
      <input id="admin-scope-toggle" type="checkbox"/> Show all users' jobs
    </label>
  </div>
  <div id="run-banner">Loading status…</div>
```

- [ ] **Step 2: Add CSS for the new block**

In the `<style>` block, add after the existing `.dialog-button:hover` rule:

```css
    .dialog-button:hover { background-color: #e5e5e5; }
    #admin-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
    .admin-scope-toggle { font-size: 12px; color: #333; display: flex; align-items: center; gap: 4px; }
    .library-skip-button { font-size: 11px; padding: 2px 8px; }
```

- [ ] **Step 3: Verify the file is well-formed XHTML**

Run: `uv run python -c "import xml.dom.minidom as m; m.parse('plugin/src/autoindex-status.xhtml'); print('well-formed')"`
Expected: prints `well-formed` (catches unclosed tags before loading it in Zotero)

- [ ] **Step 4: Commit**

```bash
git add plugin/src/autoindex-status.xhtml
git commit -m "feat: add admin-controls markup to auto-index status dialog"
```

---

### Task 18: Plugin — typedefs, init() wiring, fetchAndRender scope param

**Files:**
- Modify: `plugin/src/autoindex-status.js:7-99`

- [ ] **Step 1: Extend the typedefs**

Replace the three typedef blocks at the top of the file (lines 7-36) with:

```js
/**
 * @typedef {Object} AutoIndexSlugStatus
 * @property {string} status - pending|indexing|done|error|skipped
 * @property {number} [items_processed]
 * @property {number} [items_total]
 * @property {number} [chunks_added]
 * @property {string} [error]
 * @property {string} [skip_reason]
 * @property {string} [library_name] - only present in ?scope=all responses (admin)
 * @property {number} [owner_id] - only present in ?scope=all responses (admin)
 */

/**
 * @typedef {Object} AutoIndexKeyIssue
 * @property {string} [user]
 * @property {string} reason
 * @property {boolean} pruned
 * @property {string} [kind]
 */

/**
 * @typedef {Object} AutoIndexStatusResponse
 * @property {boolean} enabled
 * @property {number} keys_registered
 * @property {string} [disabled_reason]
 * @property {boolean} [running]
 * @property {boolean} [crashed]
 * @property {string} [started_at]
 * @property {string} [finished_at]
 * @property {Record<string, AutoIndexSlugStatus>} [slugs]
 * @property {AutoIndexKeyIssue[]} [key_issues]
 * @property {boolean} [is_admin]
 */
```

- [ ] **Step 2: Add the `adminScope` field and new button listeners in `init()`**

Replace the object's field declarations and `init()` (lines 38-77) with:

```js
var ZoteroRAGAutoIndexStatus = {
	/** @type {ZoteroRAGPlugin|null} */
	plugin: null,
	/** @type {number|null} */
	refreshTimer: null,
	/** @type {'own'|'all'} */
	adminScope: 'own',

	/**
	 * Initialize the dialog.
	 * @returns {void}
	 */
	init() {
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (window.arguments && window.arguments[0]) {
			// @ts-ignore
			this.plugin = window.arguments[0].plugin;
		} else {
			console.error('No plugin reference passed to autoindex-status dialog');
			return;
		}

		const closeButton = document.getElementById('close-button');
		if (closeButton) {
			closeButton.addEventListener('click', () => window.close());
		}

		const runNowButton = document.getElementById('run-now-button');
		if (runNowButton) {
			runNowButton.addEventListener('click', () => this.runNow());
		}

		const adminRunNowButton = document.getElementById('admin-run-now-button');
		if (adminRunNowButton) {
			adminRunNowButton.addEventListener('click', () => this.runNowAdmin());
		}

		const adminPauseButton = document.getElementById('admin-pause-button');
		if (adminPauseButton) {
			adminPauseButton.addEventListener('click', () => this.pauseScheduler());
		}

		const adminResumeButton = document.getElementById('admin-resume-button');
		if (adminResumeButton) {
			adminResumeButton.addEventListener('click', () => this.resumeScheduler());
		}

		const adminAbortButton = document.getElementById('admin-abort-button');
		if (adminAbortButton) {
			adminAbortButton.addEventListener('click', () => this.abortRun());
		}

		const adminScopeToggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-scope-toggle'));
		if (adminScopeToggle) {
			adminScopeToggle.addEventListener('change', () => {
				this.adminScope = adminScopeToggle.checked ? 'all' : 'own';
				this.fetchAndRender();
			});
		}

		window.addEventListener('unload', () => {
			if (this.refreshTimer !== null) {
				clearInterval(this.refreshTimer);
				this.refreshTimer = null;
			}
		});

		this.fetchAndRender();
		this.refreshTimer = setInterval(() => this.fetchAndRender(), 5000);
	},
```

- [ ] **Step 3: Add `?scope=all` to `fetchAndRender()`**

Replace `fetchAndRender()` (lines 83-99, in the pre-edit file):

```js
	async fetchAndRender() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/status`, {
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				this.renderBanner(`Error: could not load status (HTTP ${response.status}).`, 'crashed');
				return;
			}
			/** @type {AutoIndexStatusResponse} */
			const data = await response.json();
			this.render(data);
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},
```

with:

```js
	async fetchAndRender() {
		if (!this.plugin) return;
		try {
			const url = this.adminScope === 'all'
				? `${this.plugin.backendURL}/api/autoindex/status?scope=all`
				: `${this.plugin.backendURL}/api/autoindex/status`;
			const response = await fetch(url, {
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				this.renderBanner(`Error: could not load status (HTTP ${response.status}).`, 'crashed');
				return;
			}
			/** @type {AutoIndexStatusResponse} */
			const data = await response.json();
			this.render(data);
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},
```

- [ ] **Step 4: Verify the file still parses as valid JS**

Run: `node --check plugin/src/autoindex-status.js`
Expected: no output, exit code 0

- [ ] **Step 5: Commit**

```bash
git add plugin/src/autoindex-status.js
git commit -m "feat: wire admin button listeners and scope query param in autoindex-status dialog"
```

---

### Task 19: Plugin — `render()`, `updateAdminControlsVisibility`, and the five admin action methods

**Files:**
- Modify: `plugin/src/autoindex-status.js`

- [ ] **Step 1: Call `updateAdminControlsVisibility` from `render()`**

Replace the end of `render()` (the pre-edit file's lines 139-142):

```js
		this.renderLibraries(data.slugs || {});
		this.renderProblems(data.key_issues || []);
		this.updateRunNowButtonState(data);
	},
```

with:

```js
		this.renderLibraries(data.slugs || {}, data.is_admin === true);
		this.renderProblems(data.key_issues || []);
		this.updateRunNowButtonState(data);
		this.updateAdminControlsVisibility(data);
	},
```

- [ ] **Step 2: Add the six new methods**

Insert these new methods immediately after `updateRunNowButtonState()` (which ends right before the existing `runNow()` method in the pre-edit file):

```js
	/**
	 * Show/hide the admin-only controls block based on the server-reported
	 * is_admin flag. Runs on every poll so admin status granted/revoked
	 * mid-session takes effect within one tick.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	updateAdminControlsVisibility(data) {
		const block = document.getElementById('admin-controls');
		if (!block) return;
		const isAdmin = data.is_admin === true;
		block.style.display = isAdmin ? '' : 'none';
		if (!isAdmin) {
			this.adminScope = 'own';
			const toggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-scope-toggle'));
			if (toggle) toggle.checked = false;
		}
	},

	/**
	 * Trigger an immediate, unscoped indexing run covering every registered
	 * library (admin only).
	 * @returns {Promise<void>}
	 */
	async runNowAdmin() {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement} */ (document.getElementById('admin-run-now-button'));
		if (button) {
			button.disabled = true;
			button.textContent = 'Starting…';
		}
		this.renderBanner('Starting full index…', 'running');
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/run-now`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					button.textContent = 'Run full index now (all libraries)';
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
			if (button) {
				button.disabled = false;
				button.textContent = 'Run full index now (all libraries)';
			}
		}
	},

	/**
	 * Pause the built-in scheduler (admin only).
	 * @returns {Promise<void>}
	 */
	async pauseScheduler() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/pause`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not pause scheduler (HTTP ${response.status}).`, 'crashed');
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Resume the built-in scheduler (admin only).
	 * @returns {Promise<void>}
	 */
	async resumeScheduler() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/resume`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not resume scheduler (HTTP ${response.status}).`, 'crashed');
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Abort the entire running indexing process (admin only).
	 * @returns {Promise<void>}
	 */
	async abortRun() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/abort`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not abort run (HTTP ${response.status}).`, 'crashed');
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Cooperatively skip a single job in the active run without killing the
	 * whole process (admin only).
	 * @param {string} slug
	 * @returns {Promise<void>}
	 */
	async skipSlug(slug) {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement|null} */ (document.querySelector(`[data-skip-slug="${slug}"]`));
		if (button) {
			button.disabled = true;
			button.textContent = 'Skipping…';
		}
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/skip-slug`, {
				method: 'POST',
				headers: { ...this.plugin.getAuthHeaders(), 'Content-Type': 'application/json' },
				body: JSON.stringify({ slug }),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not skip job (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					button.textContent = 'Skip this job';
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},
```

- [ ] **Step 3: Verify the file still parses as valid JS**

Run: `node --check plugin/src/autoindex-status.js`
Expected: no output, exit code 0

- [ ] **Step 4: Commit**

```bash
git add plugin/src/autoindex-status.js
git commit -m "feat: add admin action methods (run-now, pause, resume, abort, skip-slug) to autoindex-status dialog"
```

---

### Task 20: Plugin — `renderLibraries()` job labels and per-row skip button

**Files:**
- Modify: `plugin/src/autoindex-status.js`

- [ ] **Step 1: Replace `renderLibraries()`**

Replace the entire method (pre-edit file's lines 211-280, `renderLibraries(slugs) { ... }`) with:

```js
	/**
	 * Render one row per library with a progress bar reflecting its status.
	 * @param {Record<string, AutoIndexSlugStatus>} slugs
	 * @param {boolean} [isAdmin]
	 * @returns {void}
	 */
	renderLibraries(slugs, isAdmin = false) {
		const container = document.getElementById('libraries-container');
		const emptyState = document.getElementById('empty-state');
		if (!container || !emptyState) return;
		container.innerHTML = '';

		const slugNames = Object.keys(slugs);
		if (slugNames.length === 0) {
			emptyState.style.display = '';
			return;
		}
		emptyState.style.display = 'none';

		for (const slug of slugNames.sort()) {
			const info = slugs[slug];
			const row = document.createElement('div');
			row.className = 'library-row';

			const header = document.createElement('div');
			header.className = 'library-row-header';

			const nameSpan = document.createElement('span');
			nameSpan.className = 'library-name';
			nameSpan.textContent = info.library_name
				? `${info.library_name} (${info.owner_id ?? 'unknown owner'})`
				: slug;
			header.appendChild(nameSpan);

			const badge = document.createElement('span');
			badge.className = `library-status-badge ${info.status}`;
			badge.textContent = info.status;
			header.appendChild(badge);

			if (isAdmin && (info.status === 'pending' || info.status === 'indexing')) {
				const skipButton = document.createElement('button');
				skipButton.type = 'button';
				skipButton.className = 'dialog-button library-skip-button';
				skipButton.textContent = 'Skip this job';
				skipButton.dataset.skipSlug = slug;
				skipButton.addEventListener('click', () => this.skipSlug(slug));
				header.appendChild(skipButton);
			}

			row.appendChild(header);

			const progress = /** @type {HTMLProgressElement} */ (document.createElement('progress'));
			progress.className = 'library-progress';
			if (info.items_total) {
				progress.max = info.items_total;
				progress.value = info.items_processed || 0;
			} else {
				progress.removeAttribute('value');
			}
			row.appendChild(progress);

			const meta = document.createElement('div');
			meta.className = 'library-meta';
			const parts = [];
			if (typeof info.items_processed === 'number' && typeof info.items_total === 'number') {
				parts.push(`${info.items_processed} / ${info.items_total} items`);
			}
			if (typeof info.chunks_added === 'number') {
				parts.push(`${info.chunks_added} chunks added`);
			}
			meta.textContent = parts.join(' — ');
			row.appendChild(meta);

			if (info.error || info.skip_reason) {
				const errorDiv = document.createElement('div');
				errorDiv.className = 'library-error';
				errorDiv.textContent = info.error || info.skip_reason || '';
				row.appendChild(errorDiv);
			}

			container.appendChild(row);
		}
	},
```

- [ ] **Step 2: Verify the file still parses as valid JS**

Run: `node --check plugin/src/autoindex-status.js`
Expected: no output, exit code 0

- [ ] **Step 3: Manual verification in the running Zotero dev environment**

This dialog has no automated JS tests (established in Task 17); the spec's own testing section calls for manual verification here. With the plugin dev server running (`npm run start` — do not rebuild) and the backend running with `AUTHORIZED_GROUP_ID` set:

1. Open the auto-index status dialog with a Zotero key belonging to a group admin. Confirm `#admin-controls` becomes visible and "Run full index now (all libraries)" starts a run, disabling itself while running.
2. Open it with a non-admin registered key. Confirm the admin block stays hidden.
3. While an admin session's dialog is open, toggle "Show all users' jobs" during an active multi-user run. Confirm other users' library rows appear, each labeled `Library Name (owner id)` rather than a raw slug; toggle off and confirm the view reverts to the admin's own jobs only.
4. Click "Skip this job" on a row that is `indexing`. Confirm the button disables immediately and, within a few 5s polls, the row's status flips to `skipped`. Confirm the button does not render on rows already `done`/`error`/`skipped`.

- [ ] **Step 4: Commit**

```bash
git add plugin/src/autoindex-status.js
git commit -m "feat: show admin job labels and per-row skip button in autoindex-status dialog"
```

---

### Task 21: Docs — `.env.dist`

**Files:**
- Modify: `.env.dist:186-190`

- [ ] **Step 1: Add the new variable's documentation**

In `.env.dist`, replace:

```env
# Keep this value stable — rotating it makes all previously stored keys
# undecryptable.
#
# AUTOINDEX_SECRET=your_generated_fernet_key
```

with:

```env
# Keep this value stable — rotating it makes all previously stored keys
# undecryptable.
#
# AUTOINDEX_SECRET=your_generated_fernet_key

# Built-in scheduler: if set, the backend indexes automatically every N minutes
# instead of requiring an external cron job. Recommended: 60 (hourly).
# AUTOINDEX_INTERVAL_MINUTES=60
```

- [ ] **Step 2: Commit**

```bash
git add .env.dist
git commit -m "docs: document AUTOINDEX_INTERVAL_MINUTES in .env.dist"
```

---

### Task 22: Docs — `docs/cron-indexing.md`

**Files:**
- Modify: `docs/cron-indexing.md:210-220`

- [ ] **Step 1: Rewrite "Setting Up a Scheduled Job" as the built-in scheduler, keep cron as an alternative**

Replace:

```markdown
## Setting Up a Scheduled Job (local installation)

### Linux / macOS (cron)

```cron
# Edit with: crontab -e
# Index every night at 2 AM (targets come from the stored keys)
0 2 * * * cd /path/to/zotero-rag && uv run python bin/index_libraries.py >> data/logs/cron_indexer.log 2>&1
```
```

with:

```markdown
## Setting Up a Scheduled Job

The simplest way to schedule indexing — for both local installs and
containers — is the backend's own built-in scheduler: set
`AUTOINDEX_INTERVAL_MINUTES` (see `.env.dist`) and restart. No crontab entry,
no `podman exec` cron line, nothing living outside version control:

```env
AUTOINDEX_INTERVAL_MINUTES=60   # index every 60 minutes
```

The scheduler starts 60 seconds after the backend does, then ticks on the
configured interval indefinitely, triggering the same unscoped indexing run
the admin "run full index now" control (see [Admin Controls](#admin-controls))
triggers on demand. `GET /` and `GET /api/autoindex/status` both report the
scheduler's state under `cron_indexing.scheduler` / `scheduler`:
`active` (whether `AUTOINDEX_INTERVAL_MINUTES` is set), `interval_minutes`,
and `paused` (see [Admin Controls](#admin-controls)).

### Alternative: external scheduler

If you need per-slug control (`--fingerprint`, `--force`) outside the
built-in scheduler's unscoped ticks, an external cron job still works exactly
as before — the built-in scheduler and an external one can coexist, since
both ultimately call the same `bin/index_libraries.py` guarded by the same
lock file:

```cron
# Edit with: crontab -e
# Index every night at 2 AM (targets come from the stored keys)
0 2 * * * cd /path/to/zotero-rag && uv run python bin/index_libraries.py >> data/logs/cron_indexer.log 2>&1
```
```

- [ ] **Step 2: Add an "Admin Controls" section**

Insert a new section after "Reading Progress" (which ends right before `## Troubleshooting`):

```markdown
## Admin Controls

Owners/admins of the server's authorizing Zotero group (`AUTHORIZED_GROUP_ID`)
get five additional endpoints, all requiring `X-Zotero-API-Key` from an
account Zotero itself reports as an owner or admin of that group (checked
live against `GET https://api.zotero.org/groups/<id>`, cached 5 minutes).
Loopback deployments (`API_HOST=localhost`) bypass this check entirely, same
as the rest of the Zotero-key auth gate. All five are also reachable from the
plugin's auto-index status dialog, hidden unless the backend reports
`is_admin: true`.

| Endpoint | Effect |
|---|---|
| `POST /api/autoindex/scheduler/pause` | Pauses the built-in scheduler (persists across restarts) |
| `POST /api/autoindex/scheduler/resume` | Resumes it |
| `POST /api/autoindex/scheduler/run-now` | Triggers an immediate unscoped run of every registered library, without waiting for the next tick |
| `POST /api/autoindex/abort` | Kills the entire running indexing process (last resort — use when it's genuinely stuck) |
| `POST /api/autoindex/scheduler/skip-slug` `{"slug": "users/12345"}` | Cooperatively skips one job in the active run, without killing the process — the running subprocess notices the request at its next progress-callback checkpoint (or immediately, if the slug hasn't started yet) and moves on to the next library |

Admins can also pass `?scope=all` to `GET /api/autoindex/status` to see every
job in the run (not just their own), with each job labeled
`library_name`/`owner_id` joined from `registrations.json` rather than a raw
slug. A slug with no matching registration falls back to the raw slug string.

`abort` vs. `skip-slug`: `abort` kills the whole subprocess and relies on the
existing crash-recovery path (the next run detects the dead PID and forces a
full re-index of whatever was mid-flight). `skip-slug` only ever affects the
one named job — every other library in the run keeps indexing uninterrupted.
Prefer `skip-slug` unless the process itself is unresponsive.
```

- [ ] **Step 3: Commit**

```bash
git add docs/cron-indexing.md
git commit -m "docs: document the built-in scheduler and admin controls in cron-indexing.md"
```

---

### Task 23: Docs — `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` ("Debugging the cron indexer" section — the `Enable/disable the cron job` subsection)

- [ ] **Step 1: Replace the enable/disable snippets**

Find this block in `CLAUDE.md`:

```markdown
**Enable/disable the cron job:**
```bash
# Disable (comment out)
sudo sed -i 's|^0 \* \* \* \* root /usr/bin/podman exec|#DISABLED 0 * * * * root /usr/bin/podman exec|' /etc/cron.d/zotero-rag-indexer

# Re-enable
sudo sed -i 's|^#DISABLED 0 \* \* \* \* root|0 * * * * root|' /etc/cron.d/zotero-rag-indexer

# Verify
sudo cat /etc/cron.d/zotero-rag-indexer
```
```

Replace it with:

```markdown
**Enable/disable indexing:**

If the deployment uses the built-in scheduler (`AUTOINDEX_INTERVAL_MINUTES`
set in the deploy env file), pause/resume it without a restart, as an admin:

```bash
curl -X POST https://your-instance/api/autoindex/scheduler/pause \
  -H "X-Zotero-API-Key: <admin-read-only-key>"
curl -X POST https://your-instance/api/autoindex/scheduler/resume \
  -H "X-Zotero-API-Key: <admin-read-only-key>"
```

To force an immediate full run instead of waiting for the next tick:

```bash
curl -X POST https://your-instance/api/autoindex/scheduler/run-now \
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
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document scheduler pause/resume/run-now in CLAUDE.md cron debugging section"
```

---

## Final verification

After all 23 tasks:

```bash
uv run python -m unittest discover backend/tests -v
```
Expected: PASS, no failures or errors.

```bash
node --check plugin/src/autoindex-status.js
uv run python -c "import xml.dom.minidom as m; m.parse('plugin/src/autoindex-status.xhtml'); print('well-formed')"
```
Expected: both succeed silently / print `well-formed`.

```bash
uv run pytest -m container -v -s
```
Expected: PASS or skipped (no podman/docker available) — required because `backend/main.py` changed.
