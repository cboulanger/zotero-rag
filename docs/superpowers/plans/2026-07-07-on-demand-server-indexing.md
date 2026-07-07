# On-Demand Server-Side Indexing Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user trigger an on-demand, scoped server-side index of their own libraries from the auto-indexing status dialog, and make the main search dialog's "Index" button redirect to that same dialog instead of starting a conflicting client-side run whenever a server-side run is already active.

**Architecture:** A new `POST /api/autoindex/run` endpoint identifies the caller from their `X-Zotero-API-Key` header, validates they're registered with usable keys, checks no run is already active, and spawns `bin/index_libraries.py --fingerprint <fp>` as a detached subprocess — reusing the existing cron script, lock file, and status file unchanged except for one new scoping flag. The existing `_acquire_lock()` check-then-write pattern is hardened to an atomic `filelock`-based acquire first, since this feature turns a once-an-hour, effectively-serial trigger into a low-latency, user-triggered one where two near-simultaneous clicks are now realistic. On the plugin side, a "Run now" button is added to the existing auto-indexing status monitor dialog, a small cross-window flag exposes the search dialog's in-progress state to that monitor dialog, and the search dialog's "Index" button gains a pre-check that redirects to the monitor dialog when a server-side run is already active.

**Tech Stack:** Python/FastAPI backend (`backend/`), vanilla JS Zotero plugin (`plugin/src/`), `filelock` (already a dependency), `unittest`/`pytest`.

**Design spec:** `docs/superpowers/specs/2026-07-07-on-demand-server-indexing-design.md`

---

### Task 1: Harden `CronIndexer`'s run lock with an atomic file lock

**Files:**
- Modify: `backend/services/cron_indexer.py:15-24` (imports), `:103-136` (`__init__`), `:166-199` (`_acquire_lock`/`_release_lock`)
- Test: `backend/tests/test_cron_indexer.py:112-149` (`TestLockFile`)

**Problem:** `_acquire_lock()` currently does `if self.lock_file.exists(): ...raise... ; self.lock_file.write_text(...)` — a check-then-write race, not atomic. Today the only caller is the hourly cron, so two runs overlapping is vanishingly rare. Once indexing can be triggered by a low-latency HTTP click (Task 4), two users clicking within milliseconds of each other becomes realistic, and both could see "no lock held" and both proceed to index concurrently.

- [ ] **Step 1: Write the failing test for lock contention between two independent lock holders**

Replace the two tests that assert the *old* PID-file-content semantics (`test_acquire_lock_fails_if_alive` and `test_acquire_lock_takes_over_dead_process`, `backend/tests/test_cron_indexer.py:124-137`) with tests against the new atomic-lock semantics. Edit `backend/tests/test_cron_indexer.py`:

```python
class TestLockFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_acquire_lock_creates_file(self):
        indexer = _make_indexer([], self.tmp)
        indexer._acquire_lock()
        self.assertTrue(indexer.lock_file.exists())
        pid_in_file = int(indexer.lock_file.read_text())
        self.assertEqual(pid_in_file, os.getpid())
        indexer._release_lock()

    def test_acquire_lock_fails_while_another_holder_has_it(self):
        """Two independent lock acquisitions on the same path must not both succeed —
        this is what protects two near-simultaneous manual triggers from both indexing
        at once."""
        from filelock import FileLock
        indexer = _make_indexer([], self.tmp)
        other_holder = FileLock(str(indexer.lock_file))
        other_holder.acquire(timeout=0)
        try:
            with self.assertRaises(AlreadyRunningError):
                indexer._acquire_lock()
        finally:
            other_holder.release()

    def test_acquire_lock_takes_over_stale_file(self):
        """A lock file left behind by a crashed process (no live holder) is taken
        over and reported as stale, without needing any PID/liveness check —
        the OS-level lock itself is the authoritative signal that no one holds it."""
        indexer = _make_indexer([], self.tmp)
        indexer.lock_file.write_text("99999999")  # leftover content, no live flock
        stale = indexer._acquire_lock()
        self.assertTrue(stale)
        self.assertTrue(indexer.lock_file.exists())
        indexer._release_lock()

    def test_acquire_lock_fresh_returns_false(self):
        indexer = _make_indexer([], self.tmp)
        stale = indexer._acquire_lock()
        self.assertFalse(stale)
        indexer._release_lock()

    def test_release_lock_deletes_file(self):
        indexer = _make_indexer([], self.tmp)
        indexer.lock_file.write_text(str(os.getpid()))
        indexer._release_lock()
        self.assertFalse(indexer.lock_file.exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_cron_indexer.py::TestLockFile -v`
