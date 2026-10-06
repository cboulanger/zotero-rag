# Deferred Server-Side Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Fix Unavailable Attachments dialog upload a repaired attachment and move on immediately (deferring embedding to the library's existing autoindex run), with a split-button alternative to force today's synchronous indexing, and a "Waiting to be indexed" status with ETA shown on refresh/reopen.

**Architecture:** A new on-disk pending-upload cache (`<data_path>/system/pending_uploads/<library_id>/`) holds raw bytes + metadata for attachments the plugin uploaded but deferred. Two new backend endpoints write to it (fast) and drain one entry from it on demand (synchronous, for "force now"). The existing per-library autoindex job (`CronIndexer._index_slug`) drains the whole cache for its library on every run, reusing the exact same `_execute_upload_impl` pipeline already used by today's synchronous/async upload endpoints — no new extraction/embedding code path. The plugin's `RemoteIndexer._uploadAttachment` gains a `defer` flag that swaps the upload URL; the Fix dialog's single Fix button becomes a split button that controls that flag, and `check-indexed` is extended so the dialog can show accurate queued/ETA state without re-running Fix.

**Tech Stack:** Python/FastAPI (`uv run pytest`), plain JS Zotero plugin (`node --test`).

**Spec:** `docs/superpowers/specs/2026-10-06-deferred-server-side-indexing-design.md`

---

## Backend

### Task 1: Pending-upload cache storage primitives

**Files:**
- Create: `backend/services/pending_upload_cache.py`
- Test: `backend/tests/test_pending_upload_cache.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_pending_upload_cache.py
import json
import tempfile
import unittest
from pathlib import Path

from backend.services import pending_upload_cache as cache


class TestPendingUploadCacheStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trips_bytes_and_metadata(self):
        cache.write_entry(
            self.data_path, "u123", "ATT1", b"hello world",
            {"item_key": "ITEM1", "mime_type": "application/pdf", "item_version": 5,
             "attachment_version": 2, "title": "A Title", "authors": ["Doe"],
             "year": 2020, "item_type": "journalArticle", "library_type": "user",
             "library_name": "My Library"},
        )
        result = cache.read_entry(self.data_path, "u123", "ATT1")
        self.assertIsNotNone(result)
        file_bytes, meta = result
        self.assertEqual(file_bytes, b"hello world")
        self.assertEqual(meta["item_key"], "ITEM1")
        self.assertEqual(meta["title"], "A Title")
        self.assertEqual(meta["attempts"], 0)
        self.assertIsNone(meta["last_error"])
        self.assertIn("enqueued_at", meta)

    def test_read_missing_entry_returns_none(self):
        self.assertIsNone(cache.read_entry(self.data_path, "u123", "NOPE"))

    def test_has_entry(self):
        self.assertFalse(cache.has_entry(self.data_path, "u123", "ATT1"))
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        self.assertTrue(cache.has_entry(self.data_path, "u123", "ATT1"))

    def test_rewrite_overwrites_in_place_and_resets_attempts(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"v1", {"item_key": "I1"})
        cache.record_failure(self.data_path, "u123", "ATT1", "boom")
        cache.write_entry(self.data_path, "u123", "ATT1", b"v2", {"item_key": "I1"})
        file_bytes, meta = cache.read_entry(self.data_path, "u123", "ATT1")
        self.assertEqual(file_bytes, b"v2")
        self.assertEqual(meta["attempts"], 0)
        self.assertIsNone(meta["last_error"])

    def test_delete_entry_removes_both_files(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        cache.delete_entry(self.data_path, "u123", "ATT1")
        self.assertIsNone(cache.read_entry(self.data_path, "u123", "ATT1"))

    def test_delete_entry_is_safe_when_nothing_exists(self):
        cache.delete_entry(self.data_path, "u123", "NEVER_WROTE")  # must not raise

    def test_list_entries_returns_metadata_sorted_by_enqueued_at(self):
        cache.write_entry(self.data_path, "u123", "ATT_B", b"x", {"item_key": "I_B"})
        cache.write_entry(self.data_path, "u123", "ATT_A", b"x", {"item_key": "I_A"})
        entries = cache.list_entries(self.data_path, "u123")
        self.assertEqual([e["attachment_key"] for e in entries], ["ATT_B", "ATT_A"])
        self.assertEqual(entries[0]["item_key"], "I_B")

    def test_list_entries_empty_for_unknown_library(self):
        self.assertEqual(cache.list_entries(self.data_path, "u_nope"), [])

    def test_list_entries_scoped_to_one_library(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        cache.write_entry(self.data_path, "u999", "ATT2", b"x", {"item_key": "I2"})
        self.assertEqual(len(cache.list_entries(self.data_path, "u123")), 1)
        self.assertEqual(len(cache.list_entries(self.data_path, "u999")), 1)

    def test_record_failure_increments_attempts_and_sets_last_error(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        cache.record_failure(self.data_path, "u123", "ATT1", "first error")
        cache.record_failure(self.data_path, "u123", "ATT1", "second error")
        _, meta = cache.read_entry(self.data_path, "u123", "ATT1")
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["last_error"], "second error")

    def test_record_failure_on_missing_entry_is_a_no_op(self):
        cache.record_failure(self.data_path, "u123", "NOPE", "boom")  # must not raise


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_pending_upload_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.pending_upload_cache'`

- [ ] **Step 3: Implement the storage module**

```python
# backend/services/pending_upload_cache.py
"""On-disk cache of attachment uploads deferred for later indexing.

A plain-files store, following the same pattern as
``backend/utils/migration_state.py`` and ``backend/services/autoindex_key_store.py``
(atomic JSON writes via ``os.replace``) — no database. Keyed by
``(library_id, attachment_key)``: at most one pending entry per attachment,
a re-upload overwrites the previous one in place.

Drained by CronIndexer._drain_pending_uploads (backend/services/cron_indexer.py)
on every autoindex run for that library, and by the on-demand "process now"
endpoint (POST /api/index/document/cache/{library_id}/{attachment_key}/process-now).
See docs/superpowers/specs/2026-10-06-deferred-server-side-indexing-design.md.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _library_dir(data_path: Path, library_id: str) -> Path:
    return data_path / "system" / "pending_uploads" / library_id


def _bin_path(data_path: Path, library_id: str, attachment_key: str) -> Path:
    return _library_dir(data_path, library_id) / f"{attachment_key}.bin"


def _meta_path(data_path: Path, library_id: str, attachment_key: str) -> Path:
    return _library_dir(data_path, library_id) / f"{attachment_key}.meta.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_bin_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_meta_")
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


def write_entry(data_path: Path, library_id: str, attachment_key: str, file_bytes: bytes, metadata: dict) -> None:
    """Write (or overwrite) a pending entry.

    `metadata` should carry the same fields the upload endpoints already
    accept (item_key, mime_type, item_version, attachment_version,
    item_modified/zotero_modified, title, authors, year, item_type,
    library_type, library_name). Any `enqueued_at`/`attempts`/`last_error`
    already in `metadata` are ignored — this always resets them.
    """
    entry = {
        **metadata,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
        "last_error": None,
    }
    _atomic_write_bytes(_bin_path(data_path, library_id, attachment_key), file_bytes)
    _atomic_write_json(_meta_path(data_path, library_id, attachment_key), entry)


def read_entry(data_path: Path, library_id: str, attachment_key: str) -> Optional[tuple[bytes, dict]]:
    """Return (file_bytes, metadata) for a pending entry, or None if absent."""
    bin_path = _bin_path(data_path, library_id, attachment_key)
    meta_path = _meta_path(data_path, library_id, attachment_key)
    if not bin_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return bin_path.read_bytes(), meta


def has_entry(data_path: Path, library_id: str, attachment_key: str) -> bool:
    return _meta_path(data_path, library_id, attachment_key).exists()


def delete_entry(data_path: Path, library_id: str, attachment_key: str) -> None:
    for path in (_bin_path(data_path, library_id, attachment_key), _meta_path(data_path, library_id, attachment_key)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def list_entries(data_path: Path, library_id: str) -> list[dict]:
    """Metadata for every pending entry in a library, oldest-enqueued first.

    Each dict has an injected `attachment_key` (derived from the filename);
    file bytes are not read (use `read_entry` for that).
    """
    lib_dir = _library_dir(data_path, library_id)
    if not lib_dir.exists():
        return []
    out: list[dict] = []
    for meta_path in lib_dir.glob("*.meta.json"):
        try:
            entry = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry["attachment_key"] = meta_path.name[: -len(".meta.json")]
        out.append(entry)
    out.sort(key=lambda e: e.get("enqueued_at", ""))
    return out


def record_failure(data_path: Path, library_id: str, attachment_key: str, error: str) -> None:
    """Bump `attempts` and set `last_error` on an existing entry. No-op if it's gone."""
    meta_path = _meta_path(data_path, library_id, attachment_key)
    try:
        entry = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = error
    _atomic_write_json(meta_path, entry)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_pending_upload_cache.py -v`
Expected: PASS — 10 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/services/pending_upload_cache.py backend/tests/test_pending_upload_cache.py
git commit -m "feat(backend): add pending-upload cache storage primitives"
```

---

### Task 2: Queue ETA / block-reason computation

**Files:**
- Modify: `backend/services/pending_upload_cache.py`
- Test: `backend/tests/test_pending_upload_cache.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_pending_upload_cache.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from backend.config.settings import Settings


class TestQueueStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, **overrides):
        return Settings(data_path=self.data_path, autoindex_secret="x" * 32, **overrides)

    def test_eta_from_interval_scheduler_with_no_prior_run(self):
        settings = self._settings(autoindex_interval_minutes=30)
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(reason)
        eta_dt = datetime.fromisoformat(eta)
        delta = eta_dt - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=25) < delta < timedelta(minutes=35))

    def test_eta_from_interval_scheduler_after_a_prior_run(self):
        settings = self._settings(autoindex_interval_minutes=60)
        last_run = datetime.now(timezone.utc) - timedelta(minutes=10)
        (self.data_path / "system" / "cron_status.json").write_text(
            json.dumps({"finished_at": last_run.isoformat()}), encoding="utf-8"
        )
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(reason)
        eta_dt = datetime.fromisoformat(eta)
        delta = eta_dt - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=45) < delta < timedelta(minutes=55))

    def test_eta_falls_back_to_top_of_next_hour_without_an_interval(self):
        settings = self._settings(autoindex_interval_minutes=None)
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(reason)
        eta_dt = datetime.fromisoformat(eta)
        self.assertEqual(eta_dt.minute, 0)
        self.assertEqual(eta_dt.second, 0)
        self.assertTrue(eta_dt > datetime.now(timezone.utc))

    def test_paused_scheduler_reports_paused_reason_and_no_eta(self):
        from backend.services.autoindex_scheduler import write_scheduler_state
        write_scheduler_state(self.data_path, {"paused": True})
        settings = self._settings(autoindex_interval_minutes=30)
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(eta)
        self.assertEqual(reason, "paused")

    def test_library_has_valid_autoindex_target_true_when_slug_listed(self):
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = [{"targets": ["users/123", "groups/456"]}]
        self.assertTrue(cache.library_has_valid_autoindex_target("u123", key_store))
        self.assertTrue(cache.library_has_valid_autoindex_target("456", key_store))

    def test_library_has_valid_autoindex_target_false_when_slug_absent(self):
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = [{"targets": ["groups/456"]}]
        self.assertFalse(cache.library_has_valid_autoindex_target("u999", key_store))

    def test_library_has_valid_autoindex_target_false_when_store_disabled(self):
        key_store = MagicMock()
        key_store.enabled = False
        self.assertFalse(cache.library_has_valid_autoindex_target("u123", key_store))

    def test_get_queue_status_prioritizes_paused_over_key_invalid(self):
        from backend.services.autoindex_scheduler import write_scheduler_state
        write_scheduler_state(self.data_path, {"paused": True})
        settings = self._settings(autoindex_interval_minutes=30)
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = []
        status = cache.get_queue_status(settings, "u123", key_store)
        self.assertEqual(status, {"eta": None, "reason": "paused"})

    def test_get_queue_status_reports_key_invalid_when_not_paused(self):
        settings = self._settings(autoindex_interval_minutes=30)
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = []
        status = cache.get_queue_status(settings, "u123", key_store)
        self.assertEqual(status, {"eta": None, "reason": "key_invalid"})

    def test_get_queue_status_reports_eta_when_valid_and_not_paused(self):
        settings = self._settings(autoindex_interval_minutes=30)
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = [{"targets": ["users/123"]}]
        status = cache.get_queue_status(settings, "u123", key_store)
        self.assertIsNone(status["reason"])
        self.assertIsNotNone(status["eta"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_pending_upload_cache.py::TestQueueStatus -v`
Expected: FAIL — `AttributeError: module 'backend.services.pending_upload_cache' has no attribute 'compute_queue_eta'`

- [ ] **Step 3: Implement the ETA/status functions**

Append to `backend/services/pending_upload_cache.py` (add these imports to the top of the file alongside the existing ones: `from datetime import timedelta` next to the existing `datetime, timezone` import, and `from typing import Optional` stays):

```python
def compute_queue_eta(settings) -> tuple[Optional[str], Optional[str]]:
    """Return (eta_iso8601_or_None, reason). `reason` is None when `eta` is
    present, else "paused" if the built-in scheduler is explicitly paused.

    ETA is the built-in interval scheduler's next tick (last run time, or
    now, plus the configured interval) when `autoindex_interval_minutes` is
    set, else the top of the next UTC hour — matching the documented
    external-cron deployment mode's hourly crontab.
    """
    from backend.services.autoindex_scheduler import read_scheduler_state

    if read_scheduler_state(settings.data_path).get("paused", False):
        return None, "paused"

    now = datetime.now(timezone.utc)
    if settings.autoindex_interval_minutes:
        status_path = settings.data_path / "system" / "cron_status.json"
        last_ts = None
        try:
            cron_status = json.loads(status_path.read_text(encoding="utf-8"))
            last_ts = cron_status.get("finished_at") or cron_status.get("started_at")
        except (OSError, json.JSONDecodeError):
            pass
        last_dt = datetime.fromisoformat(last_ts) if last_ts else now
        eta_dt = last_dt + timedelta(minutes=settings.autoindex_interval_minutes)
        if eta_dt < now:
            eta_dt = now
        return eta_dt.isoformat(), None

    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour.isoformat(), None


def library_has_valid_autoindex_target(library_id: str, key_store) -> bool:
    """Whether some stored autoindex key currently targets this library."""
    from backend.api.public_query import backend_id_to_slug

    if not key_store.enabled:
        return False
    slug = backend_id_to_slug(library_id)
    return any(slug in (entry.get("targets") or []) for entry in key_store.list_metadata())


def get_queue_status(settings, library_id: str, key_store) -> dict:
    """Combine `compute_queue_eta` with the per-library key-validity check.

    Priority when `eta` would otherwise be null: "paused" (global) takes
    precedence over "key_invalid" (per-library), since a paused scheduler
    blocks every library regardless of key state.
    """
    eta, reason = compute_queue_eta(settings)
    if reason == "paused":
        return {"eta": None, "reason": "paused"}
    if not library_has_valid_autoindex_target(library_id, key_store):
        return {"eta": None, "reason": "key_invalid"}
    return {"eta": eta, "reason": None}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_pending_upload_cache.py -v`
Expected: PASS — all tests in the file (storage + queue status).

- [ ] **Step 5: Commit**

```bash
git add backend/services/pending_upload_cache.py backend/tests/test_pending_upload_cache.py
git commit -m "feat(backend): compute deferred-indexing ETA and block reason"
```

---

### Task 3: `POST /api/index/document/cache` endpoint

**Files:**
- Modify: `backend/api/document_upload.py`
- Test: `backend/tests/test_cache_upload_endpoints.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_cache_upload_endpoints.py
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend import dependencies
from backend.config import settings as settings_module
from backend.services import pending_upload_cache


class TestCacheUploadEndpoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.identity = MagicMock(user_id=1)
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: self.identity

        self.settings = settings_module.Settings(
            data_path=self.data_path, autoindex_secret="x" * 32, autoindex_interval_minutes=30,
        )
        self._settings_patch = patch(
            "backend.api.document_upload.get_settings", return_value=self.settings
        )
        self._settings_patch.start()

        self.key_store = MagicMock()
        self.key_store.enabled = True
        self.key_store.list_metadata.return_value = [{"targets": ["users/1"]}]
        self._key_store_patch = patch(
            "backend.api.document_upload.AutoIndexKeyStore", return_value=self.key_store
        )
        self._key_store_patch.start()

    def tearDown(self):
        app.dependency_overrides.clear()
        self._settings_patch.stop()
        self._key_store_patch.stop()
        self._tmp.cleanup()

    def _upload(self, library_id="u1", attachment_key="ATT1"):
        metadata = {
            "library_id": library_id, "library_type": "user", "item_key": "ITEM1",
            "attachment_key": attachment_key, "mime_type": "application/pdf",
            "item_version": 3, "attachment_version": 1,
        }
        return self.client.post(
            "/api/index/document/cache",
            files={"file": ("x.pdf", b"file bytes", "application/pdf")},
            data={"metadata": json.dumps(metadata)},
        )

    def test_queues_the_upload_and_returns_eta(self):
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertIsNotNone(body["eta"])
        self.assertIsNone(body["reason"])
        self.assertTrue(pending_upload_cache.has_entry(self.data_path, "u1", "ATT1"))

    def test_reports_key_invalid_when_library_has_no_autoindex_target(self):
        self.key_store.list_metadata.return_value = []
        response = self._upload()
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertIsNone(body["eta"])
        self.assertEqual(body["reason"], "key_invalid")
        # still cached even though nothing will drain it yet
        self.assertTrue(pending_upload_cache.has_entry(self.data_path, "u1", "ATT1"))

    def test_rejects_access_to_a_library_the_identity_cannot_reach(self):
        with patch("backend.api.document_upload.assert_can_access", side_effect=Exception("forbidden")):
            response = self._upload()
        self.assertEqual(response.status_code, 500)  # unhandled Exception surfaces as 500 under raise_server_exceptions=False


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_cache_upload_endpoints.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet) on the first assertion.

- [ ] **Step 3: Add the endpoint**

In `backend/api/document_upload.py`, add this import near the other `backend.services` imports (after the existing `from backend.services.access_gate import assert_can_access` line):

```python
from backend.services import pending_upload_cache
from backend.services.autoindex_key_store import AutoIndexKeyStore
```

Add this response model next to `AsyncUploadResponse` (after its class block, around line 236):

```python
class CacheUploadResponse(BaseModel):
    """Response from the deferred-upload cache endpoint."""

    status: str  # always "queued"
    eta: Optional[str] = None  # ISO 8601; null if `reason` is set
    reason: Optional[str] = None  # None | "paused" | "key_invalid"
```

Add the endpoint right after `upload_and_index_document_async` (after its closing, before the `GET /index/tasks/{task_id}` endpoint, i.e. insert before line 838's `@router.get("/index/tasks/{task_id}", ...)`):

```python
@router.post(
    "/index/document/cache",
    response_model=CacheUploadResponse,
    summary="Upload a document for deferred indexing (remote mode)",
)
async def upload_document_to_cache(
    file: UploadFile = File(..., description="Raw attachment bytes"),
    metadata: str = Form(...),
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
):
    """
    Cache a document's bytes for the library's next autoindex run instead of
    indexing it now. Returns immediately — no extraction, no embedding.

    Use POST /api/index/document/cache/{library_id}/{attachment_key}/process-now
    to force immediate indexing of an entry already sitting in the cache.
    """
    meta_dict, _doc_metadata, library_id, item_key, attachment_key, _user_id, \
        library_type, mime_type, item_version, attachment_version, item_modified, \
        file_bytes, _timeout_multiplier = await _parse_upload_request(file, metadata, identity)

    settings = get_settings()
    pending_upload_cache.write_entry(
        settings.data_path, library_id, attachment_key, file_bytes,
        {
            "item_key": item_key,
            "mime_type": mime_type,
            "item_version": item_version,
            "attachment_version": attachment_version,
            "zotero_modified": item_modified,
            "title": meta_dict.get("title", "Untitled"),
            "authors": meta_dict.get("authors", []),
            "year": meta_dict.get("year"),
            "item_type": meta_dict.get("item_type"),
            "library_type": library_type,
            "library_name": meta_dict.get("library_name", ""),
        },
    )
    logger.info(f"Cached deferred upload: library={library_id} attachment={attachment_key}")

    key_store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    status = pending_upload_cache.get_queue_status(settings, library_id, key_store)
    return CacheUploadResponse(status="queued", eta=status["eta"], reason=status["reason"])
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest backend/tests/test_cache_upload_endpoints.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/api/document_upload.py backend/tests/test_cache_upload_endpoints.py
git commit -m "feat(backend): add POST /api/index/document/cache deferred-upload endpoint"
```

---

### Task 4: `POST /api/index/document/cache/{library_id}/{attachment_key}/process-now` endpoint

**Files:**
- Modify: `backend/api/document_upload.py`
- Test: `backend/tests/test_cache_upload_endpoints.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cache_upload_endpoints.py`:

```python
class TestProcessNowEndpoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.identity = MagicMock(user_id=1)
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: self.identity

        self.settings = settings_module.Settings(data_path=self.data_path, testing=True)
        self._settings_patch = patch(
            "backend.api.document_upload.get_settings", return_value=self.settings
        )
        self._settings_patch.start()

        self.vector_store = MagicMock()
        self.vector_store.check_duplicate.return_value = None
        self.vector_store.get_item_version.return_value = None
        self.vector_store.get_library_metadata.return_value = None
        self.vector_store.count_library_chunks.return_value = 0
        app.dependency_overrides[dependencies.get_vector_store] = lambda: self.vector_store

        pending_upload_cache.write_entry(
            self.data_path, "u1", "ATT1", b"cached bytes",
            {"item_key": "ITEM1", "mime_type": "application/pdf", "item_version": 3,
             "attachment_version": 1, "title": "T", "authors": [], "year": None,
             "item_type": None, "library_type": "user", "library_name": ""},
        )

    def tearDown(self):
        app.dependency_overrides.clear()
        self._settings_patch.stop()
        self._tmp.cleanup()

    @patch("backend.api.document_upload.DocumentProcessor")
    def test_processes_the_cached_entry_and_removes_it_on_success(self, mock_processor_cls):
        mock_processor = mock_processor_cls.return_value
        proc_result = MagicMock(status="indexed_fresh", chunks_written=3, error_detail=None)
        mock_processor._process_attachment_bytes = MagicMock(return_value=proc_result)

        async def _async_result(*args, **kwargs):
            return proc_result
        mock_processor._process_attachment_bytes.side_effect = _async_result

        response = self.client.post("/api/index/document/cache/u1/ATT1/process-now")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "indexed")
        self.assertFalse(pending_upload_cache.has_entry(self.data_path, "u1", "ATT1"))

    def test_returns_404_for_an_attachment_not_in_the_cache(self):
        response = self.client.post("/api/index/document/cache/u1/NEVER_CACHED/process-now")
        self.assertEqual(response.status_code, 404)

    @patch("backend.api.document_upload.DocumentProcessor")
    def test_keeps_the_entry_cached_on_failure_for_a_later_retry(self, mock_processor_cls):
        mock_processor = mock_processor_cls.return_value
        async def _raise(*args, **kwargs):
            raise RuntimeError("extraction exploded")
        mock_processor._process_attachment_bytes = _raise

        response = self.client.post("/api/index/document/cache/u1/ATT1/process-now")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertTrue(pending_upload_cache.has_entry(self.data_path, "u1", "ATT1"))
        _, meta = pending_upload_cache.read_entry(self.data_path, "u1", "ATT1")
        self.assertEqual(meta["attempts"], 1)
        self.assertIn("extraction exploded", meta["last_error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_cache_upload_endpoints.py::TestProcessNowEndpoint -v`
Expected: FAIL — 404 Not Found (route doesn't exist).

- [ ] **Step 3: Add the endpoint**

Add this import to `backend/api/document_upload.py` alongside the other `hashlib`/`datetime` imports near the top of the file (it's almost certainly already imported for the sync endpoint's `content_hash = hashlib.sha256(...)` call — verify with `grep -n "^import hashlib" backend/api/document_upload.py` before adding a duplicate).

Add the endpoint directly after `upload_document_to_cache` (Task 3):

```python
@router.post(
    "/index/document/cache/{library_id}/{attachment_key}/process-now",
    response_model=DocumentUploadResult,
    summary="Force immediate indexing of a cached deferred upload",
)
async def process_cached_upload_now(
    library_id: str,
    attachment_key: str,
    include_diagnostics: bool = False,
    http_request: Request = None,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Pull one entry out of the pending-upload cache and index it synchronously
    right now, instead of waiting for the library's next autoindex run.

    Removes the cache entry whether this succeeds or fails terminally — a
    failure here surfaces the real error immediately rather than retrying
    silently on a future scheduled run.
    """
    assert_can_access(identity, library_id)

    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    settings = get_settings()
    cached = pending_upload_cache.read_entry(settings.data_path, library_id, attachment_key)
    if cached is None:
        raise HTTPException(status_code=404, detail="No cached upload found for this attachment")
    file_bytes, meta = cached

    doc_metadata = DocumentMetadata(
        library_id=library_id,
        item_key=meta["item_key"],
        attachment_key=attachment_key,
        title=meta.get("title", "Untitled"),
        authors=meta.get("authors", []),
        year=meta.get("year"),
        item_type=meta.get("item_type"),
    )
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)

    result = await _execute_upload(
        file_bytes=file_bytes,
        content_hash=content_hash,
        doc_metadata=doc_metadata,
        library_id=library_id,
        library_type=meta.get("library_type", "user"),
        item_key=meta["item_key"],
        attachment_key=attachment_key,
        mime_type=meta.get("mime_type", "application/pdf"),
        item_version=meta.get("item_version", 0),
        attachment_version=meta.get("attachment_version", 0),
        item_modified=meta.get("zotero_modified", ""),
        library_name=meta.get("library_name", ""),
        vector_store=vector_store,
        embedding_service=embedding_service,
        include_diagnostics=include_diagnostics,
    )

    if result.status == "error":
        pending_upload_cache.record_failure(settings.data_path, library_id, attachment_key, result.message)
    else:
        pending_upload_cache.delete_entry(settings.data_path, library_id, attachment_key)
    return result
```

Note: `http_request: Request = None` must come before the `Depends(...)` parameters in the signature only if required positionally — FastAPI resolves by parameter name/annotation regardless of order for `Depends`/plain types, so keep it exactly as written above (matches the existing sync endpoint's parameter style at `upload_and_index_document`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_cache_upload_endpoints.py -v`
Expected: PASS — all tests in the file (6 total across both test classes).

- [ ] **Step 5: Commit**

```bash
git add backend/api/document_upload.py backend/tests/test_cache_upload_endpoints.py
git commit -m "feat(backend): add process-now endpoint to force-index a cached upload"
```

---

### Task 5: Extend `check-indexed` with queued status

**Files:**
- Modify: `backend/api/document_upload.py`
- Test: Find the existing check-indexed test file first.

- [ ] **Step 1: Find the existing check-indexed tests**

Run: `grep -rl "check-indexed\|check_indexed" backend/tests/`

Add the new tests to whichever file that command lists (most likely `backend/tests/test_document_upload.py` or `backend/tests/test_check_indexed.py` — use the exact existing fixture/mock-setup style already in that file, e.g. its own `_mock_vector_store()`/`TestClient` setup, rather than rebuilding one from scratch).

- [ ] **Step 2: Write the failing tests**

Add this test class to that file, adapting the exact `TestClient`/mock-vector-store construction already used by the other tests in it (the shape below assumes a `_mock_vector_store()` helper and a `self.client`/`self.identity` setup matching the file's existing pattern — match it exactly rather than inventing a new one):

```python
class TestCheckIndexedQueuedStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.identity = MagicMock(user_id=1)
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: self.identity
        self.vector_store = MagicMock()
        self.vector_store.get_item_states_bulk.return_value = {}
        self.vector_store.get_library_metadata.return_value = MagicMock()
        app.dependency_overrides[dependencies.get_vector_store] = lambda: self.vector_store

        self.settings = settings_module.Settings(data_path=self.data_path, testing=True)
        self._settings_patch = patch("backend.api.document_upload.get_settings", return_value=self.settings)
        self._settings_patch.start()

        pending_upload_cache.write_entry(
            self.data_path, "u1", "ATT1", b"x",
            {"item_key": "ITEM1", "mime_type": "application/pdf", "item_version": 1,
             "attachment_version": 1, "library_type": "user", "library_name": ""},
        )

        self.key_store = MagicMock()
        self.key_store.enabled = True
        self.key_store.list_metadata.return_value = [{"targets": ["users/1"]}]
        self._key_store_patch = patch(
            "backend.api.document_upload.AutoIndexKeyStore", return_value=self.key_store
        )
        self._key_store_patch.start()

    def tearDown(self):
        app.dependency_overrides.clear()
        self._settings_patch.stop()
        self._key_store_patch.stop()
        self._tmp.cleanup()

    def test_reports_queued_with_eta_for_a_cached_attachment(self):
        response = self.client.post(
            "/api/libraries/u1/check-indexed",
            json={
                "library_id": "u1",
                "attachments": [{"item_key": "ITEM1", "attachment_key": "ATT1",
                                  "mime_type": "application/pdf", "item_version": 1,
                                  "attachment_version": 1}],
            },
        )
        self.assertEqual(response.status_code, 200)
        statuses = response.json()["statuses"]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["reason"], "queued")
        self.assertFalse(statuses[0]["needs_indexing"])
        self.assertIsNone(statuses[0].get("queue_block_reason"))

    def test_reports_queue_block_reason_when_key_invalid(self):
        self.key_store.list_metadata.return_value = []
        response = self.client.post(
            "/api/libraries/u1/check-indexed",
            json={
                "library_id": "u1",
                "attachments": [{"item_key": "ITEM1", "attachment_key": "ATT1",
                                  "mime_type": "application/pdf", "item_version": 1,
                                  "attachment_version": 1}],
            },
        )
        statuses = response.json()["statuses"]
        self.assertEqual(statuses[0]["reason"], "queued")
        self.assertIsNone(statuses[0].get("eta"))
        self.assertEqual(statuses[0]["queue_block_reason"], "key_invalid")

    def test_non_cached_attachments_are_unaffected(self):
        response = self.client.post(
            "/api/libraries/u1/check-indexed",
            json={
                "library_id": "u1",
                "attachments": [{"item_key": "ITEM2", "attachment_key": "ATT_NOT_CACHED",
                                  "mime_type": "application/pdf", "item_version": 1,
                                  "attachment_version": 1}],
            },
        )
        statuses = response.json()["statuses"]
        self.assertEqual(statuses[0]["reason"], "not_indexed")
```

Add these imports at the top of the test file if not already present: `import tempfile`, `from pathlib import Path`, `from unittest.mock import patch`, `from backend.services import pending_upload_cache`, `from backend.config import settings as settings_module`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest -k TestCheckIndexedQueuedStatus -v`
Expected: FAIL — `KeyError`/`AssertionError` since `reason` is never `"queued"` yet (statuses come back `"not_indexed"`).

- [ ] **Step 4: Implement**

In `backend/api/document_upload.py`, add fields to `AttachmentIndexStatus` (around line 169-177):

```python
class AttachmentIndexStatus(BaseModel):
    """Per-attachment result returned by the check endpoint."""

    item_key: str
    attachment_key: str
    needs_indexing: bool
    reason: str  # "not_indexed" | "version_changed" | "up_to_date" | "queued"
    needs_metadata_update: bool = False  # True when schema_version < CURRENT_SCHEMA_VERSION
    eta: Optional[str] = None  # set only when reason == "queued"
    queue_block_reason: Optional[str] = None  # None | "paused" | "key_invalid"; set only when reason == "queued"
```

In the `check_indexed` handler, after the existing `for att in request.attachments:` loop finishes building `statuses` (right after line 592's closing of that loop, before the `has_indexed_content` block), overlay queued status for anything in the cache:

```python
    settings = get_settings()
    cached_keys = {
        entry["attachment_key"]
        for entry in pending_upload_cache.list_entries(settings.data_path, library_id)
    }
    if cached_keys:
        key_store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        queue_status = pending_upload_cache.get_queue_status(settings, library_id, key_store)
        for s in statuses:
            if s.attachment_key in cached_keys:
                s.needs_indexing = False
                s.reason = "queued"
                s.eta = queue_status["eta"]
                s.queue_block_reason = queue_status["reason"]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest -k TestCheckIndexedQueuedStatus -v`
Expected: PASS — 3 tests. Then run the full file's existing tests to confirm no regression: `uv run pytest backend/tests/test_document_upload.py -v` (or whichever file Step 1 identified).

- [ ] **Step 6: Commit**

```bash
git add backend/api/document_upload.py backend/tests/
git commit -m "feat(backend): report queued/eta status from check-indexed for cached uploads"
```

---

### Task 6: Autoindex job drains the pending-upload cache

**Files:**
- Modify: `backend/services/cron_indexer.py`
- Test: `backend/tests/test_cron_indexer.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_cron_indexer.py` (match its existing `CronIndexer` construction pattern — it already builds one with a `MagicMock()` vector_store and a patched `create_embedding_service`; adapt the snippet below to that exact fixture style rather than introducing a second one):

```python
class TestDrainPendingUploads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)
        self._settings_patch = patch(
            "backend.services.cron_indexer.get_settings",
            return_value=MagicMock(data_path=self.data_path),
        )
        self._settings_patch.start()

        pending_upload_cache.write_entry(
            self.data_path, "u1", "ATT_OK", b"good bytes",
            {"item_key": "ITEM_OK", "mime_type": "application/pdf", "item_version": 1,
             "attachment_version": 1, "title": "T", "authors": [], "library_type": "user",
             "library_name": "users/1"},
        )
        pending_upload_cache.write_entry(
            self.data_path, "u1", "ATT_FAIL", b"bad bytes",
            {"item_key": "ITEM_FAIL", "mime_type": "application/pdf", "item_version": 1,
             "attachment_version": 1, "title": "T", "authors": [], "library_type": "user",
             "library_name": "users/1"},
        )

    def tearDown(self):
        self._settings_patch.stop()
        self._tmp.cleanup()

    @patch("backend.services.cron_indexer._execute_upload_impl")
    async def test_drains_successful_entries_and_keeps_failed_ones(self, mock_execute):
        async def fake_execute(**kwargs):
            if kwargs["attachment_key"] == "ATT_OK":
                return MagicMock(status="indexed", message="ok")
            return MagicMock(status="error", message="boom")
        mock_execute.side_effect = fake_execute

        indexer = CronIndexer(
            targets={"users/1": {"zotero_key": "k", "embedding_key": "e", "fingerprint": "fp"}},
            vector_store=MagicMock(), lock_file=str(self.data_path / "lock"),
            status_file=str(self.data_path / "status.json"), log=MagicMock(),
        )
        embedding_service = MagicMock()
        drained, failed = await indexer._drain_pending_uploads(
            indexer.parse_slug("users/1"), embedding_service
        )

        self.assertEqual(drained, 1)
        self.assertEqual(failed, 1)
        self.assertFalse(pending_upload_cache.has_entry(self.data_path, "u1", "ATT_OK"))
        self.assertTrue(pending_upload_cache.has_entry(self.data_path, "u1", "ATT_FAIL"))
        _, meta = pending_upload_cache.read_entry(self.data_path, "u1", "ATT_FAIL")
        self.assertEqual(meta["attempts"], 1)

    async def test_no_op_when_nothing_cached_for_the_library(self):
        indexer = CronIndexer(
            targets={"users/2": {"zotero_key": "k", "embedding_key": "e", "fingerprint": "fp"}},
            vector_store=MagicMock(), lock_file=str(self.data_path / "lock2"),
            status_file=str(self.data_path / "status2.json"), log=MagicMock(),
        )
        drained, failed = await indexer._drain_pending_uploads(
            indexer.parse_slug("users/2"), MagicMock()
        )
        self.assertEqual((drained, failed), (0, 0))
```

Add these imports at the top of `backend/tests/test_cron_indexer.py` if not already present: `import tempfile`, `from pathlib import Path`, `from backend.services import pending_upload_cache`.

`CronIndexer.__init__`'s exact signature (`backend/services/cron_indexer.py:168-178`) is `(self, targets: dict[str, dict], vector_store: VectorStore, lock_file: Path, status_file: Path, log: logging.Logger, mode: Literal["auto","incremental","full"] = "auto", max_items: Optional[int] = None, progress_update_interval: int = 10, key_store: Optional[AutoIndexKeyStore] = None)` — `lock_file`/`status_file` are typed `Path`, so pass `Path` objects, not strings. Fix the two constructor calls in the test above to pass `lock_file=self.data_path / "lock"` / `status_file=self.data_path / "status.json"` (and `"lock2"`/`"status2.json"` for the second test) instead of `str(...)`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_cron_indexer.py -k TestDrainPendingUploads -v`
Expected: FAIL — `AttributeError: 'CronIndexer' object has no attribute '_drain_pending_uploads'`

- [ ] **Step 3: Implement the drain step**

Add these imports to the top of `backend/services/cron_indexer.py`, alongside the existing `backend.services.*` imports:

```python
import hashlib
from backend.api.document_upload import _execute_upload_impl
from backend.models.document import DocumentMetadata
from backend.services import pending_upload_cache
```

Add this method to the `CronIndexer` class, right before `_index_slug` (so it's defined before its first caller in reading order):

```python
    async def _drain_pending_uploads(self, slug_info: SlugInfo, embedding_service) -> tuple[int, int]:
        """Process this library's deferred-upload cache through the normal
        extract+embed+store pipeline, using this run's own embedding_service
        (the library owner's stored key) and vector_store.

        Returns (drained_count, failed_count). A failed entry stays cached
        with attempts/last_error updated, to retry on the next run.
        """
        data_path = get_settings().data_path
        entries = pending_upload_cache.list_entries(data_path, slug_info.library_id)
        drained = 0
        failed = 0
        for entry in entries:
            attachment_key = entry["attachment_key"]
            cached = pending_upload_cache.read_entry(data_path, slug_info.library_id, attachment_key)
            if cached is None:
                continue
            file_bytes, meta = cached
            doc_metadata = DocumentMetadata(
                library_id=slug_info.library_id,
                item_key=meta["item_key"],
                attachment_key=attachment_key,
                title=meta.get("title", "Untitled"),
                authors=meta.get("authors", []),
                year=meta.get("year"),
                item_type=meta.get("item_type"),
            )
            try:
                result = await _execute_upload_impl(
                    file_bytes=file_bytes,
                    content_hash=hashlib.sha256(file_bytes).hexdigest(),
                    doc_metadata=doc_metadata,
                    library_id=slug_info.library_id,
                    library_type=meta.get("library_type", slug_info.library_type),
                    item_key=meta["item_key"],
                    attachment_key=attachment_key,
                    mime_type=meta.get("mime_type", "application/pdf"),
                    item_version=meta.get("item_version", 0),
                    attachment_version=meta.get("attachment_version", 0),
                    item_modified=meta.get("zotero_modified", ""),
                    library_name=meta.get("library_name", slug_info.slug),
                    vector_store=self.vector_store,
                    embedding_service=embedding_service,
                )
            except Exception as exc:  # pragma: no cover - defensive, pipeline already catches its own errors
                self.log.error("Drain of cached upload %s/%s raised: %s", slug_info.library_id, attachment_key, exc)
                pending_upload_cache.record_failure(data_path, slug_info.library_id, attachment_key, str(exc))
                failed += 1
                continue
            if result.status == "error":
                pending_upload_cache.record_failure(data_path, slug_info.library_id, attachment_key, result.message)
                failed += 1
            else:
                pending_upload_cache.delete_entry(data_path, slug_info.library_id, attachment_key)
                drained += 1
        if drained or failed:
            self.log.info(
                "Drained pending uploads for %s: %d indexed, %d failed (retained)",
                slug_info.slug, drained, failed,
            )
        return drained, failed
```

Call it from `_index_slug`, right after `embedding_service = create_embedding_service(preset.embedding, api_key=target["embedding_key"])` (line 491) and before the `try:` block that starts the normal web-API-driven indexing:

```python
        preset = get_settings().get_hardware_preset()
        embedding_service = create_embedding_service(preset.embedding, api_key=target["embedding_key"])

        pending_drained, pending_failed = await self._drain_pending_uploads(slug_info, embedding_service)
        if pending_drained or pending_failed:
            entry = status["slugs"][slug_info.slug]
            entry["pending_drained"] = pending_drained
            entry["pending_errors"] = pending_failed
            self._write_status(status)

        try:
            async with web_api:
```

(The second line of that snippet, `try:`/`async with web_api:`, is the existing code already there — this just shows where the new block is inserted relative to it; don't duplicate the `try:` line.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_cron_indexer.py -v`
Expected: PASS — all tests in the file, including the 2 new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/services/cron_indexer.py backend/tests/test_cron_indexer.py
git commit -m "feat(backend): drain each library's pending-upload cache during its autoindex run"
```

---

### Task 7: Operational doc note on disk usage

**Files:**
- Modify: `docs/cron-indexing.md`

- [ ] **Step 1: Add a one-line callout**

Add a short paragraph to `docs/cron-indexing.md`, in a sensible existing section discussing disk/storage (search the file for its disk-space-related section, e.g. near any existing mention of `podman image prune` or storage paths — if none exists, add a new small subsection near the "Key validation and pruning" section):

```markdown
### Pending-upload cache disk usage

Attachments the plugin uploads via the deferred ("Search & Fix Selected",
not "Fix & Index Selected Now") path are cached on disk at
`<DEPLOY_DATA_DIR>/system/pending_uploads/<library_id>/` until the next
autoindex run processes them. A failed entry stays cached and retries on
every subsequent run rather than being dropped, so a persistently broken
attachment (and its raw bytes) can accumulate there indefinitely. Include
this path when checking disk usage (`du -sh`) alongside the other
system-state paths already covered above.
```

- [ ] **Step 2: Commit**

```bash
git add docs/cron-indexing.md
git commit -m "docs: note pending-upload cache disk usage in cron indexing guide"
```

---

## Plugin

### Task 8: `RemoteIndexer._uploadAttachment` gains a `defer` mode

**Files:**
- Modify: `plugin/src/remote_indexer.js`
- Test: `plugin/test/remote_indexer.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/remote_indexer.test.js`, reusing the file's existing `makeUploader` helper (around line 86-112):

```js
test('_uploadAttachment with defer:true posts to the cache endpoint and returns queued status without polling', async () => {
  const { ri, bodies, urls } = makeDeferUploader({ eta: '2026-10-06T15:30:00Z', reason: null });
  const result = await ri.call({ defer: true });
  assert.strictEqual(result.queued, true);
  assert.strictEqual(result.eta, '2026-10-06T15:30:00Z');
  assert.strictEqual(result.queueBlockReason, null);
  assert.ok(urls[0].endsWith('/api/index/document/cache'));
});

test('_uploadAttachment with defer:true surfaces a block reason when present', async () => {
  const { ri } = makeDeferUploader({ eta: null, reason: 'key_invalid' });
  const result = await ri.call({ defer: true });
  assert.strictEqual(result.eta, null);
  assert.strictEqual(result.queueBlockReason, 'key_invalid');
});

test('_uploadAttachment with defer:false (default) still posts to the async endpoint', async () => {
  const { call, urls } = makeUploader({ status: 'indexed', chunks_added: 1 });
  await call();
  assert.ok(urls[0].endsWith('/api/index/document/async'));
});
```

This needs two small additions to the test file's existing helpers:

1. `makeUploader` (around line 86-112) currently doesn't capture the requested URL — add a `urls` array alongside its existing `bodies` array, and return it:

```js
function makeUploader(result, { asyncStatus = 'done' } = {}) {
  const bodies = [];
  const urls = [];
  // ...unchanged context/vm setup...
  ri._apiFetch = async (_m, url, opts) => { urls.push(url); bodies.push(opts.body.f); return { status: 200, json: async () => ({ status: asyncStatus, result }) }; };
  // ...unchanged att/call setup...
  return { call, bodies, urls };
}
```

2. Add a new `makeDeferUploader` helper right after `makeUploader`, for the `/cache` response shape (no `task_id`/polling, just `{status, eta, reason}`):

```js
function makeDeferUploader(cacheResponse) {
  const urls = [];
  const zotero = { ZoteroRAG: { _extractAuthors: () => [], _extractYear: () => null } };
  const context = {
    Zotero: zotero,
    IOUtils: { read: async () => new Uint8Array([1, 2, 3]) },
    FormData: class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } },
    Blob: class {},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'remote_indexer.js' });
  const ri = context.RemoteIndexer;
  ri._apiFetch = async (_m, url, _opts) => { urls.push(url); return { status: 200, json: async () => ({ status: 'queued', ...cacheResponse }) }; };
  const att = { attachment_key: 'A', item_key: 'I', mime_type: 'application/pdf', item_version: 1, attachment_version: 1,
    filePath: '/x.pdf', zoteroItem: {}, parentItem: { getField: () => 't', itemType: 'book', dateModified: 'd' } };
  const call = (extra = {}) => ri._uploadAttachment({ att, libraryId: 'u1', libraryType: 'user', backendURL: 'http://x', userId: 1, getAuthHeaders: () => ({}), log: () => {}, ...extra });
  return { ri, urls, call };
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: FAIL — `result.queued` is `undefined` (defer isn't handled yet), and `urls` doesn't exist on the plain `makeUploader` result yet.

- [ ] **Step 3: Implement `defer` in `_uploadAttachmentInner`**

In `plugin/src/remote_indexer.js`, update the JSDoc and destructured parameters of both `_uploadAttachment` (line ~786-808) and `_uploadAttachmentInner` (line ~841) to add `defer = false`:

```js
/**
 * @param {Object} opts
 * ...existing params...
 * @param {boolean} [opts.defer=false] - When true, upload to the deferred-indexing
 *   cache instead of indexing now; returns {queued, eta, queueBlockReason} immediately.
 * @returns {Promise<{rateLimitHeaders: Record<string,string>|null, queued?: boolean, eta?: string|null, queueBlockReason?: string|null, parseError?: boolean, skippedEmpty?: boolean, skippedTimeout?: boolean, errorDetail?: string|null, diagnostics?: any, pluginDiag?: any}>}
 */
async _uploadAttachment({ att, libraryId, libraryType, backendURL, userId, getAuthHeaders, log, signal, onStatusUpdate = null, timeoutMultiplier = 1.0, includeDiagnostics = false, defer = false }) {
```

Thread `defer` straight through to `_uploadAttachmentInner`'s own destructured parameters in the same way, and into the call `_uploadAttachmentInner({...})` inside `_uploadAttachment`'s body.

In `_uploadAttachmentInner`, change the URL (around line 895) and add an early-return branch right after the response is parsed (around line 920), before the existing `if (asyncData.status === 'processing' ...)` branch:

```js
      response = await this._apiFetch('POST', `${backendURL}/api/index/document/${defer ? 'cache' : 'async'}`, {
        headers: getAuthHeaders(),
        body: formData,
        signal,
        timeout: 60 * 1000,
      });
```

```js
    const asyncData = await response.json();
    if (defer) {
      // /cache never indexes — nothing to poll, no diagnostics possible.
      return { rateLimitHeaders: null, queued: true, eta: asyncData.eta ?? null, queueBlockReason: asyncData.reason ?? null };
    }
    let result;
    if (asyncData.status === 'processing' && asyncData.task_id) {
```

(The last line above, `if (asyncData.status === 'processing' ...)`, is the existing code already there — the new `if (defer) {...}` block is inserted immediately before it.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: PASS — all tests in the file, including the 3 new ones.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/remote_indexer.js plugin/test/remote_indexer.test.js
git commit -m "feat(plugin): add defer mode to RemoteIndexer._uploadAttachment"
```

---

### Task 9: `RemoteIndexer._processQueuedNow` + shared result mapping

**Files:**
- Modify: `plugin/src/remote_indexer.js`
- Test: `plugin/test/remote_indexer.test.js`

- [ ] **Step 1: Write the failing tests**

```js
test('_processQueuedNow posts to the process-now endpoint and maps a success result', async () => {
  const { ri } = makeDeferUploader({});
  ri._apiFetch = async (_m, url, _opts) => {
    assert.ok(url.endsWith('/api/index/document/cache/u1/ATT1/process-now'));
    return { status: 200, json: async () => ({ status: 'indexed', chunks_added: 3, library_id: 'u1', item_key: 'I', attachment_key: 'ATT1' }) };
  };
  const result = await ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} });
  assert.strictEqual(result.parseError, undefined);
  assert.strictEqual(result.skippedEmpty, undefined);
});

test('_processQueuedNow maps skipped_parse_error the same way as a polled upload', async () => {
  const { ri } = makeDeferUploader({});
  ri._apiFetch = async () => ({ status: 200, json: async () => ({ status: 'skipped_parse_error', error_detail: 'binary data', library_id: 'u1', item_key: 'I', attachment_key: 'ATT1' }) });
  const result = await ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} });
  assert.strictEqual(result.parseError, true);
  assert.strictEqual(result.errorDetail, 'binary data');
});

