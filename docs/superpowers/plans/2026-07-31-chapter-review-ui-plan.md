# Chapter Review UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent review/commit queue populated automatically by the chapter-segmentation pipeline's dry-run paths, plus admin-only API endpoints and a Jinja2 web page so a human can approve/edit/reject uncertain items (low-confidence chapters, ambiguous retrofit matches, needs_ocr attachments) and inspect/execute/defer confident items before they're written to Zotero.

**Architecture:** A new `backend/services/review_queue_store.py` module owns a single JSON file (`data/system/review_queue.json`, keyed by library slug then queue_id) with plain functions (`upsert_many`/`list_pending`/`get_entry`/`set_status`/`set_bucket`/`remove_entry`) — no class, no encryption, `FileLock`-guarded whole-file read/modify/write, mirroring `AutoIndexKeyStore`'s pattern. Three existing service `run()` functions (`chapter_segmentation.py`, `chapter_upload.py`, `chapter_retrofit.py`) each get one added upsert call in their dry-run path. Five new endpoints on the existing `backend/api/chapter_linking.py` router (gated by the existing `require_authorized_group_admin` dependency) drive the queue; a new `backend/api/admin_pages.py` router serves the page itself (unauthenticated shell — the real gate is the API calls the page's JS makes). "Approve" for a chapter/match entry works by calling the existing batch `upload_run()`/`commit_links()` with a synthetic single-item input (zero refactor of the existing per-item logic, which is otherwise fully inlined in a loop) rather than extracting new functions.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, `pyzotero`, `uv run pytest` (unittest-style), Jinja2, vanilla JS (no framework, no build step — matches the existing `backend/templates/` convention).

**Spec:** `docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md`

---

## File Structure

- Create: `backend/services/review_queue_store.py` — the queue's storage module.
- Test: `backend/tests/test_review_queue_store.py`
- Modify: `backend/config/settings.py` — add `review_queue_path`.
- Modify: `backend/services/chapter_segmentation.py` — upsert `ocr` review entries at the end of `run()`.
- Modify: `backend/services/chapter_upload.py` — upsert `chapter` review/commit entries at the end of `run()`.
- Modify: `backend/services/chapter_retrofit.py` — upsert `match` review/commit entries in `run()`'s dry-run branch.
- Modify: `backend/api/chapter_linking.py` — add `GET .../review/pending`, `POST .../review/{queue_id}/approve`, `POST .../review/{queue_id}/reject`, `POST .../review/execute`, `POST .../review/{queue_id}/send-to-review`.
- Create: `backend/api/admin_pages.py` — serves `GET /admin/review`.
- Create: `backend/templates/admin_review.html`
- Modify: `backend/main.py` — register `admin_pages.router`.
- Modify: `docs/chapter-segmentation.md` — document the review UI.
- Test: `backend/tests/test_chapter_segmentation.py`, `backend/tests/test_chapter_upload.py`, `backend/tests/test_chapter_retrofit.py`, `backend/tests/test_chapter_linking_api.py`, `backend/tests/test_admin_pages.py` (new).

---

### Task 1: Settings — add `review_queue_path`

**Files:**
- Modify: `backend/config/settings.py`

- [ ] **Step 1: Add the field**

In `backend/config/settings.py`, right after the existing `autoindex_keys_path` field (currently at line 147-151):

```python
    autoindex_keys_path: Optional[Path] = Field(
        default=None,
        description="Path to the encrypted auto-index keys JSON. Defaults to "
                    "<data_path>/system/autoindex_keys.json.",
    )
    review_queue_path: Optional[Path] = Field(
        default=None,
        description="Path to the chapter-review queue JSON. Defaults to "
                    "<data_path>/system/review_queue.json.",
    )
```

- [ ] **Step 2: Add it to the path-expansion validator and derived-path fallback**

Change (line 219):

```python
    @field_validator("data_path", "model_weights_path", "vector_db_path", "log_file", "registrations_path", "autoindex_keys_path", mode="before")
```

to:

```python
    @field_validator("data_path", "model_weights_path", "vector_db_path", "log_file", "registrations_path", "autoindex_keys_path", "review_queue_path", mode="before")
```

In `set_derived_paths` (around line 241), right after the `autoindex_keys_path` fallback:

```python
        if self.autoindex_keys_path is None:
            self.autoindex_keys_path = self.data_path / "system" / "autoindex_keys.json"
        if self.review_queue_path is None:
            self.review_queue_path = self.data_path / "system" / "review_queue.json"
        return self
```

- [ ] **Step 3: Add it to `ensure_directories`**

In `ensure_directories` (around line 268), right after the `autoindex_keys_path` block:

```python
        if self.autoindex_keys_path:
            self.autoindex_keys_path.parent.mkdir(parents=True, exist_ok=True)
        if self.review_queue_path:
            self.review_queue_path.parent.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Verify by import**

Run: `uv run python -c "from backend.config.settings import get_settings; print(get_settings().review_queue_path)"`
Expected: prints a path ending in `data/system/review_queue.json`, no error.

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py
git commit -m "feat: add review_queue_path setting"
```

---

### Task 2: `review_queue_store.py` — core module

**Files:**
- Create: `backend/services/review_queue_store.py`
- Test: `backend/tests/test_review_queue_store.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_review_queue_store.py`:

```python
"""Unit tests for backend.services.review_queue_store."""

import tempfile
import unittest
from pathlib import Path

from backend.services.review_queue_store import (
    get_entry,
    list_pending,
    remove_entry,
    set_bucket,
    set_status,
    upsert_many,
)


class ReviewQueueStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()


class TestUpsertMany(ReviewQueueStoreTestCase):
    def test_new_entries_become_pending(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {"book_key": "BOOK1"}},
        ])
        entry = get_entry(self.path, "groups/1", "ocr:ATT1")
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["payload"], {"book_key": "BOOK1"})
        self.assertIn("created_at", entry)
        self.assertIn("updated_at", entry)

    def test_upsert_existing_pending_refreshes_payload(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "chapter:BOOK1:1-2", "type": "chapter", "bucket": "review", "payload": {"confidence": 0.5}},
        ])
        upsert_many(self.path, "groups/1", [
            {"queue_id": "chapter:BOOK1:1-2", "type": "chapter", "bucket": "review", "payload": {"confidence": 0.6}},
        ])
        entry = get_entry(self.path, "groups/1", "chapter:BOOK1:1-2")
        self.assertEqual(entry["payload"]["confidence"], 0.6)

    def test_upsert_existing_approved_left_untouched(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit", "payload": {"book_key": "OLD"}},
        ])
        set_status(self.path, "groups/1", "match:CHAP1", "approved")
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit", "payload": {"book_key": "NEW"}},
        ])
        entry = get_entry(self.path, "groups/1", "match:CHAP1")
        self.assertEqual(entry["status"], "approved")
        self.assertEqual(entry["payload"]["book_key"], "OLD")

    def test_upsert_existing_rejected_left_untouched(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP2", "type": "match", "bucket": "review", "payload": {}},
        ])
        set_status(self.path, "groups/1", "match:CHAP2", "rejected")
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP2", "type": "match", "bucket": "review", "payload": {"changed": True}},
        ])
        entry = get_entry(self.path, "groups/1", "match:CHAP2")
        self.assertEqual(entry["status"], "rejected")
        self.assertNotIn("changed", entry["payload"])

    def test_empty_list_is_a_noop(self):
        upsert_many(self.path, "groups/1", [])
        self.assertFalse(self.path.exists())

    def test_per_library_isolation(self):
        upsert_many(self.path, "groups/1", [{"queue_id": "ocr:A", "type": "ocr", "bucket": "review", "payload": {}}])
        upsert_many(self.path, "groups/2", [{"queue_id": "ocr:A", "type": "ocr", "bucket": "review", "payload": {}}])
        set_status(self.path, "groups/1", "ocr:A", "approved")
        self.assertEqual(get_entry(self.path, "groups/1", "ocr:A")["status"], "approved")
        self.assertEqual(get_entry(self.path, "groups/2", "ocr:A")["status"], "pending")


class TestListPending(ReviewQueueStoreTestCase):
    def setUp(self):
        super().setUp()
        upsert_many(self.path, "groups/1", [
            {"queue_id": "ocr:A", "type": "ocr", "bucket": "review", "payload": {}},
            {"queue_id": "chapter:B:1-2", "type": "chapter", "bucket": "commit", "payload": {}},
            {"queue_id": "match:C", "type": "match", "bucket": "review", "payload": {}},
        ])
        set_status(self.path, "groups/1", "match:C", "approved")

    def test_excludes_non_pending(self):
        results = list_pending(self.path, "groups/1")
        self.assertEqual({e["queue_id"] for e in results}, {"ocr:A", "chapter:B:1-2"})

    def test_filters_by_bucket(self):
        results = list_pending(self.path, "groups/1", bucket="commit")
        self.assertEqual({e["queue_id"] for e in results}, {"chapter:B:1-2"})

    def test_filters_by_entry_type(self):
        results = list_pending(self.path, "groups/1", entry_type="ocr")
        self.assertEqual({e["queue_id"] for e in results}, {"ocr:A"})

    def test_unknown_library_returns_empty(self):
        self.assertEqual(list_pending(self.path, "groups/999"), [])


class TestSetStatusAndBucket(ReviewQueueStoreTestCase):
    def test_set_status_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            set_status(self.path, "groups/1", "does-not-exist", "approved")

    def test_set_bucket_flips_bucket_keeps_status(self):
        upsert_many(self.path, "groups/1", [{"queue_id": "match:D", "type": "match", "bucket": "commit", "payload": {}}])
        set_bucket(self.path, "groups/1", "match:D", "review")
        entry = get_entry(self.path, "groups/1", "match:D")
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["status"], "pending")


class TestGetEntryAndRemove(ReviewQueueStoreTestCase):
    def test_get_entry_returns_none_if_missing(self):
        self.assertIsNone(get_entry(self.path, "groups/1", "nope"))

    def test_remove_entry_deletes(self):
        upsert_many(self.path, "groups/1", [{"queue_id": "ocr:E", "type": "ocr", "bucket": "review", "payload": {}}])
        remove_entry(self.path, "groups/1", "ocr:E")
        self.assertIsNone(get_entry(self.path, "groups/1", "ocr:E"))

    def test_remove_entry_missing_is_a_noop(self):
        remove_entry(self.path, "groups/1", "does-not-exist")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_review_queue_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.review_queue_store'`.

- [ ] **Step 3: Implement the module**

Create `backend/services/review_queue_store.py`:

```python
"""Persist the chapter-review queue: items awaiting an admin decision
(bucket="review") or awaiting a batch commit (bucket="commit"), produced
by chapter_segmentation.run()/chapter_upload.run()/chapter_retrofit.run()'s
dry-run paths. See design spec
docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md section 3.

One JSON file, keyed first by library slug then by a deterministic
queue_id, following the same whole-file read/modify/write + FileLock
pattern as AutoIndexKeyStore -- unencrypted, since nothing stored here is
a credential.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

_VALID_STATUSES = {"pending", "approved", "rejected"}
_VALID_BUCKETS = {"review", "commit"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_many(path: Path, slug: str, entries: list[dict]) -> None:
    """Upsert `entries` (each `{"queue_id", "type", "bucket", "payload"}`)
    into the queue for `slug`. An entry whose queue_id already exists with
    a non-"pending" status (already approved/rejected) is left completely
    untouched -- a re-run must never resurrect a decision the admin
    already made. A new or still-pending entry is written/refreshed.
    """
    if not entries:
        return
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        library = data.setdefault(slug, {})
        now = _now()
        for entry in entries:
            queue_id = entry["queue_id"]
            existing = library.get(queue_id)
            if existing is not None and existing["status"] != "pending":
                continue
            library[queue_id] = {
                "type": entry["type"],
                "bucket": entry["bucket"],
                "status": "pending",
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                "payload": entry["payload"],
            }
        _save(path, data)


def list_pending(path: Path, slug: str, bucket: str | None = None, entry_type: str | None = None) -> list[dict]:
    """Return pending entries for `slug`, each merged with its `queue_id`,
    optionally filtered by `bucket` and/or `entry_type`.
    """
    data = _load(path)
    library = data.get(slug, {})
    results = []
    for queue_id, entry in library.items():
        if entry["status"] != "pending":
            continue
        if bucket is not None and entry["bucket"] != bucket:
            continue
        if entry_type is not None and entry["type"] != entry_type:
            continue
        results.append({**entry, "queue_id": queue_id})
    return results


def get_entry(path: Path, slug: str, queue_id: str) -> dict | None:
    """Return one entry (merged with its `queue_id`), or None if missing."""
    data = _load(path)
    entry = data.get(slug, {}).get(queue_id)
    if entry is None:
        return None
    return {**entry, "queue_id": queue_id}


def set_status(path: Path, slug: str, queue_id: str, status: str) -> None:
    """Set an entry's status ("approved" | "rejected"). Raises KeyError if
    the library/queue_id does not exist."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        entry = data[slug][queue_id]
        entry["status"] = status
        entry["updated_at"] = _now()
        _save(path, data)


def set_bucket(path: Path, slug: str, queue_id: str, bucket: str) -> None:
    """Flip an entry's bucket ("review" | "commit"), keeping its status.
    Raises KeyError if the library/queue_id does not exist."""
    if bucket not in _VALID_BUCKETS:
        raise ValueError(f"invalid bucket: {bucket!r}")
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        entry = data[slug][queue_id]
        entry["bucket"] = bucket
        entry["updated_at"] = _now()
        _save(path, data)


def remove_entry(path: Path, slug: str, queue_id: str) -> None:
    """Delete an entry outright. A no-op if it doesn't exist -- used by the
    OCR-approve flow to clear a stale entry before a fresh analyze run
    re-upserts whatever the current state actually is."""
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        data.get(slug, {}).pop(queue_id, None)
        _save(path, data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_review_queue_store.py -v`
Expected: PASS — all 15 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/services/review_queue_store.py backend/tests/test_review_queue_store.py
git commit -m "feat: add review_queue_store for the chapter review/commit queue"
```

---

### Task 3: Wire `chapter_segmentation.py::run()` — `needs_ocr` review entries

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Test: `backend/tests/test_chapter_segmentation.py`

Analyze has no confidence-threshold concept of its own (see the spec's §5 correction) — its only contribution to the queue is `needs_ocr` attachments.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_chapter_segmentation.py`, add these imports at the top:

```python
import tempfile
from pathlib import Path as _TestPath

from backend.config.settings import get_settings, reset_settings
from backend.services.review_queue_store import get_entry
```

Add a `setUp`/`tearDown` to the existing `class TestRun(unittest.TestCase):` (so every test in it gets an isolated `review_queue_path`, since `run()` will now write to it):

```python
class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_skips_already_linked_book(self):
        ...  # existing tests below are unchanged
```

Add a new test method to `TestRun` (after any existing test — e.g. after `test_processes_unlinked_book`):

```python
    def test_needs_ocr_attachment_is_upserted_into_review_queue(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK9", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT9", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 no text layer"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[""],
        ):
            asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))

        entry = get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT9")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "ocr")
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["payload"], {"book_key": "BOOK9", "attachment_key": "ATT9"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestRun::test_needs_ocr_attachment_is_upserted_into_review_queue -v`
Expected: FAIL — `entry` is `None` (nothing upserts yet).

- [ ] **Step 3: Wire the upsert call**

In `backend/services/chapter_segmentation.py`, add to the imports (after the existing `from backend.services.chapter_link_store import parse_links` line):