Expected: `test_acquire_lock_fails_while_another_holder_has_it` and `test_acquire_lock_takes_over_stale_file` FAIL (the old `_acquire_lock` doesn't raise for a same-process-but-different-fd holder, and treats the stale-file case via a PID-liveness check that isn't being exercised the same way). The other three tests should still PASS against the old implementation — that's expected, they're unchanged in intent.

- [ ] **Step 3: Implement the atomic lock**

In `backend/services/cron_indexer.py`, add the import (near the existing imports, `:15-24`):

```python
from filelock import FileLock, Timeout
```

In `CronIndexer.__init__` (`:106-136`), add one field after `self.key_store = key_store` (`:132`):

```python
        self.key_store = key_store
        # Pruned-key issues from re-validation; set by the caller before run().
        self.key_issues: list[dict] = []
        # Slugs whose previous run was interrupted (stale lock takeover); set in run().
        self._interrupted_slugs: set[str] = set()
        # Atomic OS-level lock acquired in _acquire_lock(); None until then.
        self._file_lock: Optional[FileLock] = None
```

Replace `_acquire_lock`/`_release_lock` (`:166-199`) with:

```python
    def _acquire_lock(self) -> bool:
        """Atomically acquire the indexer's exclusive run lock via a non-blocking
        OS-level file lock (flock on POSIX, msvcrt on Windows) — atomic by
        construction, and automatically released by the kernel if the holding
        process crashes, so no PID/liveness bookkeeping is needed here.

        Returns True if a stale lock file (left behind by a process that
        crashed mid-run, without a live holder) was found and taken over.
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
        self.log.debug("Lock acquired (PID %s)", os.getpid())
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

Note: `is_process_alive()` stays defined and stays in use — `read_live_status()` (`:85-100`) still uses it to detect a crashed run for status-reporting purposes, a separate concern from mutual exclusion.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_cron_indexer.py -v`
Expected: all tests in the file PASS, including the two new/rewritten ones and every other existing test in the file (none of the other test classes touch `_acquire_lock` directly, so they're unaffected).

- [ ] **Step 5: Run the full backend test suite**

Run: `uv run python -m pytest backend/tests/ -q`
Expected: same pass count as before this task, no new failures.

- [ ] **Step 6: Commit**

```bash
git add backend/services/cron_indexer.py backend/tests/test_cron_indexer.py
git commit -m "fix: make CronIndexer's run lock acquisition atomic"
```

---

### Task 2: Scope `bin/index_libraries.py` runs to a single fingerprint

**Files:**
- Modify: `bin/index_libraries.py`
- Create: `backend/tests/test_index_libraries.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_index_libraries.py`:

```python
"""Unit tests for bin/index_libraries.py's argument parsing and target scoping."""

import importlib.util
import unittest
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "index_libraries.py"
_SPEC = importlib.util.spec_from_file_location("index_libraries_script", _SCRIPT_PATH)
index_libraries = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index_libraries)


class ParseArgsTest(unittest.TestCase):
    def test_fingerprint_defaults_to_none(self):
        args = index_libraries._parse_args([])
        self.assertIsNone(args.fingerprint)

    def test_fingerprint_accepted(self):
        args = index_libraries._parse_args(["--fingerprint", "abc123"])
        self.assertEqual(args.fingerprint, "abc123")


class FilterTargetsTest(unittest.TestCase):
    def setUp(self):
        self.targets = {
            "users/1": {"fingerprint": "fp-a", "zotero_key": "KA"},
            "groups/2": {"fingerprint": "fp-b", "zotero_key": "KB"},
        }

    def test_no_fingerprint_returns_all_targets(self):
        result = index_libraries._filter_targets(self.targets, None)
        self.assertEqual(result, self.targets)

    def test_fingerprint_restricts_to_owner(self):
        result = index_libraries._filter_targets(self.targets, "fp-a")
        self.assertEqual(result, {"users/1": self.targets["users/1"]})

    def test_unmatched_fingerprint_returns_empty(self):
        result = index_libraries._filter_targets(self.targets, "fp-does-not-exist")
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_index_libraries.py -v`
Expected: FAIL with `AttributeError: module 'index_libraries_script' has no attribute '_filter_targets'` (and `--fingerprint` not a recognized argument for the parse-args tests).

- [ ] **Step 3: Implement `--fingerprint` and `_filter_targets`**

In `bin/index_libraries.py`, add a new CLI argument in `_parse_args` (after the existing `--force` argument, before `--log-level`, around line 65-69):

```python
    parser.add_argument(
        "--fingerprint",
        metavar="FP",
        default=None,
        help="Restrict indexing to the auto-index entry with this fingerprint "
             "(used by the on-demand /api/autoindex/run trigger).",
    )
```

Add a small pure function near the top of the file, after `_parse_args` (before `_main`):

```python
def _filter_targets(targets: dict, fp: str | None) -> dict:
    """Restrict targets to those owned by fp, if given; otherwise return them unchanged."""
    if not fp:
        return targets
    return {slug: t for slug, t in targets.items() if t["fingerprint"] == fp}
```

In `_main`, right after `targets, key_issues = await resolve_targets(store)` (line 101), add:

```python
    targets, key_issues = await resolve_targets(store)
    for issue in key_issues:
        log.warning("Key pruned for user %s: %s", issue.get("user"), issue.get("reason"))

    targets = _filter_targets(targets, args.fingerprint)
    if args.fingerprint and not targets:
        log.error("No targets for fingerprint %s; nothing to index for this user.", args.fingerprint)
        return 1

    if not targets:
        log.error("No valid auto-index keys found. Submit a read-only key via the plugin.")
        return 1
```

(This replaces the existing `if not targets: log.error(...); return 1` block that currently follows the `key_issues` loop — the fingerprint filter is inserted between the loop and that existing check, and a fingerprint-specific error message is added for the scoped case.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_index_libraries.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Run the full backend test suite**

Run: `uv run python -m pytest backend/tests/ -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add bin/index_libraries.py backend/tests/test_index_libraries.py
git commit -m "feat: scope index_libraries.py to a single fingerprint via --fingerprint"
```

---

### Task 3: Extract `is_embedding_key_usable` and expose rate-limit expiry in key store metadata

**Files:**
- Modify: `backend/services/autoindex_resolver.py`
- Modify: `backend/services/autoindex_key_store.py:118-132` (`list_metadata`)
- Test: `backend/tests/test_autoindex_resolver.py`, `backend/tests/test_autoindex_key_store.py`

**Why:** Task 4's new endpoint needs to answer "does this user have a usable embedding key right now" — the exact same fail-closed, rate-limit-window-aware check `resolve_targets()` already makes inline. Extracting it into a standalone function avoids duplicating (and risking diverging from) that logic. It needs `embedding_key_rate_limit_until`, which today `list_metadata()` doesn't expose.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_autoindex_resolver.py` (new top-level test class, alongside the existing `ResolveTargetsTest`):

```python
from backend.services.autoindex_resolver import is_embedding_key_usable


class IsEmbeddingKeyUsableTest(unittest.TestCase):
    def test_ok_status_is_usable(self):
        self.assertTrue(is_embedding_key_usable("ok", None))

    def test_unverified_status_is_usable(self):
        self.assertTrue(is_embedding_key_usable("unverified", None))

    def test_invalid_status_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable("invalid", None))

    def test_missing_status_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable(None, None))

    def test_rate_limited_within_window_is_not_usable(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertFalse(is_embedding_key_usable("rate_limited", future))

    def test_rate_limited_after_window_is_usable(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(is_embedding_key_usable("rate_limited", past))

    def test_rate_limited_without_timestamp_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable("rate_limited", None))

    def test_unrecognized_status_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable("some-future-status", None))