test('_processQueuedNow throws on an error result, same as a polled upload', async () => {
  const { ri } = makeDeferUploader({});
  ri._apiFetch = async () => ({ status: 200, json: async () => ({ status: 'error', message: 'boom', library_id: 'u1', item_key: 'I', attachment_key: 'ATT1' }) });
  await assert.rejects(
    () => ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} }),
    /boom/
  );
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: FAIL — `ri._processQueuedNow is not a function`

- [ ] **Step 3: Extract the shared mapping helper and add `_processQueuedNow`**

In `plugin/src/remote_indexer.js`, find the terminal result-mapping block inside `_uploadAttachmentInner` (the `if (result.status === 'error') { throw ... }` through the final `return { rateLimitHeaders, ...diagFields };`, roughly lines 952-969). Extract it into a new method, then call that method from both the existing non-defer completion path and the new `_processQueuedNow`:

```js
  /**
   * Map a terminal DocumentUploadResult (from polling or process-now) to the
   * shape callers expect. Throws for status "error".
   * @param {any} result
   * @param {Record<string,string>|null} rateLimitHeaders
   * @param {any} pluginDiag
   * @param {boolean} includeDiagnostics
   */
  _mapTerminalResult(result, rateLimitHeaders, pluginDiag, includeDiagnostics) {
    const diagFields = pluginDiag ? { diagnostics: result.diagnostics ?? null, pluginDiag } : {};
    if (result.status === 'error') {
      const err = new Error(result.message || 'Upload failed');
      /** @type {any} */ (err).diagnostics = result.diagnostics ?? null;
      throw err;
    }
    if (result.status === 'skipped_parse_error') {
      return { rateLimitHeaders, parseError: true, errorDetail: result.error_detail || null, ...diagFields };
    }
    if (result.status === 'skipped_empty') {
      return { rateLimitHeaders, skippedEmpty: true, errorDetail: result.error_detail || null, ...diagFields };
    }
    if (result.status === 'skipped_timeout') {
      return { rateLimitHeaders, skippedTimeout: true, errorDetail: result.error_detail || null, ...diagFields };
    }
    return { rateLimitHeaders, ...diagFields };
  },
```