```python
from backend.config.settings import get_settings
from backend.services import review_queue_store
from backend.services.chapter_link_store import parse_links
```

Change the end of `run()` from:

```python
    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
```

to:

```python
    ocr_entries = [
        {
            "queue_id": f"ocr:{a['attachment_key']}",
            "type": "ocr",
            "bucket": "review",
            "payload": {"book_key": a["item_key"], "attachment_key": a["attachment_key"]},
        }
        for a in attachments_out
        if a.get("needs_ocr")
    ]
    if ocr_entries:
        review_queue_store.upsert_many(get_settings().review_queue_path, slug, ocr_entries)

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS — all tests, including the new one. Every pre-existing `TestRun` test must still pass (they don't touch `review_queue_path` behavior, and `setUp`/`tearDown` just isolate it).

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: upsert needs_ocr attachments into the review queue"
```

---

### Task 4: Wire `chapter_upload.py::run()` — chapter review/commit entries

**Files:**
- Modify: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_upload.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_upload.py`, add to the top imports:

```python
import tempfile
from pathlib import Path as _TestPath

from backend.config.settings import get_settings, reset_settings
from backend.services.review_queue_store import get_entry
```

Add `setUp`/`tearDown` to `class TestUploadRun(unittest.TestCase):` (it currently has none — insert right after the class line, before `_book_and_analysis`):

```python
class TestUploadRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def _book_and_analysis(self):
        ...  # unchanged