```

Add to `backend/tests/test_autoindex_key_store.py` (alongside the existing `test_set_embedding_key_status_updates_rate_limit`):

```python
    def test_list_metadata_exposes_rate_limit_until(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        self.store.set_embedding_key_status(fp, "rate_limited", rate_limit_until="2026-01-01T00:00:00+00:00")
        meta = self.store.list_metadata()[0]
        self.assertEqual(meta["embedding_key_rate_limit_until"], "2026-01-01T00:00:00+00:00")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_autoindex_resolver.py::IsEmbeddingKeyUsableTest backend/tests/test_autoindex_key_store.py::AutoIndexKeyStoreTest::test_list_metadata_exposes_rate_limit_until -v`
Expected: FAIL — `is_embedding_key_usable` doesn't exist yet; `embedding_key_rate_limit_until` isn't in `list_metadata()`'s output.

- [ ] **Step 3: Extract the function and expose the field**

In `backend/services/autoindex_resolver.py`, add a new function above `resolve_targets`:

```python
def is_embedding_key_usable(status: Optional[str], rate_limit_until_str: Optional[str]) -> bool:
    """Fail closed: only explicitly recognized "good" statuses let a key through.

    An unrecognized/typo'd/future status string is treated as blocked rather
    than silently permitted. A "rate_limited" status becomes usable again once
    its recorded window has passed.
    """
    if status in ("ok", "unverified"):
        return True
    if status != "rate_limited":
        return False
    if not rate_limit_until_str:
        return False
    try:
        rate_limit_until = datetime.fromisoformat(rate_limit_until_str)
        if rate_limit_until.tzinfo is None:
            rate_limit_until = rate_limit_until.replace(tzinfo=timezone.utc)
        return rate_limit_until <= datetime.now(timezone.utc)
    except ValueError:
        logger.warning("Invalid embedding_key_rate_limit_until: %r", rate_limit_until_str)
        return False
```

Add `from typing import Optional` to the imports if not already present.

Replace the inline block inside `resolve_targets` that currently computes `still_rate_limited`/`usable`:

```python
        embedding_info = store.get_decrypted_embedding_key(fp)
        embedding_status = entry.get("embedding_key_status")
        rate_limit_until_str = entry.get("embedding_key_rate_limit_until")
        still_rate_limited = False
        if rate_limit_until_str:
            try:
                rate_limit_until = datetime.fromisoformat(rate_limit_until_str)
                if rate_limit_until.tzinfo is None:
                    rate_limit_until = rate_limit_until.replace(tzinfo=timezone.utc)
                still_rate_limited = rate_limit_until > datetime.now(timezone.utc)
            except ValueError:
                still_rate_limited = False
                logger.warning(
                    "Invalid embedding_key_rate_limit_until for %s: %r", fp, rate_limit_until_str
                )

        # Fail closed: only explicitly recognized "good" statuses let the slug
        # through. An unrecognized/typo'd/future status string is treated as
        # blocked rather than silently permitted.
        usable = embedding_status in ("ok", "unverified") or (
            embedding_status == "rate_limited" and not still_rate_limited
        )
        if not embedding_info or not usable:
```

with:

```python
        embedding_info = store.get_decrypted_embedding_key(fp)
        embedding_status = entry.get("embedding_key_status")
        rate_limit_until_str = entry.get("embedding_key_rate_limit_until")
        usable = is_embedding_key_usable(embedding_status, rate_limit_until_str)
        if not embedding_info or not usable:
```

(The rest of that `if` block — building the `reason` string and appending to `issues` — is unchanged.)

In `backend/services/autoindex_key_store.py`, in `list_metadata()` (`:118-132`), add one field to the appended dict, after `"embedding_key_status": entry.get("embedding_key_status"),`:

```python
                "embedding_key_status": entry.get("embedding_key_status"),
                "embedding_key_rate_limit_until": entry.get("embedding_key_rate_limit_until"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_autoindex_resolver.py backend/tests/test_autoindex_key_store.py -v`
Expected: all tests PASS, including every pre-existing test in both files (the refactor preserves `resolve_targets`'s external behavior exactly).

- [ ] **Step 5: Run the full backend test suite**

Run: `uv run python -m pytest backend/tests/ -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add backend/services/autoindex_resolver.py backend/services/autoindex_key_store.py backend/tests/test_autoindex_resolver.py backend/tests/test_autoindex_key_store.py
git commit -m "refactor: extract is_embedding_key_usable, expose rate-limit expiry in key metadata"
```

---

### Task 4: Add `POST /api/autoindex/run`

**Files:**
- Modify: `backend/api/autoindex.py`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_autoindex_api.py` (new imports at the top: `from unittest.mock import MagicMock` alongside the existing `patch, AsyncMock`; new test methods on `AutoIndexApiTest`):

```python
    def _register_user(self, api_key="RO", targets=None):
        validation = KeyValidation(user_id=1, username="u", targets=targets or ["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            self.client.post("/api/autoindex/keys", json={"api_key": api_key})

    def _set_model_type(self, model_type: str) -> None:
        mock_preset = MagicMock()
        mock_preset.embedding.model_type = model_type
        get_settings().get_hardware_preset = MagicMock(return_value=mock_preset)

    def test_run_rejects_missing_header(self):
        self._register_user()
        r = self.client.post("/api/autoindex/run")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Missing X-Zotero-API-Key", r.json()["detail"])

    def test_run_rejects_unregistered_fingerprint(self):
        r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "NEVER-REGISTERED"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not registered", r.json()["detail"])

    def test_run_rejects_missing_embedding_key_on_remote_preset(self):
        self._register_user()
        self._set_model_type("remote")
        r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("No embedding API key", r.json()["detail"])

    def test_run_succeeds_on_local_preset_without_embedding_key(self):
        self._register_user()
        self._set_model_type("local")
        with patch("backend.api.autoindex.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        mock_spawn.assert_awaited_once()
        args = mock_spawn.await_args.args
        self.assertIn("--fingerprint", args)

    def test_run_succeeds_with_valid_embedding_key_on_remote_preset(self):
        from backend.services.embedding_key_validator import EmbeddingKeyValidation
        emb_validation = EmbeddingKeyValidation(status="ok", key_name="KISSKI_API_KEY")
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.api.autoindex.validate_embedding_key", new=AsyncMock(return_value=emb_validation)):
            self.client.post("/api/autoindex/keys", json={"api_key": "RO", "embedding_api_key": "EMB"})
        self._set_model_type("remote")
        with patch("backend.api.autoindex.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        mock_spawn.assert_awaited_once()

    def test_run_rejects_when_already_running(self):
        self._register_user()
        self._set_model_type("local")
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir()
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 1}), encoding="utf-8")
        with patch("backend.api.autoindex.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("already running", r.json()["detail"])
```

Note: `is_process_alive` is imported by `read_live_status` (in `cron_indexer.py`) to decide whether a `running: True` status file reflects a genuinely live process; patch it at `backend.api.autoindex.is_process_alive`... actually `read_live_status` is called directly (not re-implemented in `autoindex.py`), so the correct patch target for that liveness check is `backend.services.cron_indexer.is_process_alive` (same module `read_live_status` lives in). Use:
```python
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
```
in `test_run_rejects_when_already_running` instead of patching it on `backend.api.autoindex`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_autoindex_api.py -v -k test_run_`
Expected: FAIL with 404 (no such route yet) for all six new tests.

- [ ] **Step 3: Implement the endpoint**

In `backend/api/autoindex.py`, update imports at the top:

```python
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.dependencies import get_zotero_identity
from backend.services.autoindex_key_store import AutoIndexKeyStore, fingerprint
from backend.services.autoindex_resolver import is_embedding_key_usable
from backend.services.cron_indexer import read_live_status
from backend.services.embedding_key_validator import validate_embedding_key
from backend.services.zotero_identity import ZoteroIdentity
from backend.zotero.key_validator import validate_key

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
```

Add near the bottom of the file (after the existing `status()` endpoint):

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

    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if live_status.get("running"):
        raise HTTPException(status_code=409, detail="Indexing is already running on the server.")

    await _spawn_index_run(settings, fp)
    return {"started": True}


def _find_own_entry(store: AutoIndexKeyStore, fp: str) -> Optional[dict]:
    return next((k for k in store.list_metadata() if k["fingerprint"] == fp), None)


def _embedding_key_block_reason(own: dict) -> Optional[str]:
    status = own.get("embedding_key_status")
    rate_limit_until = own.get("embedding_key_rate_limit_until")
    if is_embedding_key_usable(status, rate_limit_until):
        return None
    if not own.get("has_embedding_key"):
        return "No embedding API key configured; set one up in Preferences before running indexing."
    if status == "invalid":
        return "Embedding API key was rejected; update it in Preferences."
    if status == "rate_limited":
        return f"Embedding API key is rate-limited until {rate_limit_until}; try again later."
    return f"Embedding API key has unrecognized status {status!r}."


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
        logf.close()
```

Update the module docstring at the top of the file to list the new route:

```python
"""Auto-index key endpoints.

POST   /api/autoindex/keys    — submit a read-only key (validated, stored encrypted)
DELETE /api/autoindex/keys    — remove a key
GET    /api/autoindex/keys    — list the caller's own key metadata (no plaintext)
GET    /api/autoindex/status  — live cron-run progress (running/crashed, counts)
POST   /api/autoindex/run     — trigger an on-demand run scoped to the caller's own libraries

All endpoints are protected by the global Zotero-key auth middleware (X-Zotero-API-Key). When
AUTOINDEX_SECRET is unset the feature is disabled and the key endpoints return 503.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_autoindex_api.py -v`
Expected: all tests PASS, including every pre-existing test in the file.

- [ ] **Step 5: Run the full backend test suite**

Run: `uv run python -m pytest backend/tests/ -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add backend/api/autoindex.py backend/tests/test_autoindex_api.py
git commit -m "feat: add POST /api/autoindex/run to trigger on-demand indexing"
```

---

### Task 5: Expose the search dialog's in-progress state to other windows

**Files:**
- Modify: `plugin/src/zotero-rag.js` (near `:135-141`, the `ZoteroRAGPlugin` constructor fields; near `:1751-1763`, add a new method after `openAutoindexStatusDialog`)
- Modify: `plugin/src/dialog.js:175-184` (`init()`)

No automated test harness exists for this privileged Zotero-global JS (consistent with the rest of the plugin) — verified manually in Task 8.

- [ ] **Step 1: Track the live dialog instance on the plugin singleton**

In `plugin/src/zotero-rag.js`, in the `ZoteroRAGPlugin` constructor, add a field alongside the existing window-tracking fields (after `this._autoindexStatusWindow = null;`, `:141`):

```js
		this._autoindexStatusWindow = null;
		// Set by dialog.js's init() to the live ZoteroRAGDialog object (not just its
		// window), so other windows (e.g. the autoindex-status dialog) can read its
		// isOperationInProgress flag without reaching into window internals themselves.
		this._dialogInstance = null;
```

- [ ] **Step 2: Set it from the search dialog**

In `plugin/src/dialog.js`, in `init()`, right after the existing plugin-reference assignment (`:180`):

```js
			this.plugin = window.arguments[0].plugin;
			this.plugin._dialogInstance = this;
```

- [ ] **Step 3: Add the query method**

In `plugin/src/zotero-rag.js`, add a new method on `ZoteroRAGPlugin` right after `openAutoindexStatusDialog` (`:1751-1763`):

```js
	/**
	 * Whether the search dialog currently has a client-side indexing operation
	 * in progress. Used by the auto-indexing status dialog's "Run now" button
	 * and the search dialog's own "Index" button to avoid starting a second,
	 * conflicting indexing operation.
	 * @returns {boolean}
	 */
	isClientIndexingActive() {
		return !!(this._dialogInstance && this._dialogInstance.isOperationInProgress);
	}
```

- [ ] **Step 4: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/src/dialog.js
git commit -m "feat: expose search dialog's indexing state via plugin.isClientIndexingActive()"
```

---

### Task 6: Add a "Run now" button to the auto-indexing status dialog, and fix the empty-progress edge case

**Files:**
- Modify: `plugin/src/autoindex-status.xhtml` (near `:58`, the `run-banner` div)
- Modify: `plugin/src/autoindex-status.js` (`render()` at `:114-131`, plus new `runNow()`/`updateRunNowButtonState()` methods)

No automated test harness exists for this privileged Zotero-global JS — verified manually in Task 8.

- [ ] **Step 1: Add the button to the dialog markup**

In `plugin/src/autoindex-status.xhtml`, add a button right before the existing `<div id="run-banner">` (`:58`):

```xml
  <button id="run-now-button" type="button" class="dialog-button">Run indexing now</button>
  <div id="run-banner">Loading status…</div>
```

- [ ] **Step 2: Wire the button and fix the empty-progress message**

In `plugin/src/autoindex-status.js`:

Add to the `@typedef {Object} AutoIndexStatusResponse` block (`:26-36`), nothing needed — `slugs` already reflects the caller's own filtered entries.

In `init()` (`:48-72`), add button wiring after the existing `closeButton` wiring:

```js
		const runNowButton = document.getElementById('run-now-button');
		if (runNowButton) {
			runNowButton.addEventListener('click', () => this.runNow());
		}
```

Replace `render()` (`:114-131`) with:

```js
	/**
	 * Render the full dialog from a status response.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	render(data) {
		if (!data.enabled) {
			this.renderBanner(data.disabled_reason || 'Automatic indexing is not configured on this server.', 'crashed');
			return;
		}
		const ownSlugCount = Object.keys(data.slugs || {}).length;
		if (data.crashed) {
			this.renderBanner('The last automatic indexing run crashed unexpectedly.', 'crashed');
		} else if (data.running && ownSlugCount === 0) {
			// A run is active, but none of it is this caller's own libraries —
			// most likely another user's manual trigger or a shared-lock cron tick.
			this.renderBanner('Indexing server currently busy, please wait and try again later.', 'running');
		} else if (data.running) {
			this.renderBanner(`Running since ${this.formatTime(data.started_at)}…`, 'running');
		} else if (data.finished_at) {
			this.renderBanner(`Idle. Last run finished ${this.formatTime(data.finished_at)}.`, 'idle');
		} else {
			this.renderBanner('Idle. No automatic indexing run has happened yet.', 'idle');
		}

		this.renderLibraries(data.slugs || {});
		this.renderProblems(data.key_issues || []);
		this.updateRunNowButtonState(data);
	},

	/**
	 * Enable/disable the "Run now" button based on server- and client-side
	 * indexing state.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	updateRunNowButtonState(data) {
		const button = /** @type {HTMLButtonElement} */ (document.getElementById('run-now-button'));
		if (!button) return;
		const busy = data.running === true || (this.plugin && this.plugin.isClientIndexingActive());
		button.disabled = busy;
		button.textContent = busy ? 'Indexing in progress…' : 'Run indexing now';
	},

	/**
	 * Trigger an on-demand server-side indexing run for the caller's own libraries.
	 * @returns {Promise<void>}
	 */
	async runNow() {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement} */ (document.getElementById('run-now-button'));
		if (button) button.disabled = true;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/run`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				if (button) button.disabled = false;
				return;
			}
			// Success: the next poll tick (within 5s) picks up running:true and
			// re-disables the button via updateRunNowButtonState().
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
			if (button) button.disabled = false;
		}
	},
```

- [ ] **Step 3: Commit**

```bash
git add plugin/src/autoindex-status.xhtml plugin/src/autoindex-status.js
git commit -m "feat: add Run now button to the auto-indexing status dialog"
```

---

### Task 7: Redirect the search dialog's "Index" button to the monitor dialog when a server run is active

**Files:**
- Modify: `plugin/src/dialog.js:1156-1157` (top of `submitIndexOnly()`), new helper method

No automated test harness exists for this privileged Zotero-global JS — verified manually in Task 8.

- [ ] **Step 1: Add the pre-check**

In `plugin/src/dialog.js`, change the start of `submitIndexOnly()` (`:1156-1157`) from:

```js
	async submitIndexOnly() {
		if (!this.plugin) return;
```

to:

```js
	async submitIndexOnly() {
		if (!this.plugin) return;

		if (this.plugin.backendURL && await this.isServerIndexingRunning()) {
			this.plugin.openAutoindexStatusDialog(this.window);
			return;
		}
```

- [ ] **Step 2: Add the helper method**

Add a new method on `ZoteroRAGDialog`, near `submitIndexOnly` (directly above it):

```js
	/**
	 * Check whether a server-side auto-indexing run is currently active.
	 * Fails open (returns false) on any network/parse error — a backend
	 * hiccup should not block the user's own client-side indexing.
	 * @returns {Promise<boolean>}
	 */
	async isServerIndexingRunning() {
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/status`, {
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) return false;
			const data = await response.json();
			return data.running === true;
		} catch (e) {
			return false;
		}
	},