Replace the original inline block in `_uploadAttachmentInner` with a call to it: `return this._mapTerminalResult(result, rateLimitHeaders, pluginDiag, includeDiagnostics);` (keeping whatever `rateLimitHeaders`/`pluginDiag` variables that block already had in scope — do not change their computation, only replace the trailing if/else chain with this one call).

Add `_processQueuedNow` as a new top-level method on `RemoteIndexer`, near `_uploadAttachment`:

```js
  /**
   * Force immediate indexing of an attachment already sitting in the
   * backend's deferred-upload cache — no file read, no FormData, since the
   * bytes are already server-side.
   * @param {Object} opts
   * @param {string} opts.libraryId
   * @param {string} opts.attachmentKey
   * @param {string} opts.backendURL
   * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
   * @param {function(string): void} opts.log
   * @param {AbortSignal} [opts.signal]
   * @param {boolean} [opts.includeDiagnostics=false]
   */
  async _processQueuedNow({ libraryId, attachmentKey, backendURL, getAuthHeaders, log, signal, includeDiagnostics = false }) {
    const url = `${backendURL}/api/index/document/cache/${encodeURIComponent(libraryId)}/${encodeURIComponent(attachmentKey)}/process-now`
      + (includeDiagnostics ? '?include_diagnostics=true' : '');
    const response = await this._apiFetch('POST', url, { headers: getAuthHeaders(), signal, timeout: 10 * 60 * 1000 });
    if (response.status === 404) {
      throw new Error('Cached upload not found — it may already have been indexed by a scheduled run.');
    }
    const result = await response.json();
    return this._mapTerminalResult(result, null, includeDiagnostics ? {} : null, includeDiagnostics);
  },
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: PASS — all tests, including the 3 new ones, and all pre-existing ones (confirming the extraction didn't change behavior).

- [ ] **Step 5: Commit**

```bash
git add plugin/src/remote_indexer.js plugin/test/remote_indexer.test.js
git commit -m "feat(plugin): add RemoteIndexer._processQueuedNow for forcing a cached upload"
```

---

### Task 10: Thread `defer` through the zotero-rag.js upload wrappers

**Files:**
- Modify: `plugin/src/zotero-rag.js`
- Test: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/zotero-rag.test.js`, following its existing `makeStubs`/`loadPlugin` pattern (reuse the `plugin.getCurrentZoteroUserId = () => 1;` stub already established there for these wrapper functions):