```

Add two new test methods to `TestUploadRun` (after `test_commit_downloads_book_pdf_once_for_multiple_chapters`):

```python
    def test_dry_run_upserts_confident_chapter_as_commit_bucket(self):
        book_item, analysis = self._book_and_analysis()
        zot = MagicMock()
        zot.item.return_value = book_item

        asyncio.run(upload_run(
            zotero_write_client=zot, zotero_read_client=MagicMock(get_attachment_file=MagicMock()),
            slug="groups/1", analyses=[analysis], commit=False, confidence_threshold=0.8,
            target_collection="Book Chapters", max_items=None,
        ))

        entry = get_entry(get_settings().review_queue_path, "groups/1", "chapter:BOOK1:2-4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "chapter")
        self.assertEqual(entry["bucket"], "commit")
        self.assertEqual(entry["payload"]["book_key"], "BOOK1")
        self.assertEqual(entry["payload"]["attachment_key"], "ATT1")
        self.assertEqual(entry["payload"]["title"], "Comparing Citation Styles")
        self.assertEqual(entry["payload"]["target_collection"], "Book Chapters")

    def test_low_confidence_chapter_upserted_as_review_bucket_in_both_dry_run_and_commit(self):
        book_item, analysis = self._book_and_analysis()
        analysis["chapters"][0]["confidence"] = 0.5  # below the 0.8 threshold used below
        zot = MagicMock()
        zot.item.return_value = book_item

        asyncio.run(upload_run(
            zotero_write_client=zot, zotero_read_client=MagicMock(get_attachment_file=MagicMock()),
            slug="groups/1", analyses=[analysis], commit=False, confidence_threshold=0.8,
            target_collection="Book Chapters", max_items=None,
        ))

        entry = get_entry(get_settings().review_queue_path, "groups/1", "chapter:BOOK1:2-4")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["payload"]["confidence"], 0.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_upload.py -k "commit_bucket or review_bucket" -v`
Expected: FAIL — both `entry` values are `None` (nothing upserts yet).

- [ ] **Step 3: Wire the upsert calls**

In `backend/services/chapter_upload.py`, add to the imports:

```python
from backend.config.settings import get_settings
from backend.services import review_queue_store
from backend.services.chapter_link_store import (
```

Add two accumulator lists alongside the existing ones near the top of `run()`:

```python
    would_create: list[dict] = []
    created: list[dict] = []
    skipped_low_confidence: list[dict] = []
    failed: list[dict] = []
    review_entries: list[dict] = []
    commit_entries: list[dict] = []
```

Replace:

```python
        confident_chapters = [c for c in analysis.get("chapters", []) if c["confidence"] >= confidence_threshold]
        low_confidence = [c for c in analysis.get("chapters", []) if c["confidence"] < confidence_threshold]
        skipped_low_confidence.extend({"book_key": book_key, "title": c["title"]} for c in low_confidence)

        if not commit:
            would_create.extend({"book_key": book_key, "title": c["title"], "pdf_start_index": c["pdf_start_index"],
                                  "pdf_end_index": c["pdf_end_index"]} for c in confident_chapters)
            continue
```

with:

```python
        confident_chapters = [c for c in analysis.get("chapters", []) if c["confidence"] >= confidence_threshold]
        low_confidence = [c for c in analysis.get("chapters", []) if c["confidence"] < confidence_threshold]
        skipped_low_confidence.extend({"book_key": book_key, "title": c["title"]} for c in low_confidence)

        def _chapter_payload(c: dict) -> dict:
            return {
                "book_key": book_key, "attachment_key": attachment_key,
                "title": c["title"], "authors": c.get("authors", []),
                "pdf_start_index": c["pdf_start_index"], "pdf_end_index": c["pdf_end_index"],
                "citation_pages": c.get("citation_pages"), "confidence": c["confidence"],
                "target_collection": target_collection,
            }

        review_entries.extend(
            {"queue_id": f"chapter:{book_key}:{c['pdf_start_index']}-{c['pdf_end_index']}",
             "type": "chapter", "bucket": "review", "payload": _chapter_payload(c)}
            for c in low_confidence
        )

        if not commit:
            commit_entries.extend(
                {"queue_id": f"chapter:{book_key}:{c['pdf_start_index']}-{c['pdf_end_index']}",
                 "type": "chapter", "bucket": "commit", "payload": _chapter_payload(c)}
                for c in confident_chapters
            )
            would_create.extend({"book_key": book_key, "title": c["title"], "pdf_start_index": c["pdf_start_index"],
                                  "pdf_end_index": c["pdf_end_index"]} for c in confident_chapters)
            continue
```

Change the function's final return from:

```python
    return {
        "would_create": would_create,
        "created": created,
        "skipped_low_confidence": skipped_low_confidence,
        "failed": failed,
    }
```

to:

```python
    if review_entries or commit_entries:
        review_queue_store.upsert_many(get_settings().review_queue_path, slug, review_entries + commit_entries)

    return {
        "would_create": would_create,
        "created": created,
        "skipped_low_confidence": skipped_low_confidence,
        "failed": failed,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_upload.py -v`
Expected: PASS — all tests, including the 2 new ones. Every pre-existing `TestUploadRun` test must still pass unmodified.

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "feat: upsert confident/low-confidence chapters into the review/commit queue"
```

---

### Task 5: Wire `chapter_retrofit.py::run()` — match review/commit entries

**Files:**
- Modify: `backend/services/chapter_retrofit.py`
- Test: `backend/tests/test_chapter_retrofit.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_retrofit.py`, add to the top imports:

```python
import tempfile
from pathlib import Path as _TestPath

from backend.config.settings import get_settings, reset_settings
from backend.services.review_queue_store import get_entry
```

Add `setUp`/`tearDown` to `class TestRetrofitRun(unittest.TestCase):` (it currently has none):

```python
class TestRetrofitRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_links_confident_match_and_skips_ambiguous(self):
        ...  # unchanged
```

Add two new test methods to `TestRetrofitRun` (after any existing test in the class, e.g. at the end before the class ends):

```python
    def test_dry_run_upserts_would_link_as_commit_bucket(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items

        retrofit_run(zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None, commit=False)

        entry = get_entry(get_settings().review_queue_path, "groups/1", "match:CHAP1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["bucket"], "commit")
        self.assertEqual(entry["payload"]["book_key"], "BOOK1")

    def test_dry_run_upserts_ambiguous_as_review_bucket(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Same Title", "date": "2020", "extra": ""}},
            {"key": "BOOK2", "data": {"key": "BOOK2", "itemType": "book", "title": "Same Title Vol 2", "date": "2020", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Same Title", "date": "2020", "extra": ""}},
        ]
        zot.everything.return_value = all_items

        retrofit_run(zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None, commit=False)

        entry = get_entry(get_settings().review_queue_path, "groups/1", "match:CHAP1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(len(entry["payload"]["candidates"]), 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -k "commit_bucket or review_bucket" -v`
Expected: FAIL — both `entry` values are `None`.

- [ ] **Step 3: Wire the upsert calls**

In `backend/services/chapter_retrofit.py`, add to the imports:

```python
from backend.config.settings import get_settings
from backend.services import review_queue_store
from backend.services.chapter_link_store import (
```

Change `run()`'s dry-run branch from:

```python
    if not commit:
        return {
            "linked": [], "would_link": matches["would_link"],
            "ambiguous": matches["ambiguous"], "no_match": matches["no_match"], "failed": [],
        }
```

to:

```python
    if not commit:
        review_entries = [
            {"queue_id": f"match:{a['chapter_key']}", "type": "match", "bucket": "review",
             "payload": {"chapter_key": a["chapter_key"], "candidates": a["candidates"], "target_collection": target_collection}}
            for a in matches["ambiguous"]
        ]
        commit_entries = [
            {"queue_id": f"match:{m['chapter_key']}", "type": "match", "bucket": "commit",
             "payload": {"chapter_key": m["chapter_key"], "book_key": m["book_key"], "score": m["score"],
                         "target_collection": target_collection}}
            for m in matches["would_link"]
        ]
        if review_entries or commit_entries:
            review_queue_store.upsert_many(get_settings().review_queue_path, slug, review_entries + commit_entries)
        return {
            "linked": [], "would_link": matches["would_link"],
            "ambiguous": matches["ambiguous"], "no_match": matches["no_match"], "failed": [],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v`
Expected: PASS — all tests, including the 2 new ones.

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: upsert would_link/ambiguous matches into the review/commit queue"
```

---

### Task 6: API — `GET .../review/pending`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_linking_api.py`, add to the top imports:

```python
import tempfile
from pathlib import Path

from backend.config.settings import get_settings, reset_settings
from backend.dependencies import require_authorized_group_admin
from backend.services import review_queue_store
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
from backend.zotero.group_roles import reset_admin_role_cache
from backend.zotero.key_validator import KeyValidation
```

(`unittest`, `AsyncMock`/`patch`, `TestClient`, `chapter_linking`, and `app` are already imported at the top of this file — leave those as-is.)

Add a new test class at the end of the file, before the `if __name__ == "__main__":` block:

```python
class TestReviewEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.review_queue_path = Path(self.tmp.name) / "review_queue.json"
        s.authorized_group_id = 999
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _override_admin(self):
        app.dependency_overrides[require_authorized_group_admin] = lambda: ZoteroIdentity(
            user_id=1, username="admin", targets=["groups/1"]
        )

    def test_lists_pending_entries_for_library(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {"book_key": "BOOK1", "attachment_key": "ATT1"}},
        ])
        response = self.client.get("/api/chapter-linking/review/pending", params={"library_slug": "groups/1"})
        self.assertEqual(response.status_code, 200)
        entries = response.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["queue_id"], "ocr:ATT1")

    def test_filters_by_bucket(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {}},
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit", "payload": {}},
        ])
        response = self.client.get(
            "/api/chapter-linking/review/pending", params={"library_slug": "groups/1", "bucket": "commit"}
        )
        entries = response.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["queue_id"], "match:CHAP1")

    def test_rejects_non_admin(self):
        get_settings().api_host = "rag.example.com"
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            response = self.client.get(
                "/api/chapter-linking/review/pending",
                params={"library_slug": "groups/1"},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(response.status_code, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py::TestReviewEndpoints -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Add the endpoint**

In `backend/api/chapter_linking.py`, change the imports from:

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pyzotero import zotero

from backend.dependencies import make_llm_service
from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_ocr import run as ocr_run
from backend.services.chapter_retrofit import run as retrofit_run
from backend.services.chapter_segmentation import run as analyze_run
from backend.services.chapter_upload import run as upload_run
from backend.services.extraction import create_document_extractor
from backend.services.job_tracker import JobTracker
from backend.zotero.web_api import ZoteroWebAPI
```

to:

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pyzotero import zotero

from backend.config.settings import get_settings
from backend.dependencies import make_llm_service, require_authorized_group_admin
from backend.services import review_queue_store
from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_ocr import run as ocr_run
from backend.services.chapter_retrofit import commit_links, run as retrofit_run
from backend.services.chapter_segmentation import run as analyze_run
from backend.services.chapter_upload import run as upload_run
from backend.services.extraction import create_document_extractor
from backend.services.job_tracker import JobTracker
from backend.services.zotero_identity import ZoteroIdentity
from backend.zotero.web_api import ZoteroWebAPI
```

At the end of the file, after `start_segment_upload`, add:

```python
@router.get(
    "/chapter-linking/review/pending",
    summary="List pending chapter-review queue entries for a library (admin only)",
)
async def list_pending_review(
    library_slug: str,
    bucket: str | None = None,
    identity: ZoteroIdentity | None = Depends(require_authorized_group_admin),
) -> dict:
    entries = review_queue_store.list_pending(get_settings().review_queue_path, library_slug, bucket=bucket)
    return {"entries": entries}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 3 new ones. Pre-existing `TestJobPolling` tests must still pass unmodified.

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add GET /api/chapter-linking/review/pending"
```

---

### Task 7: API — shared dispatch helper + `POST .../review/{queue_id}/approve`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestReviewEndpoints` in `backend/tests/test_chapter_linking_api.py` (after `test_rejects_non_admin`):

```python
    def test_approve_chapter_entry_calls_upload_run_and_marks_approved(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "chapter:BOOK1:2-4", "type": "chapter", "bucket": "review",
             "payload": {"book_key": "BOOK1", "attachment_key": "ATT1", "title": "T", "authors": [],
                         "pdf_start_index": 2, "pdf_end_index": 4, "citation_pages": None,
                         "confidence": 0.7, "target_collection": "Book Chapters"}},
        ])
        with patch("backend.api.chapter_linking.upload_run", new=AsyncMock(
            return_value={"created": [{"book_key": "BOOK1", "chapter_key": "CHAP1"}], "failed": []}
        )) as mock_upload:
            response = self.client.post(
                "/api/chapter-linking/review/chapter:BOOK1:2-4/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        self.assertEqual(call_kwargs["confidence_threshold"], 0.0)
        self.assertEqual(call_kwargs["analyses"][0]["chapters"][0]["title"], "T")
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "chapter:BOOK1:2-4")
        self.assertEqual(entry["status"], "approved")

    def test_approve_chapter_entry_applies_edits(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "chapter:BOOK1:2-4", "type": "chapter", "bucket": "review",
             "payload": {"book_key": "BOOK1", "attachment_key": "ATT1", "title": "Original", "authors": [],
                         "pdf_start_index": 2, "pdf_end_index": 4, "citation_pages": None,
                         "confidence": 0.7, "target_collection": "Book Chapters"}},
        ])
        with patch("backend.api.chapter_linking.upload_run", new=AsyncMock(
            return_value={"created": [], "failed": []}
        )) as mock_upload:
            self.client.post(
                "/api/chapter-linking/review/chapter:BOOK1:2-4/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "title": "Edited Title", "pdf_end_index": 5},
            )
        chapter = mock_upload.call_args.kwargs["analyses"][0]["chapters"][0]
        self.assertEqual(chapter["title"], "Edited Title")
        self.assertEqual(chapter["pdf_start_index"], 2)
        self.assertEqual(chapter["pdf_end_index"], 5)

    def test_approve_match_entry_requires_book_key_for_ambiguous(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "review",
             "payload": {"chapter_key": "CHAP1", "candidates": [{"key": "BOOK1", "title": "T", "year": 2020}]}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/match:CHAP1/approve",
            json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
        )
        self.assertEqual(response.status_code, 502)

    def test_approve_match_entry_with_book_key_calls_commit_links(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "review",
             "payload": {"chapter_key": "CHAP1", "candidates": [{"key": "BOOK1", "title": "T", "year": 2020}]}},
        ])
        with patch("backend.api.chapter_linking.commit_links", return_value={"linked": [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}], "failed": []}) as mock_commit:
            response = self.client.post(
                "/api/chapter-linking/review/match:CHAP1/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "book_key": "BOOK1"},
            )
        self.assertEqual(response.status_code, 200)
        mock_commit.assert_called_once()
        would_link = mock_commit.call_args.args[2]
        self.assertEqual(would_link, [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

    def test_approve_ocr_entry_runs_ocr_and_reanalyzes_then_removes_entry(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review",
             "payload": {"book_key": "BOOK1", "attachment_key": "ATT1"}},
        ])
        with patch("backend.api.chapter_linking.ocr_run", new=AsyncMock(return_value={"results": []})), \
             patch("backend.api.chapter_linking.analyze_run", new=AsyncMock(return_value={"slug": "groups/1", "attachments": []})):
            response = self.client.post(
                "/api/chapter-linking/review/ocr:ATT1/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT1"))

    def test_approve_unknown_queue_id_returns_404(self):
        self._override_admin()
        response = self.client.post(
            "/api/chapter-linking/review/does-not-exist/approve",
            json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
        )
        self.assertEqual(response.status_code, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -k approve -v`
Expected: FAIL — `404 Not Found` for all (route doesn't exist yet).

- [ ] **Step 3: Add the dispatch helper and the endpoint**

In `backend/api/chapter_linking.py`, at the end of the file, right after Task 6's `list_pending_review` function, add:

```python
async def _apply_entry(
    entry: dict,
    slug: str,
    api_key: str,
    *,
    title: str | None = None,
    authors: list[str] | None = None,
    pdf_start_index: int | None = None,
    pdf_end_index: int | None = None,
    book_key: str | None = None,
) -> dict:
    """Dispatch one queue entry (from review_queue_store.get_entry) to the
    existing per-batch service logic, scoped to just this one item. Shared
    by the approve and execute endpoints -- execute never passes any of
    the optional edit kwargs (bucket="commit" entries are never edited,
    design spec §4/§6). Raises on failure; callers decide whether that
    becomes an HTTPException (approve) or a per-item failure entry
    (execute).
    """
    payload = entry["payload"]
    library_type, numeric_id, library_id = parse_library_slug(slug)

    if entry["type"] == "chapter":
        write_client = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=api_key)
        read_client = ZoteroWebAPI(api_key=api_key)
        chapter = {
            "title": title if title is not None else payload["title"],
            "authors": authors if authors is not None else payload.get("authors", []),
            "pdf_start_index": pdf_start_index if pdf_start_index is not None else payload["pdf_start_index"],
            "pdf_end_index": pdf_end_index if pdf_end_index is not None else payload["pdf_end_index"],
            "citation_pages": payload.get("citation_pages"),
            "confidence": payload.get("confidence", 1.0),
        }
        analyses = [{"item_key": payload["book_key"], "attachment_key": payload["attachment_key"], "chapters": [chapter]}]
        result = await upload_run(
            zotero_write_client=write_client,
            zotero_read_client=read_client,
            slug=slug,
            analyses=analyses,
            commit=True,
            confidence_threshold=0.0,
            target_collection=payload.get("target_collection") or "Book Chapters",
            max_items=None,
        )
        if result["failed"]:
            raise RuntimeError(result["failed"][0]["error"])
        return {"created": result["created"]}

    if entry["type"] == "match":
        write_client = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=api_key)
        chosen_book_key = book_key or payload.get("book_key")
        if not chosen_book_key:
            raise ValueError("book_key is required to approve an ambiguous match")
        would_link = [{"chapter_key": payload["chapter_key"], "book_key": chosen_book_key, "score": payload.get("score", 1.0)}]
        result = await asyncio.to_thread(commit_links, write_client, slug, would_link, payload.get("target_collection"))
        if result["failed"]:
            raise RuntimeError(result["failed"][0]["error"])
        return {"linked": result["linked"]}

    if entry["type"] == "ocr":
        read_client = ZoteroWebAPI(api_key=api_key)
        extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
        ocr_result = await ocr_run(
            zotero_client=read_client,
            extractor=extractor,
            library_id=library_id,
            library_type=library_type,
            attachment_specs=[{"item_key": payload["book_key"], "attachment_key": payload["attachment_key"]}],
            max_items=None,
            cache_dir=_Path("data/ocr_cache"),
            progress_callback=lambda p, m: None,
        )
        review_queue_store.remove_entry(get_settings().review_queue_path, slug, entry["queue_id"])
        analyze_result = await analyze_run(
            zotero_client=read_client,
            library_id=library_id,
            library_type=library_type,
            slug=slug,
            item_keys=[payload["book_key"]],
            max_items=None,
            relink=False,
            progress_callback=lambda p, m: None,
        )
        return {"ocr": ocr_result, "analysis": analyze_result}

    raise ValueError(f"unknown queue entry type: {entry['type']!r}")


class ReviewApproveRequest(BaseModel):
    library_slug: str
    api_key: str
    title: str | None = None
    authors: list[str] | None = None
    pdf_start_index: int | None = None
    pdf_end_index: int | None = None
    book_key: str | None = None


@router.post(
    "/chapter-linking/review/{queue_id}/approve",
    summary="Approve one pending review-queue entry (admin only)",
)
async def approve_review_entry(
    queue_id: str,
    request: ReviewApproveRequest,
    identity: ZoteroIdentity | None = Depends(require_authorized_group_admin),
) -> dict:
    path = get_settings().review_queue_path
    entry = review_queue_store.get_entry(path, request.library_slug, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Queue entry {queue_id!r} not found")
    try:
        result = await _apply_entry(
            entry, request.library_slug, request.api_key,
            title=request.title, authors=request.authors,
            pdf_start_index=request.pdf_start_index, pdf_end_index=request.pdf_end_index,
            book_key=request.book_key,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a 502, not silently swallowed
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if entry["type"] != "ocr":
        review_queue_store.set_status(path, request.library_slug, queue_id, "approved")
    return {"queue_id": queue_id, "status": "approved", "result": result}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 6 new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/review/{queue_id}/approve"
```

---

### Task 8: API — `POST .../review/{queue_id}/reject`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestReviewEndpoints` (after the approve tests):

```python
    def test_reject_marks_entry_rejected(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/ocr:ATT1/reject", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 200)
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT1")
        self.assertEqual(entry["status"], "rejected")

    def test_reject_unknown_queue_id_returns_404(self):
        self._override_admin()
        response = self.client.post(
            "/api/chapter-linking/review/does-not-exist/reject", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -k reject -v`
Expected: FAIL — `404 Not Found` (route doesn't exist).

- [ ] **Step 3: Add the endpoint**

In `backend/api/chapter_linking.py`, after `approve_review_entry`, add:

```python
@router.post(
    "/chapter-linking/review/{queue_id}/reject",
    summary="Reject one pending review-queue entry (admin only, no Zotero write)",
)
async def reject_review_entry(
    queue_id: str,
    library_slug: str,
    identity: ZoteroIdentity | None = Depends(require_authorized_group_admin),
) -> dict:
    path = get_settings().review_queue_path
    entry = review_queue_store.get_entry(path, library_slug, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Queue entry {queue_id!r} not found")
    review_queue_store.set_status(path, library_slug, queue_id, "rejected")
    return {"queue_id": queue_id, "status": "rejected"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 2 new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/review/{queue_id}/reject"
```

---

### Task 9: API — `POST .../review/execute`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestReviewEndpoints` (after the reject tests):

```python
    def test_execute_runs_each_commit_entry_and_marks_approved(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit",
             "payload": {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 0.95}},
        ])
        with patch("backend.api.chapter_linking.commit_links", return_value={"linked": [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 0.95}], "failed": []}):
            response = self.client.post(
                "/api/chapter-linking/review/execute",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "queue_ids": ["match:CHAP1"]},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["executed"], ["match:CHAP1"])
        self.assertEqual(body["failed"], [])
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "match:CHAP1")
        self.assertEqual(entry["status"], "approved")

    def test_execute_isolates_per_item_failures(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit",
             "payload": {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 0.95}},
        ])
        with patch("backend.api.chapter_linking.commit_links", side_effect=RuntimeError("boom")):
            response = self.client.post(
                "/api/chapter-linking/review/execute",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "queue_ids": ["match:CHAP1"]},
            )
        body = response.json()
        self.assertEqual(body["executed"], [])
        self.assertEqual(len(body["failed"]), 1)
        self.assertEqual(body["failed"][0]["queue_id"], "match:CHAP1")

    def test_execute_rejects_review_bucket_entries(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "review",
             "payload": {"chapter_key": "CHAP1", "candidates": []}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/execute",
            json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "queue_ids": ["match:CHAP1"]},
        )
        body = response.json()
        self.assertEqual(body["executed"], [])
        self.assertEqual(len(body["failed"]), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -k execute -v`
Expected: FAIL — `404 Not Found`.

- [ ] **Step 3: Add the endpoint**

In `backend/api/chapter_linking.py`, after `reject_review_entry`, add:

```python
class ReviewExecuteRequest(BaseModel):
    library_slug: str
    api_key: str
    queue_ids: list[str]


@router.post(
    "/chapter-linking/review/execute",
    summary="Execute (commit) a batch of pending commit-queue entries (admin only)",
)
async def execute_review_entries(
    request: ReviewExecuteRequest,
    identity: ZoteroIdentity | None = Depends(require_authorized_group_admin),
) -> dict:
    path = get_settings().review_queue_path
    executed: list[str] = []
    failed: list[dict] = []
    for queue_id in request.queue_ids:
        entry = review_queue_store.get_entry(path, request.library_slug, queue_id)
        if entry is None or entry["bucket"] != "commit" or entry["status"] != "pending":
            failed.append({"queue_id": queue_id, "error": "not a pending commit-bucket entry"})
            continue
        try:
            await _apply_entry(entry, request.library_slug, request.api_key)
        except Exception as exc:  # noqa: BLE001 — isolate per-item, matches commit_links()'s own convention
            failed.append({"queue_id": queue_id, "error": str(exc)})
            continue
        review_queue_store.set_status(path, request.library_slug, queue_id, "approved")
        executed.append(queue_id)
    return {"executed": executed, "failed": failed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 3 new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/review/execute"
```

---

### Task 10: API — `POST .../review/{queue_id}/send-to-review`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestReviewEndpoints` (after the execute tests):

```python
    def test_send_to_review_flips_bucket(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "chapter:BOOK1:2-4", "type": "chapter", "bucket": "commit", "payload": {}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/chapter:BOOK1:2-4/send-to-review", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 200)
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "chapter:BOOK1:2-4")
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["status"], "pending")

    def test_send_to_review_rejects_review_bucket_entries(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "chapter:BOOK1:2-4", "type": "chapter", "bucket": "review", "payload": {}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/chapter:BOOK1:2-4/send-to-review", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 400)

    def test_send_to_review_unknown_queue_id_returns_404(self):
        self._override_admin()
        response = self.client.post(
            "/api/chapter-linking/review/does-not-exist/send-to-review", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -k send_to_review -v`
Expected: FAIL — `404 Not Found` for all three (route doesn't exist).

- [ ] **Step 3: Add the endpoint**

In `backend/api/chapter_linking.py`, after `execute_review_entries`, add:

```python
@router.post(
    "/chapter-linking/review/{queue_id}/send-to-review",
    summary="Move one pending commit-queue entry to the review queue (admin only)",
)
async def send_entry_to_review(
    queue_id: str,
    library_slug: str,
    identity: ZoteroIdentity | None = Depends(require_authorized_group_admin),
) -> dict:
    path = get_settings().review_queue_path
    entry = review_queue_store.get_entry(path, library_slug, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Queue entry {queue_id!r} not found")
    if entry["bucket"] != "commit":
        raise HTTPException(status_code=400, detail="Only commit-bucket entries can be sent to review")
    review_queue_store.set_bucket(path, library_slug, queue_id, "review")
    return {"queue_id": queue_id, "bucket": "review"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 3 new ones.

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/review/{queue_id}/send-to-review"
```

---

### Task 11: Admin page — `/admin/review`

**Files:**
- Create: `backend/api/admin_pages.py`
- Create: `backend/templates/admin_review.html`
- Modify: `backend/main.py`
- Test: `backend/tests/test_admin_pages.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_admin_pages.py`:

```python
"""Tests for backend.api.admin_pages."""

import unittest

from fastapi.testclient import TestClient

from backend.main import app


class TestReviewPage(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_renders_without_error(self):
        response = self.client.get("/admin/review")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chapter Review Queue", response.text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_admin_pages.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Create the router**

Create `backend/api/admin_pages.py`:

```python
"""Admin-only HTML pages. Currently just the chapter-review queue page
(design spec docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md
section 6). Registered without the /api prefix (see backend/main.py),
matching backend/api/public_query.py's own APIRouter(prefix=...)
convention -- a router mounted under /api in main.py cannot also serve an
unprefixed path.

No server-side auth on the page route itself: /admin/* is not covered by
main.py's api_key_middleware (which only gates /api/*), and this codebase
has no cookie/session mechanism to authenticate a plain page GET. The page
is a static shell; every actual data read/write happens via the admin's
own client-side fetch() calls to the already admin-gated
/api/chapter-linking/review/* endpoints, with the API key entered once in
the page and sent as the X-Zotero-API-Key header on each call.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/admin")

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/review", response_class=HTMLResponse, summary="Chapter-review queue admin page")
async def review_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse("admin_review.html", {"request": request})
```

- [ ] **Step 4: Create the template**

Create `backend/templates/admin_review.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chapter Review Queue — Zotero RAG</title>
<style>
  body { font-family: Georgia, serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
  .credentials { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  .credentials input { padding: 0.4rem; font-family: inherit; border: 1px solid #ccc; border-radius: 4px; }
  .credentials input[name="library_slug"] { width: 14rem; }
  .credentials input[name="api_key"] { width: 20rem; }
  .credentials button { padding: 0.4rem 1rem; background: #1a5c9e; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
  nav.views, nav.tabs { margin-bottom: 1rem; }
  nav.views button, nav.tabs button {
    padding: 0.4rem 0.9rem; margin-right: 0.4rem; border: 1px solid #1a5c9e; background: #fff;
    color: #1a5c9e; border-radius: 4px; cursor: pointer; font-family: inherit;
  }
  nav.views button.active, nav.tabs button.active { background: #1a5c9e; color: #fff; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 2rem; font-size: 0.9rem; }
  th, td { border-bottom: 1px solid #ddd; padding: 0.5rem; text-align: left; vertical-align: top; }
  th { color: #555; font-weight: normal; }
  input.edit-field { width: 100%; box-sizing: border-box; padding: 0.2rem; font-family: inherit; }
  button.row-action { margin-right: 0.3rem; padding: 0.3rem 0.6rem; cursor: pointer; }
  .error { color: #c00; }
  .empty { color: #777; font-style: italic; }
</style>
</head>
<body>
<h1>Chapter Review Queue</h1>

<div class="credentials">
  <input type="text" name="library_slug" placeholder="groups/6297749">
  <input type="password" name="api_key" placeholder="Admin Zotero API key">
  <button id="load-btn">Load</button>
</div>

<nav class="views">
  <button id="view-review" class="active">Needs Review</button>
  <button id="view-commit">Ready to Commit</button>
</nav>

<nav class="tabs" id="review-tabs">
  <button data-type="chapter" class="active">Chapters</button>
  <button data-type="match">Ambiguous Matches</button>
  <button data-type="ocr">Needs OCR</button>
</nav>

<nav class="tabs" id="commit-tabs" style="display:none;">
  <button data-type="chapter" class="active">Chapters</button>
  <button data-type="match">Matches</button>
</nav>

<div id="commit-toolbar" style="display:none; margin-bottom: 1rem;">
  <button id="execute-btn">Execute Selected</button>
</div>

<div id="content"></div>
<p id="status"></p>

<script>
const state = { view: "review", tab: "chapter", entries: [] };

function credentials() {
  return {
    slug: document.querySelector('input[name="library_slug"]').value.trim(),
    apiKey: document.querySelector('input[name="api_key"]').value.trim(),
  };
}

async function apiFetch(path, options = {}) {
  const { apiKey } = credentials();
  const headers = Object.assign({ "Content-Type": "application/json", "X-Zotero-API-Key": apiKey }, options.headers || {});
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return body;
}

function setStatus(message, isError) {
  const el = document.getElementById("status");
  el.textContent = message || "";
  el.className = isError ? "error" : "";
}

async function loadEntries() {
  const { slug } = credentials();
  if (!slug) { setStatus("Enter a library slug first.", true); return; }
  try {
    const data = await apiFetch(`/api/chapter-linking/review/pending?library_slug=${encodeURIComponent(slug)}&bucket=${state.view}`);
    state.entries = data.entries;
    render();
    setStatus("");
  } catch (err) {
    setStatus(err.message, true);
  }
}

function entriesForTab() {
  return state.entries.filter((e) => e.type === state.tab);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function readerUrl(slug, itemKey, attachmentKey) {
  return `https://www.zotero.org/${slug}/items/${itemKey}/attachment/${attachmentKey}/reader`;
}

function itemUrl(slug, itemKey) {
  return `https://www.zotero.org/${slug}/items/${itemKey}`;
}

function render() {
  const container = document.getElementById("content");
  const entries = entriesForTab();
  if (entries.length === 0) {
    container.innerHTML = '<p class="empty">Nothing pending.</p>';
    return;
  }
  if (state.tab === "chapter") container.innerHTML = renderChapterTable(entries);
  else if (state.tab === "match") container.innerHTML = renderMatchTable(entries);
  else container.innerHTML = renderOcrTable(entries);
}

function renderChapterTable(entries) {
  const { slug } = credentials();
  const rows = entries.map((e) => {
    const p = e.payload;
    const checkbox = state.view === "commit" ? `<input type="checkbox" class="select-box" data-id="${e.queue_id}" checked>` : "";
    const actions = state.view === "commit"
      ? `<button class="row-action" onclick="sendToReview('${e.queue_id}')">Send to Review</button>`
      : `<button class="row-action" onclick="approveChapter('${e.queue_id}')">Approve</button>
         <button class="row-action" onclick="rejectEntry('${e.queue_id}')">Reject</button>`;
    return `<tr data-queue-id="${e.queue_id}">
      <td>${checkbox}</td>
      <td><input class="edit-field" data-field="title" value="${escapeHtml(p.title)}"></td>
      <td><input class="edit-field" data-field="pdf_start_index" value="${p.pdf_start_index}" style="width:4rem"></td>
      <td><input class="edit-field" data-field="pdf_end_index" value="${p.pdf_end_index}" style="width:4rem"></td>
      <td>${p.confidence.toFixed(2)}</td>
      <td><a href="${readerUrl(slug, p.book_key, p.attachment_key)}" target="_blank">reader</a></td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
  return `<table><tr><th></th><th>Title</th><th>Start</th><th>End</th><th>Conf.</th><th></th><th></th></tr>${rows}</table>`;
}

function renderMatchTable(entries) {
  const { slug } = credentials();
  const rows = entries.map((e) => {
    const p = e.payload;
    const checkbox = state.view === "commit" ? `<input type="checkbox" class="select-box" data-id="${e.queue_id}" checked>` : "";
    let candidateCell;
    let actions;
    if (state.view === "review") {
      candidateCell = (p.candidates || []).map((c) =>
        `<label><input type="radio" name="cand-${e.queue_id}" value="${c.key}"> ${escapeHtml(c.title)} (${c.year ?? "?"}) <a href="${itemUrl(slug, c.key)}" target="_blank">view</a></label><br>`
      ).join("");
      actions = `<button class="row-action" onclick="approveMatch('${e.queue_id}')">Approve</button>
                 <button class="row-action" onclick="rejectEntry('${e.queue_id}')">Reject</button>`;
    } else {
      candidateCell = `${escapeHtml(p.book_key)} (score ${p.score.toFixed(2)}) <a href="${itemUrl(slug, p.book_key)}" target="_blank">view</a>`;
      actions = `<button class="row-action" onclick="sendToReview('${e.queue_id}')">Send to Review</button>`;
    }
    return `<tr data-queue-id="${e.queue_id}">
      <td>${checkbox}</td>
      <td>${escapeHtml(p.chapter_key)} <a href="${itemUrl(slug, p.chapter_key)}" target="_blank">view</a></td>
      <td>${candidateCell}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
  return `<table><tr><th></th><th>Chapter</th><th>Candidate(s)</th><th></th></tr>${rows}</table>`;
}

function renderOcrTable(entries) {
  const { slug } = credentials();
  const rows = entries.map((e) => {
    const p = e.payload;
    return `<tr data-queue-id="${e.queue_id}">
      <td>${escapeHtml(p.book_key)} <a href="${itemUrl(slug, p.book_key)}" target="_blank">view</a></td>
      <td><a href="${readerUrl(slug, p.book_key, p.attachment_key)}" target="_blank">reader</a></td>
      <td><button class="row-action" onclick="runOcr('${e.queue_id}')">Run OCR</button></td>
    </tr>`;
  }).join("");
  return `<table><tr><th>Book</th><th></th><th></th></tr>${rows}</table>`;
}

function editedChapterFields(queueId) {
  const row = document.querySelector(`tr[data-queue-id="${queueId}"]`);
  const fields = {};
  row.querySelectorAll(".edit-field").forEach((input) => { fields[input.dataset.field] = input.value; });
  return fields;
}

async function approveChapter(queueId) {
  const { slug, apiKey } = credentials();
  const fields = editedChapterFields(queueId);
  try {
    await apiFetch(`/api/chapter-linking/review/${queueId}/approve`, {
      method: "POST",
      body: JSON.stringify({
        library_slug: slug, api_key: apiKey, title: fields.title,
        pdf_start_index: fields.pdf_start_index ? parseInt(fields.pdf_start_index, 10) : null,
        pdf_end_index: fields.pdf_end_index ? parseInt(fields.pdf_end_index, 10) : null,
      }),
    });
    setStatus(`Approved ${queueId}`);
    await loadEntries();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function approveMatch(queueId) {
  const { slug, apiKey } = credentials();
  const picked = document.querySelector(`input[name="cand-${queueId}"]:checked`);
  try {
    await apiFetch(`/api/chapter-linking/review/${queueId}/approve`, {
      method: "POST",
      body: JSON.stringify({ library_slug: slug, api_key: apiKey, book_key: picked ? picked.value : null }),
    });
    setStatus(`Approved ${queueId}`);
    await loadEntries();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function runOcr(queueId) {
  const { slug, apiKey } = credentials();
  try {
    await apiFetch(`/api/chapter-linking/review/${queueId}/approve`, {
      method: "POST",
      body: JSON.stringify({ library_slug: slug, api_key: apiKey }),
    });
    setStatus(`OCR started for ${queueId}`);
    await loadEntries();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function rejectEntry(queueId) {
  const { slug } = credentials();
  try {
    await apiFetch(`/api/chapter-linking/review/${queueId}/reject?library_slug=${encodeURIComponent(slug)}`, { method: "POST" });
    setStatus(`Rejected ${queueId}`);
    await loadEntries();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function sendToReview(queueId) {
  const { slug } = credentials();
  try {
    await apiFetch(`/api/chapter-linking/review/${queueId}/send-to-review?library_slug=${encodeURIComponent(slug)}`, { method: "POST" });
    setStatus(`Moved ${queueId} to review`);
    await loadEntries();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function executeSelected() {
  const { slug, apiKey } = credentials();
  const ids = [...document.querySelectorAll(".select-box:checked")].map((cb) => cb.dataset.id);
  if (ids.length === 0) { setStatus("Nothing selected.", true); return; }
  try {
    const result = await apiFetch("/api/chapter-linking/review/execute", {
      method: "POST",
      body: JSON.stringify({ library_slug: slug, api_key: apiKey, queue_ids: ids }),
    });
    setStatus(`Executed ${result.executed.length}, failed ${result.failed.length}`, result.failed.length > 0);
    await loadEntries();
  } catch (err) {
    setStatus(err.message, true);
  }
}

function switchView(view) {
  state.view = view;
  document.getElementById("view-review").classList.toggle("active", view === "review");
  document.getElementById("view-commit").classList.toggle("active", view === "commit");
  document.getElementById("review-tabs").style.display = view === "review" ? "block" : "none";
  document.getElementById("commit-tabs").style.display = view === "commit" ? "block" : "none";
  document.getElementById("commit-toolbar").style.display = view === "commit" ? "block" : "none";
  const tabs = document.getElementById(view === "review" ? "review-tabs" : "commit-tabs");
  state.tab = tabs.querySelector("button").dataset.type;
  [...tabs.querySelectorAll("button")].forEach((b, i) => b.classList.toggle("active", i === 0));
  loadEntries();
}

document.getElementById("load-btn").addEventListener("click", loadEntries);
document.getElementById("execute-btn").addEventListener("click", executeSelected);
document.getElementById("view-review").addEventListener("click", () => switchView("review"));
document.getElementById("view-commit").addEventListener("click", () => switchView("commit"));
[...document.querySelectorAll("#review-tabs button, #commit-tabs button")].forEach((btn) => {
  btn.addEventListener("click", () => {
    state.tab = btn.dataset.type;
    const parent = btn.parentElement;
    [...parent.querySelectorAll("button")].forEach((b) => b.classList.toggle("active", b === btn));
    render();
  });
});
</script>
</body>
</html>
```

- [ ] **Step 5: Register the router**

In `backend/main.py`, change the import line:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, chapter_linking
```

to:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, chapter_linking, admin_pages
```

Change:

```python
app.include_router(chapter_linking.router, prefix="/api", tags=["chapter-linking"])
```

to:

```python
app.include_router(chapter_linking.router, prefix="/api", tags=["chapter-linking"])
app.include_router(admin_pages.router, tags=["admin"])
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_admin_pages.py -v`
Expected: PASS.

- [ ] **Step 7: Manual smoke test**

Start the backend (`npm start`) and open `http://localhost:8119/admin/review` in a browser. Expected: the page loads with the credentials form, view/tab buttons, and no console errors before entering a library slug. Enter a library slug + a real admin API key and click Load — expected: either a populated table (if pending items exist) or "Nothing pending." with no JS errors in the browser console. Report the actual result; do not claim success without having done this.

- [ ] **Step 8: Run the full backend test suite to check for regressions**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/api/admin_pages.py backend/templates/admin_review.html backend/main.py backend/tests/test_admin_pages.py
git commit -m "feat: add /admin/review chapter-review queue page"
```

---

### Task 12: Documentation

**Files:**
- Modify: `docs/chapter-segmentation.md`

- [ ] **Step 1: Add a new section documenting the review UI**

In `docs/chapter-segmentation.md`, after the "Running via the API" section (before the end of the file), add:

```markdown
## Reviewing uncertain and confident results

Every dry-run/analyze invocation of the four scripts above (CLI or API)
automatically feeds two persistent, per-library queues: a **review
queue** for items that need a human decision (low-confidence chapter
boundaries, ambiguous retrofit matches, `needs_ocr` attachments) and a
**commit queue** for items a real `--commit` run would already write on
its own, for a final look before it happens.

An admin can act on both from `/admin/review`: enter a library slug and
an admin Zotero API key (must belong to an owner/admin of the server's
`AUTHORIZED_GROUP_ID`), then switch between the **Needs Review** view
(approve as-is, edit page range/title/candidate then approve, reject, or
trigger OCR) and the **Ready to Commit** view (select confident items and
"Execute Selected", or "Send to Review" to defer one instead). Approving
or executing an entry writes to Zotero immediately, using the exact same
logic the scripts themselves use — see
`docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md` for the
full design.

The same functionality is available via `GET/POST
/api/chapter-linking/review/*` for scripting or a future non-web client.
```

- [ ] **Step 2: Commit**

```bash
git add docs/chapter-segmentation.md
git commit -m "docs: document the chapter review UI"
```

---

### Task 13: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full default test suite**

Run: `uv run pytest`
Expected: PASS — every test file under `backend/tests/`, excluding `integration`/`api`/`container`-marked tests.

- [ ] **Step 2: Run the Node test suites (unaffected by this plan, but confirm no accidental breakage)**

Run: `npm run test:node && npm run test:plugin`
Expected: PASS — this plan touches no `bin/` or `plugin/` files.

- [ ] **Step 3: Manual end-to-end check of the admin page against real data**

Following CLAUDE.md's "Live Query Debugging" conventions, use the `test-rag-plugin` group library (`groups/6297749`) for this — never a real personal/project library. Run a dry-run of `scripts/analyze_book_chapters.py` (or the retrofit/upload scripts) against it with a read-only key to populate the queue, then open `/admin/review`, load that library with a write-scoped admin key, and confirm at least one pending item renders correctly in each view/tab that has data. Report the actual result honestly — if no admin/owner key for that group is available in this environment, say so rather than assuming success.

---

## Self-review notes

- **Spec coverage**: §2 (architecture) → Tasks 2–11 collectively; §3 (data model/storage) → Task 2; §4 (API endpoints) → Tasks 6–10; §5 (script integration) → Tasks 3–5 (with the analyze-side correction already applied to the spec itself before this plan was written); §6 (web UI) → Task 11; §7 (future extensions) → intentionally not implemented, no task; §8 (testing) → covered throughout, plus Task 13.
- **Zero-refactor chapter/OCR approval**: confirmed via direct reading of `chapter_upload.py`/`chapter_ocr.py` that their per-item logic is fully inlined in a loop, not a separable function — Task 7's `_apply_entry` calls the existing batch `run()` functions with a synthetic single-item input instead, exactly as the spec anticipated ("reusing `upload_chapters.py`'s existing per-chapter logic").
- **Settings-redirection risk caught during planning**: `get_settings()` is a mutable singleton whose default `data_path` resolves to the real repo's `data/` directory; without redirecting `review_queue_path` in each affected test's `setUp`, Tasks 3–7's new tests (and every pre-existing test in the same classes, once `run()` unconditionally calls `review_queue_store.upsert_many`) would write to the real `data/system/review_queue.json` on every `uv run pytest`. Every task that wires a `run()` function to the queue store adds `setUp`/`tearDown` redirecting `review_queue_path`/`data_path` to a `tempfile.TemporaryDirectory()`, mirroring the exact pattern already used in `test_autoindex_api.py`.