```

- [ ] **Step 3: Commit**

```bash
git add plugin/src/dialog.js
git commit -m "feat: redirect Index button to the status dialog when server indexing is active"
```

---

### Task 8: Manual/smoke verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `uv run python -m pytest backend/tests/ -q`
Expected: all tests pass, no regressions from Tasks 1-4.

- [ ] **Step 2: Confirm the new endpoint is registered**

Run: `uv run python -c "from backend.main import app; print([r.path for r in app.routes if 'autoindex' in r.path])"`
Expected output includes `/api/autoindex/run` alongside the three existing `/api/autoindex/*` routes.

- [ ] **Step 3: Confirm the script's new flag**

Run: `uv run python bin/index_libraries.py --help`
Expected: help text lists `--fingerprint FP`.

- [ ] **Step 4: Manual plugin verification (requires a running Zotero + backend with AUTOINDEX_SECRET set)**

Using the Zotero MCP bridge or a live Zotero instance:
1. Register auto-index keys for a test user (Preferences → Automatic indexing).
2. Open the monitor dialog (View indexing status) and click "Run now" — confirm progress bars update within ~5s and the button re-enables once the run finishes.
3. While a run is active, open the search dialog and click "Index" — confirm it opens the monitor dialog instead of starting client-side indexing.
4. Start a client-side index from the search dialog (with no server run active), then open the monitor dialog — confirm "Run now" is disabled with the "Indexing in progress…" label.
5. If two accounts are available: while User B's manual run is active, open User A's monitor dialog — confirm it shows "Indexing server currently busy, please wait and try again later." instead of a bare empty list.

If a live Zotero instance/MCP bridge isn't available in this session, report Steps 1-3 as the verification performed and Step 4 as pending manual verification — don't fabricate results for it.