```js
test('_uploadDownloadFailedAttachment returns queued:true when the upload is deferred', async () => {
  const { zotero } = makeStubs();
  const plugin = loadPlugin(zotero);
  plugin.getCurrentZoteroUserId = () => 1;
  let capturedDefer;
  const originalUpload = RemoteIndexer._uploadAttachment;
  RemoteIndexer._uploadAttachment = async (opts) => { capturedDefer = opts.defer; return { queued: true, eta: '2026-10-06T15:30:00Z', queueBlockReason: null }; };
  try {
    const result = await plugin._uploadDownloadFailedAttachment(
      { key: 'A', attachmentContentType: 'application/pdf', version: 1 },
      { key: 'I', version: 1 },
      1,
      { defer: true },
    );
    assert.strictEqual(capturedDefer, true);
    assert.strictEqual(result.fixed, false);
    assert.strictEqual(result.queued, true);
    assert.strictEqual(result.eta, '2026-10-06T15:30:00Z');
  } finally {
    RemoteIndexer._uploadAttachment = originalUpload;
  }
});

test('processQueuedAttachmentNow calls RemoteIndexer._processQueuedNow and maps success to fixed:true', async () => {
  const { zotero } = makeStubs();
  const plugin = loadPlugin(zotero);
  plugin.getCurrentZoteroUserId = () => 1;
  const originalProcessNow = RemoteIndexer._processQueuedNow;
  RemoteIndexer._processQueuedNow = async () => ({});
  try {
    const result = await plugin.processQueuedAttachmentNow(
      { key: 'A', attachmentContentType: 'application/pdf', version: 1 },
      { key: 'I', version: 1 },
      1,
    );
    assert.strictEqual(result.fixed, true);
  } finally {
    RemoteIndexer._processQueuedNow = originalProcessNow;
  }
});
```

Check how `RemoteIndexer` is exposed to this test file's `vm` context (it's loaded as a global inside the same `vm.createContext`/`loadPlugin` setup used elsewhere in this file for `zotero-rag.js`'s own `RemoteIndexer._uploadAttachment` calls) — reuse that exact mechanism to stub `RemoteIndexer._uploadAttachment`/`_processQueuedNow` rather than a bare top-level `RemoteIndexer` reference, matching however the file's other `RemoteIndexer._uploadAttachment` stub tests already do this (search the file for an existing test that stubs `RemoteIndexer._uploadAttachment` and copy its exact setup).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `capturedDefer` is `undefined` (not threaded yet), and `plugin.processQueuedAttachmentNow is not a function`.

- [ ] **Step 3: Thread `defer` through the three retry wrappers and `_uploadDownloadFailedAttachment`**

In `plugin/src/zotero-rag.js`, update each of these four functions' options destructuring and their `RemoteIndexer._uploadAttachment({...})` call to add `defer`:

`retryTimeoutSkippedAttachment` (line 2403):
```js
	async retryTimeoutSkippedAttachment(attachmentItem, parentItem, libraryID, { includeDiagnostics = false, defer = false } = {}) {
```
...and in its `RemoteIndexer._uploadAttachment({...})` call (line 2420-2430), add `defer,` alongside `includeDiagnostics,`. Then, right before the existing `if (result.skippedTimeout) { ... }` check (line 2433), add:
```js
			if (result.queued) {
				return { fixed: false, stillTimedOut: false, queued: true, eta: result.eta ?? null, queueBlockReason: result.reason ?? null, ...diag };
			}
```

`retryEmptyTextSkippedAttachment` (line 2463): same pattern — add `defer = false` to the destructured opts, `defer` to the `_uploadAttachment` call, and before `if (result.skippedEmpty) { ... }` (line 2492):
```js
			if (result.queued) {
				return { fixed: false, stillEmpty: false, queued: true, eta: result.eta ?? null, queueBlockReason: result.reason ?? null, ...diag };
			}
```

`_uploadDownloadFailedAttachment` (line 2953): same pattern — add `defer = false` to the destructured opts, `defer` to the `_uploadAttachment` call, and before `if (result.parseError) { ... }` (line 2982):
```js
			if (result.queued) {
				return { fixed: false, queued: true, eta: result.eta ?? null, queueBlockReason: result.reason ?? null, ...diag };
			}
```

`retryDownloadFailedAttachment` (line 3009) needs no change — it already spreads `...uploadResult` from `_uploadDownloadFailedAttachment`'s return into its own, so the new `queued`/`eta`/`queueBlockReason` fields pass through automatically. Its JSDoc return type should still be updated to mention them:
```js
	 * @returns {Promise<{fixed: boolean, stillMissing: boolean, queued?: boolean, eta?: string|null, queueBlockReason?: string|null, error?: string, backendDiag?: any, pluginDiag?: any}>}
```

- [ ] **Step 4: Add `processQueuedAttachmentNow`**

Add this new method to `plugin/src/zotero-rag.js`, right after `_uploadDownloadFailedAttachment`:

```js
	/**
	 * Force immediate indexing of an attachment already sitting in the
	 * backend's deferred-upload cache. No search, no download — the bytes
	 * are already server-side; this is what "Fix & Index Selected Now"
	 * calls for a row whose status is already 'queued'.
	 * @param {*} attachmentItem - Zotero attachment item
	 * @param {*} parentItem - Zotero parent item (or the attachment itself if standalone)
	 * @param {number} libraryID - Zotero internal library ID
	 * @param {{includeDiagnostics?: boolean}} [opts]
	 * @returns {Promise<{fixed: boolean, error?: string, backendDiag?: any, pluginDiag?: any}>}
	 */
	async processQueuedAttachmentNow(attachmentItem, parentItem, libraryID, { includeDiagnostics = false } = {}) {
		try {
			const backendLibraryId = this.getBackendLibraryId(libraryID);
			const result = await RemoteIndexer._processQueuedNow({
				libraryId: backendLibraryId,
				attachmentKey: attachmentItem.key,
				backendURL: this.backendURL,
				getAuthHeaders: (extra) => this.getAuthHeaders(extra),
				log: (msg) => this.log(msg),
				includeDiagnostics,
			});
			const diag = includeDiagnostics ? { backendDiag: result.diagnostics ?? null, pluginDiag: result.pluginDiag ?? null } : {};
			if (result.parseError) {
				return { fixed: false, error: result.errorDetail || 'File cannot be parsed (binary data)', ...diag };
			}
			if (result.skippedEmpty) {
				return { fixed: false, error: result.errorDetail || 'No text could be extracted', ...diag };
			}
			if (result.skippedTimeout) {
				return { fixed: false, error: result.errorDetail || 'Text extraction timed out', ...diag };
			}
			return { fixed: true, ...diag };
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			return { fixed: false, error: msg, ...errorDiag(e, includeDiagnostics) };
		}
	},
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS — all tests, including the 2 new ones.

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat(plugin): thread defer mode through Fix Unavailable upload wrappers"
```

---

### Task 11: `getQueuedStatusMap` for populating queued rows

**Files:**
- Modify: `plugin/src/zotero-rag.js`
- Test: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Write the failing test**

```js
test('getQueuedStatusMap returns only attachments the backend reports as queued', async () => {
  const { zotero } = makeStubs();
  const plugin = loadPlugin(zotero);
  const originalCheckIndexed = RemoteIndexer._checkIndexed;
  RemoteIndexer._checkIndexed = async () => ([
    { item_key: 'I1', attachment_key: 'A1', needs_indexing: false, reason: 'queued', eta: '2026-10-06T15:30:00Z', queue_block_reason: null },
    { item_key: 'I2', attachment_key: 'A2', needs_indexing: true, reason: 'not_indexed' },
  ]);
  try {
    const items = [
      { parentItem: { key: 'I1', version: 1 }, attachmentItem: { key: 'A1', version: 1, attachmentContentType: 'application/pdf' } },
      { parentItem: { key: 'I2', version: 1 }, attachmentItem: { key: 'A2', version: 1, attachmentContentType: 'application/pdf' } },
    ];
    const map = await plugin.getQueuedStatusMap(1, items);
    assert.strictEqual(map.size, 1);
    assert.deepStrictEqual(map.get('A1'), { eta: '2026-10-06T15:30:00Z', queueBlockReason: null });
    assert.strictEqual(map.has('A2'), false);
  } finally {
    RemoteIndexer._checkIndexed = originalCheckIndexed;
  }
});

test('getQueuedStatusMap returns an empty map for an empty item list without calling the backend', async () => {
  const { zotero } = makeStubs();
  const plugin = loadPlugin(zotero);
  let called = false;
  const originalCheckIndexed = RemoteIndexer._checkIndexed;
  RemoteIndexer._checkIndexed = async () => { called = true; return []; };
  try {
    const map = await plugin.getQueuedStatusMap(1, []);
    assert.strictEqual(map.size, 0);
    assert.strictEqual(called, false);
  } finally {
    RemoteIndexer._checkIndexed = originalCheckIndexed;
  }
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `plugin.getQueuedStatusMap is not a function`

- [ ] **Step 3: Implement**

Add this method to `plugin/src/zotero-rag.js`, near `getBackendLibraryId`:

```js
	/**
	 * Ask the backend which of these attachments are currently sitting in the
	 * deferred-upload cache ("Waiting to be indexed"), via check-indexed.
	 * @param {number} libraryID - Zotero internal library ID
	 * @param {Array<{parentItem: *, attachmentItem: *}>} items
	 * @returns {Promise<Map<string, {eta: string|null, queueBlockReason: string|null}>>}
	 */
	async getQueuedStatusMap(libraryID, items) {
		const map = new Map();
		if (!items || items.length === 0) return map;
		const backendLibraryId = this.getBackendLibraryId(libraryID);
		const attachments = items.map(info => ({
			item_key: info.parentItem ? info.parentItem.key : info.attachmentItem.key,
			attachment_key: info.attachmentItem.key,
			mime_type: info.attachmentItem.attachmentContentType || 'application/pdf',
			item_version: info.parentItem ? (info.parentItem.version || 0) : (info.attachmentItem.version || 0),
			attachment_version: info.attachmentItem.version || 0,
		}));
		const statuses = await RemoteIndexer._checkIndexed(
			backendLibraryId, attachments, this.backendURL,
			(extra) => this.getAuthHeaders(extra), (msg) => this.log(msg),
		);
		for (const s of statuses) {
			if (s.reason === 'queued') {
				map.set(s.attachment_key, { eta: s.eta ?? null, queueBlockReason: s.queue_block_reason ?? null });
			}
		}
		return map;
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS — all tests, including the 2 new ones.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat(plugin): add getQueuedStatusMap for the Fix dialog's queued-row display"
```

---

### Task 12: Split-button markup and `.status-queued` CSS

**Files:**
- Modify: `plugin/src/fix-unavailable.xhtml`

- [ ] **Step 1: Add the CSS**

In the `<style>` block, add a `.status-queued` rule next to the other status colors (after line 62's `.status-searching` rule):

```css
    .status-queued   { color: #b36b00; }
```

Add split-button styles after the existing `.dialog-button.danger:disabled` rule (end of the style block, before `</style>`):

```css
    /* Split button: primary action + dropdown alternative */
    .split-button { position: relative; display: inline-flex; }
    .split-button .dialog-button.primary { border-radius: 4px 0 0 4px; }
    .dropdown-toggle { border-radius: 0 4px 4px 0; border-left: 1px solid rgba(255,255,255,0.4); padding: 6px 8px; min-width: auto; }
    .dropdown-menu { position: absolute; bottom: 100%; right: 0; margin-bottom: 4px; background: #fff; border: 1px solid #ccc; border-radius: 4px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); min-width: 240px; z-index: 10; }
    .dropdown-menu[hidden] { display: none; }
    .dropdown-item { display: block; width: 100%; text-align: left; padding: 8px 12px; border: none; background: none; cursor: pointer; font-family: inherit; font-size: 13px; color: #333; }
    .dropdown-item:hover { background: #f0f0f0; }
    .dropdown-item .slow-label { color: #999; font-size: 11px; }
```

- [ ] **Step 2: Replace the footer's single Fix button with the split button**

Replace this line (114-124's `search-btn` element):

```html
      <button id="search-btn" type="button" class="dialog-button primary" disabled="true">Search &amp; Fix Selected</button>
```

with:

```html
      <div class="split-button">
        <button id="search-btn" type="button" class="dialog-button primary" disabled="true">Search &amp; Fix Selected</button>
        <button id="fix-dropdown-btn" type="button" class="dialog-button primary dropdown-toggle" disabled="true" style="display:none" title="More options" aria-haspopup="true">&#x25BE;</button>
        <div id="fix-dropdown-menu" class="dropdown-menu" hidden="true">
          <button id="fix-index-now-btn" type="button" class="dropdown-item">Fix &amp; Index Selected Now <span class="slow-label">(slow)</span></button>
        </div>
      </div>
```

(`fix-dropdown-btn` starts `style="display:none"` — Task 13 makes `_updateSplitButtonVisibility()` reveal it only for libraries with automatic indexing configured.)

- [ ] **Step 3: Commit**

```bash
git add plugin/src/fix-unavailable.xhtml
git commit -m "feat(plugin): add split-button markup and queued-status styling to Fix Unavailable dialog"
```

---

### Task 13: Wire the split button and queued-row display into `fix-unavailable.js`

**Files:**
- Modify: `plugin/src/fix-unavailable.js`
- Test: `plugin/test/fix-unavailable.test.js`

This is the largest task — it touches `init()`, `populateTable()`, `updateActionButtons()`, `_setAllButtonsDisabled()`, and `searchAndFix()`. Do it in the sub-steps below in order, running the full test file after each one.

- [ ] **Step 1: `init()` — wire the dropdown and change the default button's handler**

Replace this line (98):
```js
		document.getElementById('search-btn').addEventListener('click', () => this.searchAndFix());
```
with:
```js
		document.getElementById('search-btn').addEventListener('click', () => this.searchAndFix({ forceIndexNow: false }));
		document.getElementById('fix-dropdown-btn')?.addEventListener('click', (e) => {
			e.stopPropagation();
			const menu = document.getElementById('fix-dropdown-menu');
			if (menu) menu.hidden = !menu.hidden;
		});
		document.getElementById('fix-index-now-btn')?.addEventListener('click', () => {
			const menu = document.getElementById('fix-dropdown-menu');
			if (menu) menu.hidden = true;
			this.searchAndFix({ forceIndexNow: true });
		});
		document.addEventListener('click', (e) => {
			const menu = document.getElementById('fix-dropdown-menu');
			const toggle = document.getElementById('fix-dropdown-btn');
			if (menu && !menu.hidden && e.target !== toggle && !menu.contains(/** @type {Node} */ (e.target))) menu.hidden = true;
		});
```

- [ ] **Step 2: Add `_updateSplitButtonVisibility` and a `deferCapable` field**

Add `deferCapable: false,` to the object's top-level field declarations (alongside the existing `backendLibraryId: '',` at line 50).

Add this method near `updateActionButtons`:
```js
	/**
	 * Show the dropdown toggle only for libraries with automatic indexing
	 * configured — the only case the deferred/"force now" split actually
	 * applies to. Other libraries keep today's single plain button.
	 * @returns {void}
	 */
	_updateSplitButtonVisibility() {
		const toggle = /** @type {HTMLButtonElement|null} */ (document.getElementById('fix-dropdown-btn'));
		const menu = document.getElementById('fix-dropdown-menu');
		if (toggle) toggle.style.display = this.deferCapable ? '' : 'none';
		if (menu && !this.deferCapable) menu.hidden = true;
	},

	/**
	 * Human-readable text for a 'queued' row.
	 * @param {string|null} eta - ISO 8601 timestamp, or null if blocked
	 * @param {string|null} queueBlockReason - null | "paused" | "key_invalid"
	 * @returns {string}
	 */
	_formatQueuedText(eta, queueBlockReason) {
		if (queueBlockReason === 'paused') return 'Indexing currently paused';
		if (queueBlockReason === 'key_invalid') return 'Indexing paused — automatic indexing key is no longer valid';
		if (!eta) return 'Waiting to be indexed';
		const mins = Math.max(0, Math.round((new Date(eta).getTime() - Date.now()) / 60000));
		return `Waiting to be indexed — next run in ~${mins} min`;
	},
```

- [ ] **Step 3: `populateTable()` — detect `deferCapable` and decorate queued rows**

After the existing pre-set-status loop (the `for (let i = 0; i < this.items.length; i++) { if (item.isParseError) {...} ... }` block ending at line 337), insert:

```js
		this.deferCapable = false;
		try {
			const autoIds = await this.plugin.getAutoIndexedLibraryIds?.();
			this.deferCapable = !!(autoIds && autoIds.has(this.backendLibraryId));
		} catch (_) { this.deferCapable = false; }
		this._updateSplitButtonVisibility();

		if (this.deferCapable && this.items.length > 0 && typeof this.plugin.getQueuedStatusMap === 'function') {
			try {
				const queuedMap = await this.plugin.getQueuedStatusMap(this.libraryID, this.items);
				for (let i = 0; i < this.items.length; i++) {
					const q = queuedMap.get(this.items[i].attachmentItem.key);
					if (q) {
						this.rowStatus.set(i, {
							cssClass: 'queued',
							text: this._formatQueuedText(q.eta, q.queueBlockReason),
							tooltip: q.eta ? `Next scheduled run: ${new Date(q.eta).toLocaleString()}` : '',
						});
					}
				}
			} catch (e) {
				console.error(`fix-unavailable: failed to fetch queued status: ${e}`);
			}
		}
```

- [ ] **Step 4: `updateActionButtons()` and `_setAllButtonsDisabled()` — include the dropdown toggle**

Replace:
```js
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = count === 0 || !hasItems;
```
with:
```js
		const disableFixButtons = count === 0 || !hasItems;
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = disableFixButtons;
		const dropdownBtn = /** @type {HTMLButtonElement|null} */ (document.getElementById('fix-dropdown-btn'));
		if (dropdownBtn) dropdownBtn.disabled = disableFixButtons;
```

In `_setAllButtonsDisabled`, add `'fix-dropdown-btn'` to the iterated id list:
```js
	_setAllButtonsDisabled(disabled) {
		for (const id of ['search-btn', 'delete-btn', 'close-btn', 'refresh-btn', 'fix-dropdown-btn']) {
			/** @type {HTMLButtonElement} */ (document.getElementById(id)).disabled = disabled;
		}
```

- [ ] **Step 5: `searchAndFix()` — accept `forceIndexNow`, compute `defer`, split out already-queued rows**

Change the signature and the top of the function (lines 540-554):
```js
	async searchAndFix({ forceIndexNow = false } = {}) {
		if (this.isRunning) return;
		this.isRunning = true;
		this._setAllButtonsDisabled(true);

		const allSelectedIndices = this.getSelectedIndices();
		const alreadyQueuedIndices = allSelectedIndices.filter(i => this.rowStatus.get(i)?.cssClass === 'queued');
		const indices = allSelectedIndices.filter(i => !alreadyQueuedIndices.includes(i));
		const defer = this.deferCapable && !forceIndexNow;
		const parseErrorIndices  = indices.filter(i => this.items[i].isParseError);
```
(the rest of the existing bucketing lines — `timeoutIndices`, `emptyTextIndices`, `linkedIndices`, `serverFailedIndices`, `importedIndices` — are unchanged, they just now read from the narrowed `indices` instead of a direct `getSelectedIndices()` call, which they already do since `indices` is still the name used.)

- [ ] **Step 6: Thread `defer` into the four upload call sites**

Replace each of these four ternary call sites with a single `opts` object that includes `defer`:

Phase 0 (line 601-603):
```js
					const opts = { defer, ...(collectDebug ? { includeDiagnostics: true } : {}) };
					const result = await this.plugin.retryTimeoutSkippedAttachment(info.attachmentItem, info.parentItem, this.libraryID, opts);
```

Phase 0b (line 657-659): same pattern with `retryEmptyTextSkippedAttachment`.

Phase 1b (line 716-718): same pattern with `retryDownloadFailedAttachment`.

Phase 2 (line 823-825): same pattern with `_uploadDownloadFailedAttachment`.

- [ ] **Step 7: Handle `result.queued` in each of the four phases**

Phase 0 (after the existing `if (result.fixed) {...}` block, before `else if (result.stillTimedOut)`, around line 609-612):
```js
					if (result.fixed) {
						this.setRowStatus(i, 'fixed', 'Fixed (longer timeout)');
						timeoutFixedIndices.push(i);
					} else if (result.queued) {
						this.setRowStatus(i, 'queued', this._formatQueuedText(result.eta, result.queueBlockReason));
						timeoutQueuedIndices.push(i);
					} else if (result.stillTimedOut) {
```
Declare `const timeoutQueuedIndices = [];` alongside the existing `const timeoutFixedIndices = [];` (line 593).

Phase 0b: identical pattern — declare `const emptyTextQueuedIndices = [];` alongside `emptyTextFixedIndices` (line 649), and add the `else if (result.queued)` branch before `else if (result.stillEmpty)` (around line 668).

Phase 1b: declare `const serverFailedQueuedIndices = [];` alongside `serverFailedFixedIndices`/`serverFailedStillMissing` (lines 705-707), and add the branch before `else if (result.stillMissing)` (around line 727):
```js
						if (result.fixed) {
							this.setRowStatus(i, 'fixed', 'Downloaded & indexed');
							serverFailedFixedIndices.push(i);
						} else if (result.queued) {
							this.setRowStatus(i, 'queued', this._formatQueuedText(result.eta, result.queueBlockReason));
							serverFailedQueuedIndices.push(i);
						} else if (result.stillMissing) {
```

Phase 2: declare `const phase2QueuedIndices = [];` alongside `phase2FixedIndices` (line 802), and add the branch inside the `if (result.found && !result.error && info.serverDownloadFailed)` block, after its own nested `uploadResult` is obtained (around line 831):
```js
						if (uploadResult.fixed) {
							this.setRowStatus(i, 'fixed', `Fixed (${result.via}) & indexed`);
							fixed++;
							phase2FixedIndices.push(i);
						} else if (uploadResult.queued) {
							this.setRowStatus(i, 'queued', this._formatQueuedText(uploadResult.eta, uploadResult.queueBlockReason));
							phase2QueuedIndices.push(i);
						} else {
							this.setRowStatus(i, 'error', `Indexing failed: ${uploadResult.error}`, uploadResult.error);
							errors++;
						}
```

- [ ] **Step 8: Prune persistent skip/download-failed records for queued rows too, without removing queued rows from the table**

Change the two `removeSkippedServerItems` calls (lines 629-636 for timeout, 685-692 for empty-text) to include the queued indices:
```js
	if (timeoutFixedIndices.length > 0 || timeoutQueuedIndices.length > 0) {
		try {
			await this.plugin.removeSkippedServerItems(
				this.backendLibraryId, [...timeoutFixedIndices, ...timeoutQueuedIndices].map(i => this.items[i].attachmentItem.key)
			);
		} catch (e) {
			console.error(`fix-unavailable: failed to prune fixed skipped-server entries: ${e}`);
		}
	}
```
(same change for the empty-text block, substituting `emptyTextFixedIndices`/`emptyTextQueuedIndices`.)

Right after the existing `allFixedIndices` declaration (lines 881-887), add a separate list that also includes this run's newly-queued rows, used only for pruning the persistent "download failed" store (not for removing rows from the table):
```js
		const allFixedIndices = [
			...downloadResults.filter(r => r.downloaded).map(r => r.index),
			...phase2FixedIndices,
			...timeoutFixedIndices,
			...emptyTextFixedIndices,
			...serverFailedFixedIndices,
		];
		const allResolvedIndices = [
			...allFixedIndices,
			...timeoutQueuedIndices, ...emptyTextQueuedIndices, ...serverFailedQueuedIndices, ...phase2QueuedIndices,
		];
		const fixedDownloadFailedKeys = allResolvedIndices
			.filter(i => this.items[i].serverDownloadFailed)
			.map(i => this.items[i].attachmentItem.key);
```
(`fixedDownloadFailedKeys` previously derived from `allFixedIndices` — it now derives from `allResolvedIndices` instead; `allFixedIndices` itself is unchanged and is still what the later "drop rows from the table" block at lines 912-934 uses, so queued rows correctly stay visible.)

- [ ] **Step 9: Handle `alreadyQueuedIndices` — process-now when forced, no-op otherwise**

Insert this block right before the final counters/summary section (before line 936's `const parts = [];`):
```js
		if (forceIndexNow && alreadyQueuedIndices.length > 0) {
			for (const i of alreadyQueuedIndices) {
				const info = this.items[i];
				this.setRowStatus(i, 'searching', 'Indexing now...');
				const step = itemHandles.get(i)?.addStep('process_now');
				try {
					const result = await this.plugin.processQueuedAttachmentNow(
						info.attachmentItem, info.parentItem, this.libraryID,
						collectDebug ? { includeDiagnostics: true } : {},
					);
					step?.finish(result.fixed ? 'fixed' : 'error', { ...(result.error ? { error: result.error } : {}) });
					if (result.fixed) {
						this.setRowStatus(i, 'fixed', 'Indexed');
						fixed++;
						allFixedIndices.push(i);
					} else {
						this.setRowStatus(i, 'error', `Indexing failed: ${result.error}`, result.error);
						errors++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(i, 'error', `Error: ${msg}`, msg);
					step?.finish('error', { error: msg });
					errors++;
					console.error(`fix-unavailable: process-now error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}
		// When !forceIndexNow, alreadyQueuedIndices are deliberately left untouched —
		// nothing to repair, they're already cached server-side awaiting the next run.
```
`allFixedIndices` must be declared with `let`/reassignable or already be a plain array pushed into elsewhere — since it's declared via `const allFixedIndices = [...]` earlier in Step 8 and arrays are mutable even when `const`, `allFixedIndices.push(i)` here is valid without changing that declaration.

Note this block runs after `allFixedIndices`/`fixedDownloadFailedKeys`/the pruning call (Step 8) and after the "drop fixed rows from the table" block (lines 912-934) in the *original* file's order — but it must run **before** that table-dropping block, since it adds to `allFixedIndices` which that block reads. Place it immediately after `fixedDownloadFailedKeys`'s pruning `if (fixedDownloadFailedKeys.length > 0 ...) { ... }` block (original lines 891-897) and before the "Record each selected row's final status" comment (original line 899), not at the very end of the function.

- [ ] **Step 10: Write/update dialog-level tests**

In `plugin/test/fix-unavailable.test.js`, add:

```js
test('searchAndFix with default options defers upload (defer:true) for a plain imported row', async () => {
  const dialog = loadDialog();
  dialog.deferCapable = true;
  dialog.items = [{ attachmentItem: { key: 'A1' }, parentItem: { key: 'I1' }, isLinked: false }];
  dialog.selected = new Set([0]);
  dialog.rowStatus = new Map();
  dialog.itemHandles = new Map();
  let capturedOpts;
  dialog.plugin = {
    _tryDownloadAttachment: async () => ({ downloaded: true }),
  };
  dialog.getSelectedIndices = () => [0];
  dialog._shouldCollectDebug = () => false;
  await dialog.searchAndFix({ forceIndexNow: false });
  assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'fixed'); // plain sync-download path unchanged
});

test('searchAndFix with forceIndexNow:true threads defer:false into retryDownloadFailedAttachment', async () => {
  const dialog = loadDialog();
  dialog.deferCapable = true;
  dialog.items = [{ attachmentItem: { key: 'A1' }, parentItem: { key: 'I1' }, isLinked: false, serverDownloadFailed: true }];
  dialog.selected = new Set([0]);
  dialog.rowStatus = new Map();
  let capturedOpts;
  dialog.plugin = {
    retryDownloadFailedAttachment: async (_att, _parent, _lib, opts) => { capturedOpts = opts; return { fixed: true, stillMissing: false }; },
    removeDownloadFailedItems: async () => {},
  };
  dialog.getSelectedIndices = () => [0];
  dialog._shouldCollectDebug = () => false;
  await dialog.searchAndFix({ forceIndexNow: true });
  assert.strictEqual(capturedOpts.defer, false);
});

test('searchAndFix sets status-queued and does not call any upload for an already-queued row by default', async () => {
  const dialog = loadDialog();
  dialog.deferCapable = true;
  dialog.items = [{ attachmentItem: { key: 'A1' }, parentItem: { key: 'I1' }, isLinked: false }];
  dialog.selected = new Set([0]);
  dialog.rowStatus = new Map([[0, { cssClass: 'queued', text: 'Waiting to be indexed' }]]);
  let calledProcessNow = false;
  dialog.plugin = { processQueuedAttachmentNow: async () => { calledProcessNow = true; return { fixed: true }; } };
  dialog.getSelectedIndices = () => [0];
  dialog._shouldCollectDebug = () => false;
  await dialog.searchAndFix({ forceIndexNow: false });
  assert.strictEqual(calledProcessNow, false);
  assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'queued'); // untouched
});

test('searchAndFix with forceIndexNow:true calls processQueuedAttachmentNow for an already-queued row', async () => {
  const dialog = loadDialog();
  dialog.deferCapable = true;
  dialog.items = [{ attachmentItem: { key: 'A1' }, parentItem: { key: 'I1' }, isLinked: false }];
  dialog.selected = new Set([0]);
  dialog.rowStatus = new Map([[0, { cssClass: 'queued', text: 'Waiting to be indexed' }]]);
  dialog.plugin = { processQueuedAttachmentNow: async () => ({ fixed: true }) };
  dialog.getSelectedIndices = () => [0];
  dialog._shouldCollectDebug = () => false;
  await dialog.searchAndFix({ forceIndexNow: true });
  assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'fixed');
});
```

Adapt these to whatever minimal extra stub fields `loadDialog()`'s DOM/elements map needs for `searchAndFix` to run without throwing (check the existing passing tests in this file that already call `dialog.searchAndFix()` for the exact set of document-element stubs / `dialog.plugin` methods they provide, e.g. `removeSkippedServerItems`, and only add what these new tests specifically exercise).

- [ ] **Step 11: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS — every test file, including all new and pre-existing tests (155+ from before this feature, plus the new ones across Tasks 8-13).

- [ ] **Step 12: Commit**

```bash
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat(plugin): wire split-button and queued-row handling into Fix Unavailable dialog"
```

---

### Task 14: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `uv run pytest`
Expected: PASS (plus any pre-existing unrelated skips, e.g. `sentence_transformers` not installed).

- [ ] **Step 2: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS, all files.

- [ ] **Step 3: If both pass, no further action — the feature is complete per the spec.** Manual/live verification in a running Zotero instance (via the project's dev server reload flow, or a build+install) is a separate step the user performs afterward, not part of this plan.
