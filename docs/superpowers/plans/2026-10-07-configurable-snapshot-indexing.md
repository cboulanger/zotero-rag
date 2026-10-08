# Configurable Snapshot-Attachment Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global, admin-controlled, default-off setting ("Index Snapshots of webpages") that silently excludes Zotero webpage-snapshot attachments (title exactly `"Snapshot"`) from indexing and from the Fix Unavailable dialog, with an admin-only, double-confirmed way to purge already-indexed snapshot chunks server-wide.

**Architecture:** A tiny JSON-file-backed settings store (same pattern as the existing autoindex scheduler pause state) holds one boolean, read/written through a new admin-gated API. The backend's own attachment-selection logic and the plugin's attachment-collection/Fix-Unavailable-scan logic both consult this flag at the single point each already filters by indexable MIME type. A new `attachment_title` field on stored chunk metadata (schema v7) lets a new `VectorStore.delete_snapshot_chunks()` method find and remove already-indexed snapshot content across every library in one pass.

**Tech Stack:** FastAPI (backend/api), Pydantic models (backend/models), Qdrant via `backend/db/vector_store.py`, vanilla JS plugin code loaded into a Zotero chrome window, Node's built-in test runner for the plugin, `unittest`/`pytest` for the backend.

**Spec:** `docs/superpowers/specs/2026-10-07-configurable-snapshot-indexing-design.md`

---

## Backend tasks (do these first, in order — each builds on the last)

### Task 1: `DocumentMetadata.attachment_title` field (schema v7)

**Files:**
- Modify: `backend/models/document.py`
- Test: `backend/tests/test_document_processor.py` (new, minimal — the field itself is exercised fully by later tasks)

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_document_processor.py`, near the top-level helpers (after `_attachment`, before `class TestDocumentProcessor`):

```python
class TestDocumentMetadataAttachmentTitle(unittest.TestCase):
    def test_attachment_title_defaults_to_none_and_schema_version_is_7(self):
        from backend.models.document import DocumentMetadata, CURRENT_SCHEMA_VERSION
        meta = DocumentMetadata(library_id="1", item_key="ABC")
        self.assertIsNone(meta.attachment_title)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)

    def test_attachment_title_can_be_set(self):
        from backend.models.document import DocumentMetadata
        meta = DocumentMetadata(library_id="1", item_key="ABC", attachment_title="Snapshot")
        self.assertEqual(meta.attachment_title, "Snapshot")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py::TestDocumentMetadataAttachmentTitle -v`
Expected: FAIL — `AttributeError: 'DocumentMetadata' object has no attribute 'attachment_title'` and/or `CURRENT_SCHEMA_VERSION` still `6`.

- [ ] **Step 3: Implement**

In `backend/models/document.py`, change:

```python
CURRENT_SCHEMA_VERSION: int = 6
```

to:

```python
CURRENT_SCHEMA_VERSION: int = 7
```

And in `DocumentMetadata` (after the existing `attachment_key` field):

```python
    attachment_key: Optional[str] = Field(None, description="PDF attachment key")
    attachment_title: Optional[str] = Field(
        None,
        description=(
            "The attachment's own Zotero title (not the parent item's `title` field "
            "above). Used to identify Snapshot-titled webpage attachments for the "
            "admin-controlled indexing toggle and purge (see "
            "backend.services.admin_settings_store). None for chunks indexed before "
            "this field existed, or for chunks with no specific source attachment "
            "(e.g. abstract-fallback chunks)."
        ),
    )
```

Also bump the schema-version changelog comment in `ChunkMetadata` (just above `schema_version: int = Field(...)`):

```python
    # v6: added tags (Zotero keywords) and tags_lower keyword field for filtering
    # v7: added attachment_title (DocumentMetadata) for Snapshot-attachment identification
    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py::TestDocumentMetadataAttachmentTitle -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/models/document.py backend/tests/test_document_processor.py
git commit -m "feat(backend): add attachment_title field to DocumentMetadata (schema v7)"
```

---

### Task 2: Store `attachment_title` in Qdrant payload (VectorStore)

**Files:**
- Modify: `backend/db/vector_store.py` (3 payload-construction sites: `add_chunk`, `add_chunks_batch`, `copy_chunks_cross_library`)
- Test: `backend/tests/test_vector_store.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_vector_store.py`, inside `class TestVectorStore` (anywhere after `test_add_chunk`):

```python
    def test_add_chunk_stores_attachment_title_in_payload(self):
        chunk = DocumentChunk(
            text="Snapshot page text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-snap-1",
                document_metadata=DocumentMetadata(
                    library_id="1",
                    item_key="ABC123",
                    attachment_key="ATT1",
                    attachment_title="Snapshot",
                    title="Test Paper",
                ),
                page_number=1,
                text_preview="Snapshot page",
                chunk_index=0,
                content_hash="hash-snap-1",
            ),
            embedding=[0.1] * 384,
        )
        point_id = self.vector_store.add_chunk(chunk)
        points = self.vector_store.client.retrieve(
            collection_name=self.vector_store.CHUNKS_COLLECTION, ids=[point_id]
        )
        self.assertEqual(points[0].payload.get("attachment_title"), "Snapshot")

    def test_copy_chunks_cross_library_preserves_attachment_title(self):
        source = DocumentChunk(
            text="Snapshot page text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-src-1",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="SRC1", attachment_key="SA1",
                    attachment_title="Snapshot",
                ),
                page_number=1, text_preview="Snapshot page", chunk_index=0,
                content_hash="hash-cross-1",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(source)
        target_meta = DocumentMetadata(
            library_id="2", item_key="TGT1", attachment_key="TA1",
            attachment_title="Snapshot", title="Copied",
        )
        copied = self.vector_store.copy_chunks_cross_library(
            source_library_id="1", source_item_key="SRC1",
            target_library_id="2", target_item_key="TGT1",
            target_attachment_key="TA1", target_doc_metadata=target_meta,
            target_item_version=1, target_attachment_version=1,
            target_item_modified="2026-01-01T00:00:00Z",
        )
        self.assertEqual(copied, 1)
        chunks = self.vector_store.get_item_chunks("2", "TGT1")
        self.assertEqual(chunks[0]["payload"].get("attachment_title"), "Snapshot")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_vector_store.py -k attachment_title -v`
Expected: FAIL — both assertions get `None` instead of `"Snapshot"`.

- [ ] **Step 3: Implement**

In `backend/db/vector_store.py`'s `add_chunk` (~line 300-324), add one line to the payload dict, right after `"attachment_key"`:

```python
                "attachment_key": chunk.metadata.document_metadata.attachment_key,
                "attachment_title": chunk.metadata.document_metadata.attachment_title,
                "title": chunk.metadata.document_metadata.title,
```

Same addition in `add_chunks_batch` (~line 359-385):

```python
                    "attachment_key": chunk.metadata.document_metadata.attachment_key,
                    "attachment_title": chunk.metadata.document_metadata.attachment_title,
                    "title": chunk.metadata.document_metadata.title,
```

And in `copy_chunks_cross_library`'s `new_payload` dict (~line 976-998), add after `"attachment_key"`:

```python
                    "attachment_key":     target_attachment_key,
                    "attachment_title":   target_doc_metadata.attachment_title,
                    "title":              target_doc_metadata.title,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_vector_store.py -k attachment_title -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/db/vector_store.py backend/tests/test_vector_store.py
git commit -m "feat(backend): persist attachment_title in chunk payload"
```

---

### Task 3: `VectorStore.delete_snapshot_chunks()`

**Files:**
- Modify: `backend/db/vector_store.py`
- Test: `backend/tests/test_vector_store.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_vector_store.py`, inside `class TestVectorStore`:

```python
    def _add_snapshot_chunk(self, library_id, item_key, attachment_key, chunk_id):
        chunk = DocumentChunk(
            text="Snapshot text.",
            metadata=ChunkMetadata(
                chunk_id=chunk_id,
                document_metadata=DocumentMetadata(
                    library_id=library_id, item_key=item_key,
                    attachment_key=attachment_key, attachment_title="Snapshot",
                ),
                page_number=1, text_preview="Snapshot text", chunk_index=0,
                content_hash=chunk_id,
            ),
            embedding=[0.1] * 384,
        )
        return self.vector_store.add_chunk(chunk)

    def test_delete_snapshot_chunks_removes_only_snapshot_titled_chunks_across_libraries(self):
        self._add_snapshot_chunk("1", "ITEM1", "SNAP1", "chunk-s1")
        self._add_snapshot_chunk("2", "ITEM2", "SNAP2", "chunk-s2")
        # A normal (non-Snapshot) chunk in library 1 must survive.
        pdf_chunk = DocumentChunk(
            text="PDF text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-pdf",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM1", attachment_key="PDF1",
                    attachment_title="Full Paper.pdf",
                ),
                page_number=1, text_preview="PDF text", chunk_index=0,
                content_hash="chunk-pdf",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(pdf_chunk)
        # A legacy chunk with no attachment_title at all must also survive.
        legacy_chunk = DocumentChunk(
            text="Legacy text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-legacy",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM3", attachment_key="LEG1",
                ),
                page_number=1, text_preview="Legacy text", chunk_index=0,
                content_hash="chunk-legacy",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(legacy_chunk)

        deleted_chunks, deleted_attachments = self.vector_store.delete_snapshot_chunks()

        self.assertEqual(deleted_chunks, 2)
        self.assertEqual(deleted_attachments, 2)
        self.assertEqual(len(self.vector_store.get_item_chunks("1", "ITEM1")), 1)  # PDF chunk survives
        self.assertEqual(self.vector_store.get_item_chunks("1", "ITEM1")[0]["payload"]["attachment_key"], "PDF1")
        self.assertEqual(len(self.vector_store.get_item_chunks("2", "ITEM2")), 0)
        self.assertEqual(len(self.vector_store.get_item_chunks("1", "ITEM3")), 1)  # legacy chunk survives

    def test_delete_snapshot_chunks_returns_zero_when_nothing_matches(self):
        deleted_chunks, deleted_attachments = self.vector_store.delete_snapshot_chunks()
        self.assertEqual((deleted_chunks, deleted_attachments), (0, 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_vector_store.py -k delete_snapshot_chunks -v`
Expected: FAIL — `AttributeError: 'VectorStore' object has no attribute 'delete_snapshot_chunks'`

- [ ] **Step 3: Implement**

In `backend/db/vector_store.py`, add this method right after `delete_chunks_by_ids` (~line 1497, before `count_library_chunks`):

```python
    def delete_snapshot_chunks(self) -> tuple[int, int]:
        """
        Delete every chunk across ALL libraries whose source attachment was
        titled exactly "Snapshot" (Zotero's default webpage-snapshot title),
        plus the deduplication record for each (library_id, item_key) pair
        touched — same item-level dedup granularity as delete_item_chunks.

        Used by the admin-only POST /api/admin/settings/purge-snapshots
        endpoint. Chunks indexed before the attachment_title field existed
        have attachment_title=None and are never matched, so this is a no-op
        on data from before this feature shipped until it's re-indexed.

        No payload index exists on attachment_title (this runs rarely, as an
        explicit admin maintenance action, not a hot path — same tradeoff
        already made for attachment_key, which also has no index).

        Returns:
            (deleted_chunks, deleted_attachments) — deleted_attachments counts
            distinct (library_id, item_key, attachment_key) triples, since one
            attachment can have multiple chunks.
        """
        scroll_filter = Filter(must=[
            FieldCondition(key="attachment_title", match=MatchValue(value="Snapshot"))
        ])
        point_ids: list[str] = []
        touched_items: set[tuple[str, str]] = set()
        touched_attachments: set[tuple[str, str, str]] = set()
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter=scroll_filter,
                limit=1000,
                offset=offset,
                with_payload=["library_id", "item_key", "attachment_key"],
                with_vectors=False,
            )
            for point in points:
                point_ids.append(point.id)
                library_id = point.payload.get("library_id")
                item_key = point.payload.get("item_key")
                attachment_key = point.payload.get("attachment_key")
                if library_id and item_key:
                    touched_items.add((library_id, item_key))
                    touched_attachments.add((library_id, item_key, attachment_key))
            if offset is None:
                break

        deleted_chunks = self.delete_chunks_by_ids(point_ids)
        for library_id, item_key in touched_items:
            if not self.get_item_chunks(library_id, item_key):
                self.delete_item_deduplication_records(library_id, item_key)

        logger.info(
            f"Purged {deleted_chunks} Snapshot chunk(s) across {len(touched_attachments)} attachment(s)"
        )
        return deleted_chunks, len(touched_attachments)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_vector_store.py -k "delete_snapshot_chunks or attachment_title" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/db/vector_store.py backend/tests/test_vector_store.py
git commit -m "feat(backend): add VectorStore.delete_snapshot_chunks for cross-library purge"
```

---

### Task 4: Admin settings store (`admin_settings_store.py`)

**Files:**
- Create: `backend/services/admin_settings_store.py`
- Test: `backend/tests/test_admin_settings_store.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_admin_settings_store.py`:

```python
"""Unit tests for the admin settings JSON state store."""

import tempfile
import unittest
from pathlib import Path

from backend.services.admin_settings_store import read_admin_settings, write_admin_settings


class TestAdminSettingsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_returns_index_snapshots_false_when_file_missing(self):
        settings = read_admin_settings(self.data_path)
        self.assertEqual(settings, {"index_snapshots": False})

    def test_write_then_read_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        self.assertEqual(read_admin_settings(self.data_path), {"index_snapshots": True})

    def test_write_then_read_false_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        write_admin_settings(self.data_path, {"index_snapshots": False})
        self.assertEqual(read_admin_settings(self.data_path), {"index_snapshots": False})

    def test_read_returns_default_on_corrupt_file(self):
        system_dir = self.data_path / "system"
        system_dir.mkdir(parents=True)
        (system_dir / "admin_settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(read_admin_settings(self.data_path), {"index_snapshots": False})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_admin_settings_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.admin_settings_store'`

- [ ] **Step 3: Implement**

Create `backend/services/admin_settings_store.py`:

```python
"""Persisted store for global, admin-controlled runtime settings.

Mirrors backend.services.autoindex_scheduler's state-file pattern: a small
JSON file under data_path/system/, atomically written, with a safe default
when the file doesn't exist yet. Unlike backend.config.settings.Settings
(env-var-backed, fixed at process start), this store is meant to be toggled
live by a server admin without a restart or redeploy.
"""

import json
import os
import tempfile
from pathlib import Path

DEFAULT_ADMIN_SETTINGS = {"index_snapshots": False}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a small JSON state file (Windows-safe via os.replace).

    Mirrors CronIndexer._write_status's pattern (backend/services/cron_indexer.py)
    and autoindex_scheduler._atomic_write_json.
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


def read_admin_settings(data_path: Path) -> dict:
    """Missing or corrupt file reads as the safe default: {"index_snapshots": False}."""
    settings_path = data_path / "system" / "admin_settings.json"
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_ADMIN_SETTINGS)
    merged = dict(DEFAULT_ADMIN_SETTINGS)
    merged.update(loaded)
    return merged


def write_admin_settings(data_path: Path, settings: dict) -> None:
    _atomic_write_json(data_path / "system" / "admin_settings.json", settings)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_admin_settings_store.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/admin_settings_store.py backend/tests/test_admin_settings_store.py
git commit -m "feat(backend): add admin_settings_store for the index-snapshots toggle"
```

---

### Task 5: Admin settings API endpoints

**Files:**
- Create: `backend/api/admin_settings.py`
- Modify: `backend/main.py` (register router)
- Test: `backend/tests/test_admin_settings_api.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_admin_settings_api.py`, following the exact admin-gating test pattern from `backend/tests/test_autoindex_api.py`:

```python
"""Endpoint tests for GET/PUT /api/admin/settings and POST /api/admin/settings/purge-snapshots."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.dependencies import require_authorized_group_admin, get_vector_store
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache


class AdminSettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _override_admin(self, identity):
        app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_get_returns_default_false_with_no_identity_required(self):
        r = self.client.get("/api/admin/settings")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"index_snapshots": False})

    def test_put_requires_admin(self):
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)):
            r = self.client.put(
                "/api/admin/settings", json={"index_snapshots": True},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_put_as_admin_persists_and_get_reflects_it(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.put("/api/admin/settings", json={"index_snapshots": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"index_snapshots": True})
        r2 = self.client.get("/api/admin/settings")
        self.assertEqual(r2.json(), {"index_snapshots": True})

    def test_purge_snapshots_requires_admin(self):
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)):
            r = self.client.post(
                "/api/admin/settings/purge-snapshots",
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_purge_snapshots_as_admin_calls_vector_store_and_returns_counts(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        mock_store = Mock()
        mock_store.delete_snapshot_chunks.return_value = (5, 2)
        app.dependency_overrides[get_vector_store] = lambda: mock_store
        r = self.client.post("/api/admin/settings/purge-snapshots")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"deleted_chunks": 5, "deleted_attachments": 2})
        mock_store.delete_snapshot_chunks.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_admin_settings_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.api.admin_settings'` (or 404s once routing is attempted)

- [ ] **Step 3: Implement**

Create `backend/api/admin_settings.py`:

```python
"""Global admin-controlled runtime settings.

GET  /api/admin/settings                  — any authenticated identity (or loopback)
PUT  /api/admin/settings                  — admin only; body {"index_snapshots": bool}
POST /api/admin/settings/purge-snapshots  — admin only; no body

"Admin" here is the same concept used by the autoindex scheduler's
pause/resume/run-now controls: owner/admin of the server's
AUTHORIZED_GROUP_ID — see backend.dependencies.require_authorized_group_admin.

index_snapshots (default False) controls whether Zotero webpage-snapshot
attachments (title exactly "Snapshot") are indexed at all — see
backend.services.document_processor's _is_indexable_attachment and
plugin/src/zotero-rag.js's getIndexSnapshotsEnabled(). purge-snapshots
deletes every already-indexed chunk whose source attachment was a Snapshot,
across every library on the server — see VectorStore.delete_snapshot_chunks.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store, require_authorized_group_admin
from backend.services.admin_settings_store import read_admin_settings, write_admin_settings
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()
logger = logging.getLogger(__name__)


class AdminSettings(BaseModel):
    index_snapshots: bool = False


class PurgeSnapshotsResponse(BaseModel):
    deleted_chunks: int
    deleted_attachments: int


@router.get("/admin/settings", summary="Read global admin-controlled settings", response_model=AdminSettings)
async def get_admin_settings() -> AdminSettings:
    settings = get_settings()
    state = await asyncio.to_thread(read_admin_settings, settings.data_path)
    return AdminSettings(index_snapshots=state.get("index_snapshots", False))


@router.put("/admin/settings", summary="Update global admin-controlled settings (admin only)", response_model=AdminSettings)
async def put_admin_settings(
    body: AdminSettings,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> AdminSettings:
    settings = get_settings()
    await asyncio.to_thread(write_admin_settings, settings.data_path, {"index_snapshots": body.index_snapshots})
    return body


@router.post(
    "/admin/settings/purge-snapshots",
    summary="Delete every already-indexed Snapshot-attachment chunk, across all libraries (admin only)",
    response_model=PurgeSnapshotsResponse,
)
async def purge_snapshots(
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> PurgeSnapshotsResponse:
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    deleted_chunks, deleted_attachments = await asyncio.to_thread(vector_store.delete_snapshot_chunks)
    return PurgeSnapshotsResponse(deleted_chunks=deleted_chunks, deleted_attachments=deleted_attachments)
```

In `backend/main.py`, change the import line:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, migration
```

to:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, migration, admin_settings
```

and add, after the `autoindex.router` line (~line 221):

```python
app.include_router(autoindex.router, prefix="/api", tags=["autoindex"])
app.include_router(admin_settings.router, prefix="/api", tags=["admin-settings"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_admin_settings_api.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/api/admin_settings.py backend/main.py backend/tests/test_admin_settings_api.py
git commit -m "feat(backend): add GET/PUT /api/admin/settings and purge-snapshots endpoints"
```

---

### Task 6: `_is_indexable_attachment` helper, wired into `_index_item`

**Files:**
- Modify: `backend/services/document_processor.py`
- Test: `backend/tests/test_document_processor.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_document_processor.py`, as a new top-level test class (after `TestDocumentMetadataAttachmentTitle`):

```python
class TestIsIndexableAttachment(unittest.TestCase):
    def test_non_indexable_mime_type_is_always_excluded(self):
        from backend.services.document_processor import _is_indexable_attachment
        att = {"contentType": "image/png", "title": "diagram.png"}
        self.assertFalse(_is_indexable_attachment(att, index_snapshots_enabled=True))
        self.assertFalse(_is_indexable_attachment(att, index_snapshots_enabled=False))

    def test_snapshot_titled_html_excluded_when_disabled(self):
        from backend.services.document_processor import _is_indexable_attachment
        att = {"contentType": "text/html", "title": "Snapshot"}
        self.assertFalse(_is_indexable_attachment(att, index_snapshots_enabled=False))

    def test_snapshot_titled_html_included_when_enabled(self):
        from backend.services.document_processor import _is_indexable_attachment
        att = {"contentType": "text/html", "title": "Snapshot"}
        self.assertTrue(_is_indexable_attachment(att, index_snapshots_enabled=True))

    def test_renamed_html_attachment_always_included_regardless_of_flag(self):
        from backend.services.document_processor import _is_indexable_attachment
        att = {"contentType": "text/html", "title": "My notes on this page"}
        self.assertTrue(_is_indexable_attachment(att, index_snapshots_enabled=False))
        self.assertTrue(_is_indexable_attachment(att, index_snapshots_enabled=True))

    def test_pdf_always_included_regardless_of_title_or_flag(self):
        from backend.services.document_processor import _is_indexable_attachment
        att = {"contentType": "application/pdf", "title": "Snapshot"}
        self.assertTrue(_is_indexable_attachment(att, index_snapshots_enabled=False))
```

Also add, inside `class TestDocumentProcessor`:

```python
    async def test_index_item_skips_snapshot_attachment_when_setting_off(self):
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": False}):
            item = {
                "data": {"key": "ITEM1", "itemType": "document", "title": "A Page", "version": 1},
                "version": 1,
            }
            self.mock_zotero_client.get_item_children.return_value = [
                {"data": {"key": "SNAP1", "contentType": "text/html", "title": "Snapshot"}, "version": 1},
            ]
            chunks = await self.processor._index_item(item, "1", "user")
        self.assertEqual(chunks, 0)
        self.mock_zotero_client.get_attachment_file.assert_not_called()

    async def test_index_item_indexes_snapshot_attachment_when_setting_on(self):
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": True}):
            item = {
                "data": {"key": "ITEM1", "itemType": "document", "title": "A Page", "version": 1},
                "version": 1,
            }
            self.mock_zotero_client.get_item_children.return_value = [
                {"data": {"key": "SNAP1", "contentType": "text/html", "title": "Snapshot"}, "version": 1},
            ]
            self.mock_zotero_client.get_attachment_file.return_value = b"<html>hi</html>"
            self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("hi", 1))
            self.mock_embedding_service.embed_batch.return_value = [[0.1] * 384]
            chunks = await self.processor._index_item(item, "1", "user")
        self.assertEqual(chunks, 1)
```

(This second test mirrors the shape of other `_index_item` success tests already in this file for mocking `extract_and_chunk`/`embed_batch` — check an existing passing test like `test_index_library_skip_non_pdf_items`'s neighbors for the exact mock-return shape your `mock_extractor`/`mock_embedding_service` expect, and adjust `_make_extraction_chunks`/`embed_batch` call shape to match if this fails for a reason unrelated to the Snapshot filter itself.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k "IsIndexableAttachment or index_item_skips_snapshot or index_item_indexes_snapshot" -v`
Expected: FAIL — `_is_indexable_attachment` doesn't exist yet; the two `_index_item` tests currently index the Snapshot attachment unconditionally (first test fails because `get_attachment_file` IS called).

- [ ] **Step 3: Implement**

In `backend/services/document_processor.py`, add the import (next to the other `backend.services` imports, ~line 33):

```python
from backend.services.admin_settings_store import read_admin_settings
```

Add this function right after `INDEXABLE_MIME_TYPES` (~line 91, before the `_GC_RSS_THRESHOLD_MB` comment):

```python
def _is_indexable_attachment(att_data: dict, index_snapshots_enabled: bool) -> bool:
    """Whether a Zotero attachment's `data` dict should be indexed.

    Snapshot attachments (Zotero's default title for a saved webpage, an
    HTML attachment) are excluded when the admin-controlled index_snapshots
    setting is off — exact, case-sensitive title match, scoped to text/html
    attachments only (Zotero never auto-assigns that title to any other
    type, so e.g. a PDF titled "Snapshot" is never excluded), so a
    user-renamed snapshot is treated as a normal attachment. See
    backend/services/admin_settings_store.py and docs/superpowers/specs/
    2026-10-07-configurable-snapshot-indexing-design.md.
    """
    if att_data.get("contentType") not in INDEXABLE_MIME_TYPES:
        return False
    if not index_snapshots_enabled and att_data.get("contentType") == "text/html" and att_data.get("title") == "Snapshot":
        return False
    return True
```

In `_index_item` (~line 1011-1026), replace:

```python
        is_standalone_attachment = item["data"].get("itemType") == "attachment"
        if is_standalone_attachment:
            # A standalone attachment (no parentItem) IS the indexable unit — it has
            # no children to fetch and no abstract of its own.
            attachments = [item]
        else:
            attachments = await self.zotero_client.get_item_children(
                library_id=library_id,
                item_key=item_key,
                library_type=library_type
            )

        indexable_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
        ]
```

with:

```python
        index_snapshots_enabled = read_admin_settings(get_settings().data_path).get("index_snapshots", False)

        is_standalone_attachment = item["data"].get("itemType") == "attachment"
        if is_standalone_attachment:
            # A standalone attachment (no parentItem) IS the indexable unit — it has
            # no children to fetch and no abstract of its own.
            attachments = [item]
        else:
            attachments = await self.zotero_client.get_item_children(
                library_id=library_id,
                item_key=item_key,
                library_type=library_type
            )

        indexable_attachments = [
            att for att in attachments
            if _is_indexable_attachment(att.get("data", {}), index_snapshots_enabled)
        ]
```

In the per-attachment loop (~line 1032-1037), set `attachment_title` alongside `attachment_key`:

```python
            for attachment in indexable_attachments:
                attachment_key = attachment["data"]["key"]
                attachment_version = attachment.get("version", item_version)
                mime_type = attachment["data"].get("contentType", "application/pdf")
                doc_metadata.attachment_key = attachment_key
                doc_metadata.attachment_title = attachment["data"].get("title")
```

Finally, in the abstract-fallback branch (~line 1546 inside `_index_from_abstract`), reset the stale per-attachment title so an abstract-fallback chunk never inherits the title of a since-failed attachment attempt:

```python
        abstract_key = f"{doc_metadata.item_key}:abstract"
        meta = doc_metadata.model_copy(update={"attachment_key": abstract_key, "attachment_title": None})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k "IsIndexableAttachment or index_item_skips_snapshot or index_item_indexes_snapshot" -v`
Expected: PASS (7 tests). If the "indexes_snapshot_when_setting_on" test fails on an unrelated mock-shape mismatch, inspect a neighboring already-passing `_index_item` success test in this file and match its exact `mock_extractor`/`mock_embedding_service` return shapes rather than changing the production code.

- [ ] **Step 5: Run the full document_processor test file to check for regressions**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests PASS (no regressions from the `indexable_attachments` filter change).

- [ ] **Step 6: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat(backend): exclude Snapshot attachments from _index_item when index_snapshots is off"
```

---

### Task 7: Wire the filter into `_split_indexable_and_catalog_only` (incremental sync classification)

**Files:**
- Modify: `backend/services/document_processor.py`
- Test: `backend/tests/test_document_processor.py`

- [ ] **Step 1: Write the failing test**

Add inside `class TestDocumentProcessor`:

```python
    async def test_split_indexable_and_catalog_only_excludes_snapshot_only_item_when_setting_off(self):
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": False}):
            item = {"data": {"key": "ITEM1", "itemType": "document", "title": "A Page", "abstractNote": ""}}
            children_by_parent = {
                "ITEM1": [{"data": {"contentType": "text/html", "title": "Snapshot"}}],
            }
            indexable, catalog_only = await self.processor._split_indexable_and_catalog_only(
                [item], "1", "user", children_by_parent=children_by_parent
            )
        self.assertEqual(indexable, [])
        self.assertEqual(catalog_only, [item])

    async def test_split_indexable_and_catalog_only_includes_snapshot_only_item_when_setting_on(self):
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": True}):
            item = {"data": {"key": "ITEM1", "itemType": "document", "title": "A Page", "abstractNote": ""}}
            children_by_parent = {
                "ITEM1": [{"data": {"contentType": "text/html", "title": "Snapshot"}}],
            }
            indexable, catalog_only = await self.processor._split_indexable_and_catalog_only(
                [item], "1", "user", children_by_parent=children_by_parent
            )
        self.assertEqual(indexable, [item])
        self.assertEqual(catalog_only, [])

    async def test_split_indexable_standalone_snapshot_attachment_excluded_when_setting_off(self):
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": False}):
            standalone = {"data": {"key": "SNAP1", "itemType": "attachment", "contentType": "text/html", "title": "Snapshot"}}
            indexable, catalog_only = await self.processor._split_indexable_and_catalog_only(
                [standalone], "1", "user", children_by_parent={}
            )
        self.assertEqual(indexable, [])
        self.assertEqual(catalog_only, [])  # standalone attachments never become catalog stubs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k split_indexable_and_catalog_only -v`
Expected: FAIL — all three currently ignore the title/flag and classify purely by `contentType`.

- [ ] **Step 3: Implement**

In `backend/services/document_processor.py`'s `_split_indexable_and_catalog_only` (~line 1632-1693), add the settings read once at the top of the method, and replace both inline `contentType in INDEXABLE_MIME_TYPES` checks:

```python
    async def _split_indexable_and_catalog_only(
        self,
        items: list[dict],
        library_id: str,
        library_type: str,
        children_by_parent: Optional[dict[str, list[dict]]] = None,
    ) -> tuple[list[dict], list[dict]]:
        """Split items into (indexable, catalog-only).
        ...
        """
        min_words = get_settings().min_abstract_words
        index_snapshots_enabled = read_admin_settings(get_settings().data_path).get("index_snapshots", False)
        items_with_content = []
        catalog_only_items = []

        for item in items:
            # Skip if not a regular item (skip notes; attachments handled below)
            if "data" not in item:
                continue

            item_type = item["data"].get("itemType")
            if item_type == "note":
                continue
            if item_type == "attachment":
                # A standalone attachment (no parentItem) is itself the indexable
                # unit — see the matching case in _index_library_full's filter.
                if not item["data"].get("parentItem") \
                        and _is_indexable_attachment(item["data"], index_snapshots_enabled):
                    items_with_content.append(item)
                continue

            # Check if item has any indexable attachments
            item_key = item["data"]["key"]
            if children_by_parent is not None:
                attachments = children_by_parent.get(item_key, [])
            else:
                attachments = await self.zotero_client.get_item_children(
                    library_id=library_id,
                    item_key=item_key,
                    library_type=library_type
                )

            has_indexable = any(
                _is_indexable_attachment(att.get("data", {}), index_snapshots_enabled)
                for att in attachments
            )

            if has_indexable:
                items_with_content.append(item)
                continue

            # Fall back: include items with a substantial abstractNote
            abstract = item["data"].get("abstractNote", "")
            if abstract and len(abstract.split()) >= min_words:
                items_with_content.append(item)
            else:
                catalog_only_items.append(item)

        return items_with_content, catalog_only_items
```

(Leave the rest of the method's docstring as-is; only the two filter expressions and the new settings-read line changed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k split_indexable_and_catalog_only -v`
Expected: PASS (3 new tests, plus any pre-existing tests for this method still pass — run the whole class to confirm).

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat(backend): apply Snapshot-attachment filter in incremental-sync classification"
```

---

### Task 8: Wire the filter into `_try_metadata_only_update` (fixes a real cross-contamination bug)

**Files:**
- Modify: `backend/services/document_processor.py`
- Test: `backend/tests/test_document_processor.py`

**Why this matters:** `_try_metadata_only_update` compares the set of currently-indexable attachment keys against the set of attachment keys that actually have stored chunks, to decide if an item's version bump was metadata-only. If a Snapshot attachment is excluded from indexing (so it's never in the stored set) but this comparison still counts it as "currently indexable," the sets will always mismatch for any item that has a co-located Snapshot alongside real content — forcing a full, unnecessary re-extraction of that item's other (unrelated, unchanged) attachments on every single version bump.

- [ ] **Step 1: Write the failing test**

Add inside `class TestDocumentProcessor`:

```python
    async def test_metadata_only_update_ignores_a_cooccurring_snapshot_attachment(self):
        """A PDF + a Snapshot on the same item: the Snapshot must not be counted
        as 'currently indexable' when deciding if the PDF's version is unchanged,
        or metadata-only updates break for every item that also has a snapshot."""
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": False}):
            item = {
                "data": {
                    "key": "ITEM1", "itemType": "document", "title": "New Title",
                    "abstractNote": "", "dateModified": "2026-01-02T00:00:00Z",
                },
                "version": 5,
            }
            self.mock_vector_store.get_item_chunks.return_value = [
                {"payload": {"has_content": True, "attachment_key": "PDF1", "attachment_version": 3, "content_hash": "h1"}},
            ]
            self.mock_zotero_client.get_item_children.return_value = [
                {"data": {"key": "PDF1", "contentType": "application/pdf", "title": "paper.pdf"}, "version": 3},
                {"data": {"key": "SNAP1", "contentType": "text/html", "title": "Snapshot"}, "version": 1},
            ]
            result = await self.processor._try_metadata_only_update(item, "1", "user")
        self.assertTrue(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k metadata_only_update_ignores_a_cooccurring_snapshot -v`
Expected: FAIL — `result` is `False` today, because `current_indexable` includes `SNAP1` (unfiltered `contentType` check) while `stored_versions` (built from actual chunks) only has `PDF1`, so `set(stored_versions) != set(current_indexable)` is true and the method falls through to a full reindex instead of the cheap metadata patch.

- [ ] **Step 3: Implement**

In `backend/services/document_processor.py`'s `_try_metadata_only_update` (~line 1753-1851), add the settings read near the top of the method (right after `item_key = item["data"]["key"]`), and replace both inline `contentType in INDEXABLE_MIME_TYPES` checks:

```python
        item_key = item["data"]["key"]
        index_snapshots_enabled = read_admin_settings(get_settings().data_path).get("index_snapshots", False)
```

Then (in the abstract-fallback branch, ~line 1804-1812):

```python
            current_attachments = await self.zotero_client.get_item_children(
                library_id=library_id, item_key=item_key, library_type=library_type
            )
            has_indexable_attachment = any(
                _is_indexable_attachment(att.get("data", {}), index_snapshots_enabled)
                for att in current_attachments
            )
            if has_indexable_attachment:
                return False
```

And (in the normal branch, ~line 1825-1832):

```python
            current_attachments = await self.zotero_client.get_item_children(
                library_id=library_id, item_key=item_key, library_type=library_type
            )
            current_indexable = {
                att["data"]["key"]: att.get("version", 0)
                for att in current_attachments
                if _is_indexable_attachment(att.get("data", {}), index_snapshots_enabled)
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k metadata_only_update -v`
Expected: PASS (new test plus all pre-existing `_try_metadata_only_update` tests).

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "fix(backend): exclude Snapshot attachments from metadata-only-update diffing"
```

---

### Task 9: Wire the filter into the full-scan streaming pre-filter

**Files:**
- Modify: `backend/services/document_processor.py`
- Test: `backend/tests/test_document_processor.py`

- [ ] **Step 1: Write the failing test**

Add inside `class TestDocumentProcessor`:

```python
    async def test_full_sync_treats_snapshot_only_item_as_catalog_only_when_setting_off(self):
        with patch("backend.services.document_processor.read_admin_settings", return_value={"index_snapshots": False}):
            page_item = {
                "data": {"key": "ITEM1", "itemType": "webpage", "title": "A Page", "abstractNote": "", "dateModified": "2026-01-01T00:00:00Z"},
                "version": 1,
            }
            snap_attachment = {
                "data": {"key": "SNAP1", "itemType": "attachment", "contentType": "text/html", "title": "Snapshot", "parentItem": "ITEM1"},
                "version": 1,
            }
            self.mock_zotero_client.get_library_items_since.return_value = [page_item, snap_attachment]
            self.mock_vector_store.get_all_indexed_item_versions.return_value = {}

            result = await self.processor.index_library("1", mode="full")

        # The item has no indexable attachment (Snapshot excluded) and no
        # abstract, so it must become a catalog-only stub, not a failed/zero-
        # chunk "indexed" item, and get_attachment_file must never be called
        # for the excluded Snapshot.
        self.mock_zotero_client.get_attachment_file.assert_not_called()
        self.assertEqual(result["items_processed"], 0)
```

(If `index_library`'s exact result-dict keys differ from `items_processed`/the full-scan entrypoint signature differs from `mode="full"`, check an existing full-sync test in this file — e.g. search for `_index_library_full` or `mode="full"` usage nearby — and match its exact call shape; the assertion that matters for this task is `get_attachment_file.assert_not_called()`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k full_sync_treats_snapshot_only_item -v`
Expected: FAIL — the streaming pre-filter currently only tracks `contentType` (not `title`) in its minimal `children_by_parent` map, so it can't exclude the Snapshot and will attempt to download/process it (`get_attachment_file` gets called).

- [ ] **Step 3: Implement**

In `backend/services/document_processor.py`'s `_index_library_full` (~line 636-719), the streaming pre-filter currently keeps only `contentType` per attachment. Update it to also keep `title`, and read the setting once at the top of the method:

Replace:

```python
        logger.info(f"Full sync for library {library_id}")
        # Reset per-run: this list is instance-level so _index_item can append to it
        # without threading a new parameter through every one of its callers.
        self._download_failures = []
        self._too_large_skips = []
```

with:

```python
        logger.info(f"Full sync for library {library_id}")
        # Reset per-run: this list is instance-level so _index_item can append to it
        # without threading a new parameter through every one of its callers.
        self._download_failures = []
        self._too_large_skips = []
        index_snapshots_enabled = read_admin_settings(get_settings().data_path).get("index_snapshots", False)
```

Replace:

```python
            children_by_parent: dict[str, list[dict]] = {}
            with os.fdopen(tmp_fd, "w") as f:
                for _item in items:
                    f.write(json.dumps(_item) + "\n")
                    _parent = _item.get("data", {}).get("parentItem")
                    if _parent and _item.get("data", {}).get("itemType") == "attachment":
                        # Keep only contentType — avoids referencing full item dicts
                        children_by_parent.setdefault(_parent, []).append(
                            {"data": {"contentType": _item["data"].get("contentType")}}
                        )
```

with:

```python
            children_by_parent: dict[str, list[dict]] = {}
            with os.fdopen(tmp_fd, "w") as f:
                for _item in items:
                    f.write(json.dumps(_item) + "\n")
                    _parent = _item.get("data", {}).get("parentItem")
                    if _parent and _item.get("data", {}).get("itemType") == "attachment":
                        # Keep only contentType + title — avoids referencing full item
                        # dicts, while still letting _is_indexable_attachment correctly
                        # exclude Snapshot-titled attachments below.
                        children_by_parent.setdefault(_parent, []).append(
                            {"data": {
                                "contentType": _item["data"].get("contentType"),
                                "title": _item["data"].get("title"),
                            }}
                        )
```

Replace:

```python
                    if _item_type == "attachment":
                        # A standalone attachment (no parentItem) is itself the indexable
                        # unit — e.g. a PDF dropped straight into a collection with no
                        # bibliographic parent. Attachments that DO have a parent are
                        # handled below via children_by_parent, keyed on the parent.
                        if not _item["data"].get("parentItem") \
                                and _item["data"].get("contentType") in INDEXABLE_MIME_TYPES:
                            items_with_attachments.append(_item)
                        continue
                    _key = _item["data"]["key"]
                    _atts = children_by_parent.get(_key, [])
                    _has_indexable = any(
                        a.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
                        for a in _atts
                    )
```

with:

```python
                    if _item_type == "attachment":
                        # A standalone attachment (no parentItem) is itself the indexable
                        # unit — e.g. a PDF dropped straight into a collection with no
                        # bibliographic parent. Attachments that DO have a parent are
                        # handled below via children_by_parent, keyed on the parent.
                        if not _item["data"].get("parentItem") \
                                and _is_indexable_attachment(_item["data"], index_snapshots_enabled):
                            items_with_attachments.append(_item)
                        continue
                    _key = _item["data"]["key"]
                    _atts = children_by_parent.get(_key, [])
                    _has_indexable = any(
                        _is_indexable_attachment(a.get("data", {}), index_snapshots_enabled)
                        for a in _atts
                    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k full_sync_treats_snapshot_only_item -v`
Expected: PASS.

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all PASS — pay particular attention to any other full-sync test that constructs `children_by_parent` fixtures directly (searching the file for `children_by_parent` literal dicts in test setup) and update them to the new `{"data": {"contentType": ..., "title": ...}}` shape if any exist and fail.

- [ ] **Step 5: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat(backend): apply Snapshot-attachment filter in full-sync streaming pre-filter"
```

---

### Task 10: Store `attachment_title` from the plugin's direct upload path

**Files:**
- Modify: `backend/api/document_upload.py`
- Test: `backend/tests/test_upload_diagnostics.py` (or wherever `_parse_upload_request` is already tested — search first)

- [ ] **Step 1: Find the existing test file and write the failing test**

Run: `grep -rn "_parse_upload_request" backend/tests/*.py` to find where this function is already tested, and add the new test in that same file (do not create a new file if one already covers it). If none is found, add to `backend/tests/test_upload_diagnostics.py`:

```python
    async def test_parse_upload_request_stores_attachment_title(self):
        from backend.api.document_upload import _parse_upload_request
        from fastapi import UploadFile
        import io, json

        metadata = json.dumps({
            "library_id": "1", "item_key": "ITEM1", "attachment_key": "ATT1",
            "attachment_title": "Snapshot", "title": "Parent Title",
        })
        upload_file = UploadFile(filename="page.html", file=io.BytesIO(b"<html></html>"))
        result = await _parse_upload_request(upload_file, metadata, identity=None)
        doc_metadata = result[1]  # (meta_dict, doc_metadata, library_id, ...)
        self.assertEqual(doc_metadata.attachment_title, "Snapshot")

    async def test_parse_upload_request_defaults_attachment_title_to_none(self):
        from backend.api.document_upload import _parse_upload_request
        from fastapi import UploadFile
        import io, json

        metadata = json.dumps({"library_id": "1", "item_key": "ITEM1", "attachment_key": "ATT1"})
        upload_file = UploadFile(filename="paper.pdf", file=io.BytesIO(b"%PDF-1.4"))
        result = await _parse_upload_request(upload_file, metadata, identity=None)
        doc_metadata = result[1]
        self.assertIsNone(doc_metadata.attachment_title)
```

Adjust the class these methods are added to and the exact tuple-unpack of `result` to match whatever existing tests in that file already do for `_parse_upload_request` (match the established pattern exactly rather than guessing at `UploadFile` construction details if an existing test already shows the working incantation).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_upload_diagnostics.py -k attachment_title -v`
Expected: FAIL — `doc_metadata.attachment_title` is `None` in the first test (should be `"Snapshot"`).

- [ ] **Step 3: Implement**

In `backend/api/document_upload.py`'s `_parse_upload_request` (~line 1286-1294), add one field to the `DocumentMetadata(...)` construction:

```python
    doc_metadata = DocumentMetadata(
        library_id=library_id,
        item_key=item_key,
        attachment_key=attachment_key,
        attachment_title=meta_dict.get("attachment_title"),
        title=meta_dict.get("title", "Untitled"),
        authors=meta_dict.get("authors", []),
        year=meta_dict.get("year"),
        item_type=meta_dict.get("item_type"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_upload_diagnostics.py -k attachment_title -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full backend test suite to confirm no regressions from Tasks 1-10**

Run: `uv run pytest backend/tests/ -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/api/document_upload.py backend/tests/test_upload_diagnostics.py
git commit -m "feat(backend): accept attachment_title in the plugin's direct-upload metadata"
```

---

## Plugin tasks (sequential; depend on the API shape above, already fixed by the spec, not on the backend tasks being merged)

### Task 11: `getIndexSnapshotsEnabled()` cached fetch (zotero-rag.js)

**Files:**
- Modify: `plugin/src/zotero-rag.js`
- Test: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/zotero-rag.test.js`, near the other cached-fetch tests (search for `getAutoIndexedLibraryIds` tests and place these alongside them):

```js
test('getIndexSnapshotsEnabled fetches and caches the value, defaulting to false on error', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	let fetchCalls = 0;
	const fetchStub = async () => {
		fetchCalls++;
		return { ok: true, json: async () => ({ index_snapshots: true }) };
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { fetch: fetchStub });
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';

	const first = await plugin.getIndexSnapshotsEnabled();
	const second = await plugin.getIndexSnapshotsEnabled();

	assert.strictEqual(first, true);
	assert.strictEqual(second, true);
	assert.strictEqual(fetchCalls, 1); // cached, no second network call
});

test('getIndexSnapshotsEnabled defaults to false (exclude) when the fetch fails', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const fetchStub = async () => { throw new Error('network down'); };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { fetch: fetchStub });
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';
	plugin.log = () => {};

	const enabled = await plugin.getIndexSnapshotsEnabled();

	assert.strictEqual(enabled, false);
});

test('getIndexSnapshotsEnabled defaults to false when the backend returns a non-ok response', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const fetchStub = async () => ({ ok: false, status: 500 });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { fetch: fetchStub });
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';

	const enabled = await plugin.getIndexSnapshotsEnabled();

	assert.strictEqual(enabled, false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `plugin.getIndexSnapshotsEnabled is not a function`

- [ ] **Step 3: Implement**

In `plugin/src/zotero-rag.js`, add this method right after `getAutoIndexedLibraryIds()` (same class):

```js
	/**
	 * Fetch (and cache for 5 minutes) whether the server admin has enabled
	 * indexing of Zotero webpage-snapshot attachments (title exactly
	 * "Snapshot"). Defaults to false (exclude) on any fetch error — the same
	 * safe-default convention as getAutoIndexedLibraryIds().
	 * @returns {Promise<boolean>}
	 */
	async getIndexSnapshotsEnabled() {
		const TTL_MS = 5 * 60 * 1000;
		const now = Date.now();
		if (this._indexSnapshotsEnabled !== undefined && (now - this._indexSnapshotsEnabledFetchedAt) < TTL_MS) {
			return this._indexSnapshotsEnabled;
		}
		let enabled = false;
		try {
			const resp = await fetch(`${this.backendURL}/api/admin/settings`, {
				headers: this.getAuthHeaders(),
			});
			if (resp.ok) {
				const data = /** @type {any} */ (await resp.json());
				enabled = data.index_snapshots === true;
			}
		} catch (e) {
			this.log(`[ZoteroRAG] getIndexSnapshotsEnabled failed: ${e instanceof Error ? e.message : String(e)}`);
		}
		this._indexSnapshotsEnabled = enabled;
		this._indexSnapshotsEnabledFetchedAt = now;
		return enabled;
	}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS (3 new tests, no regressions).

- [ ] **Step 5: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat(plugin): add cached getIndexSnapshotsEnabled() fetch"
```

---

### Task 12: Filter Snapshot attachments out of `_getUnavailableAttachments` and its four sub-scans

**Files:**
- Modify: `plugin/src/zotero-rag.js`
- Test: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/zotero-rag.test.js`:

```js
test('_getParseErrorAttachments excludes a Snapshot-titled attachment when indexSnapshotsEnabled is false, and prunes it from the store', async () => {
	const fakeAttachment = {
		deleted: false, parentItemID: null, key: 'SNAP1',
		getCreators: () => [], getField: (f) => (f === 'title' ? 'Snapshot' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ SNAP1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	await plugin.storeParseErrorItems('u1', ['SNAP1']);

	const results = await plugin._getParseErrorAttachments(1, false);
	assert.deepStrictEqual([...results], []);

	// Pruned from the persisted store too, same as a deleted item.
	const resultsAgain = await plugin._getParseErrorAttachments(1, true);
	assert.deepStrictEqual([...resultsAgain], []);
});

test('_getParseErrorAttachments includes a Snapshot-titled attachment when indexSnapshotsEnabled is true', async () => {
	const fakeAttachment = {
		deleted: false, parentItemID: null, key: 'SNAP1',
		getCreators: () => [], getField: (f) => (f === 'title' ? 'Snapshot' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ SNAP1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	await plugin.storeParseErrorItems('u1', ['SNAP1']);

	const results = await plugin._getParseErrorAttachments(1, true);
	assert.strictEqual(results.length, 1);
});

test('_getSkippedServerAttachments excludes a Snapshot-titled attachment when indexSnapshotsEnabled is false', async () => {
	const fakeAttachment = {
		deleted: false, parentItemID: null, key: 'SNAP1',
		getCreators: () => [], getField: (f) => (f === 'title' ? 'Snapshot' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ SNAP1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	const filePath = plugin._skippedServerFilePath(1);
	await ioUtils.writeUTF8(filePath, JSON.stringify([{ key: 'SNAP1', reason: 'skipped_empty' }]));

	const results = await plugin._getSkippedServerAttachments(1, false);
	assert.deepStrictEqual([...results], []);
});

test('_getTooLargeAttachments excludes a Snapshot-titled attachment when indexSnapshotsEnabled is false', async () => {
	const fakeAttachment = {
		deleted: false, parentItemID: null, key: 'SNAP1',
		getCreators: () => [], getField: (f) => (f === 'title' ? 'Snapshot' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ SNAP1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	await plugin.storeTooLargeItems('u1', [{ key: 'SNAP1', detail: 'too big' }]);

	const results = await plugin._getTooLargeAttachments(1, false);
	assert.deepStrictEqual([...results], []);
});

test('_getDownloadFailedAttachments excludes a Snapshot-titled attachment when indexSnapshotsEnabled is false', async () => {
	const fakeAttachment = {
		deleted: false, parentItemID: null, key: 'SNAP1',
		attachmentLinkMode: 0, isImportedAttachment: () => true,
		getCreators: () => [], getField: (f) => (f === 'title' ? 'Snapshot' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ SNAP1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	await plugin.storeDownloadFailedItems('u1', ['SNAP1']);

	const results = await plugin._getDownloadFailedAttachments(1, false);
	assert.deepStrictEqual([...results], []);
});

test('_getUnavailableAttachments fetches indexSnapshotsEnabled once and threads it into every sub-scan', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.DB = { columnQueryAsync: async () => [] };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	plugin.getIndexSnapshotsEnabled = async () => false;
	/** @type {Array<any[]>} */
	const calls = [];
	plugin._getParseErrorAttachments = async (...args) => { calls.push(['parseError', ...args]); return []; };
	plugin._getSkippedServerAttachments = async (...args) => { calls.push(['skippedServer', ...args]); return []; };
	plugin._getTooLargeAttachments = async (...args) => { calls.push(['tooLarge', ...args]); return []; };
	plugin._getDownloadFailedAttachments = async (...args) => { calls.push(['downloadFailed', ...args]); return []; };

	await plugin._getUnavailableAttachments(1, { includeDownloadFailed: true });

	assert.deepStrictEqual(calls, [
		['parseError', 1, false],
		['skippedServer', 1, false],
		['tooLarge', 1, false],
		['downloadFailed', 1, false],
	]);
});

test('_getUnavailableAttachments main scan excludes a Snapshot-titled attachment with a missing file when indexSnapshotsEnabled is false', async () => {
	const fakeAttachment = {
		deleted: false, parentItemID: 101, key: 'SNAP1', attachmentLinkMode: 0,
		fileExists: async () => false,
		getField: (f) => (f === 'title' ? 'Snapshot' : ''),
	};
	const fakeParent = { getCreators: () => [], getField: () => '', key: 'PARENT1' };
	const { zotero, ioUtils, pathUtils } = makeStubs({ SNAP1: fakeAttachment, __parent_101: fakeParent });
	zotero.DB = { columnQueryAsync: async () => [1] };
	zotero.Items.getAsync = async (ids) => {
		if (Array.isArray(ids)) return ids.map(id => ({ 1: fakeAttachment }[id])).filter(Boolean);
		return { 101: fakeParent }[ids] || null;
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	plugin.getIndexSnapshotsEnabled = async () => false;

	const results = await plugin._getUnavailableAttachments(1);

	assert.deepStrictEqual([...results], []);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — none of the sub-scans currently accept a second parameter or filter by title; the Snapshot-titled fixtures in every new test currently appear in `results`.

- [ ] **Step 3: Implement**

In `plugin/src/zotero-rag.js`:

In `_getParseErrorAttachments` (~line 2642-2694), add the parameter and the check right after the deleted-guard:

```js
	async _getParseErrorAttachments(libraryID, indexSnapshotsEnabled = false) {
		...
		for (const key of keys) {
			// @ts-ignore
			const attachment = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, key);
			if (!attachment || attachment.deleted) continue;
			if (!indexSnapshotsEnabled && (attachment.getField ? attachment.getField('title') : '') === 'Snapshot') continue;
			validKeys.push(key);
			...
```

In `_getSkippedServerAttachments` (~line 2582-2634), same pattern:

```js
	async _getSkippedServerAttachments(libraryID, indexSnapshotsEnabled = false) {
		...
		for (const entry of entries) {
			// @ts-ignore
			const attachment = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, entry.key);
			if (!attachment || attachment.deleted) continue;
			if (!indexSnapshotsEnabled && (attachment.getField ? attachment.getField('title') : '') === 'Snapshot') continue;
			validEntries.push(entry);
			...
```

In `_getTooLargeAttachments` (~line 2840-2891), same pattern:

```js
	async _getTooLargeAttachments(libraryID, indexSnapshotsEnabled = false) {
		...
		for (const entry of entries) {
			// @ts-ignore
			const attachment = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, entry.key);
			if (!attachment || attachment.deleted) continue;
			if (!indexSnapshotsEnabled && (attachment.getField ? attachment.getField('title') : '') === 'Snapshot') continue;
			validEntries.push(entry);
			...
```

In `_getDownloadFailedAttachments` (~line 2951-3016), add the parameter and the check right after the existing `LINK_MODE_LINKED_URL` exclusion (same "exclude from validKeys too" precedent already documented there):

```js
	async _getDownloadFailedAttachments(libraryID, indexSnapshotsEnabled = false) {
		...
		for (const key of keys) {
			// @ts-ignore
			const attachment = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, key);
			if (!attachment || attachment.deleted) continue;
			// @ts-ignore - Zotero.Attachments is a global at runtime
			if (attachment.attachmentLinkMode === Zotero.Attachments.LINK_MODE_LINKED_URL) continue;
			if (!indexSnapshotsEnabled && (attachment.getField ? attachment.getField('title') : '') === 'Snapshot') continue;
			validKeys.push(key);
			...
```

In `_getUnavailableAttachments` (~line 3028-3116): fetch the flag once, use it in the main SQL loop, and thread it into every sub-scan call:

```js
	async _getUnavailableAttachments(libraryID, { includeDownloadFailed = false } = {}) {
		const indexSnapshotsEnabled = await this.getIndexSnapshotsEnabled();
		const sql = `
			SELECT ia.itemID FROM itemAttachments ia
			JOIN items i ON i.itemID = ia.itemID
			WHERE i.libraryID = ?
			AND ia.linkMode IN (0, 1, 2)
			AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
			AND (ia.parentItemID IS NULL OR ia.parentItemID NOT IN (SELECT itemID FROM deletedItems))
		`;
		const ids = /** @type {number[]} */ (await Zotero.DB.columnQueryAsync(sql, [libraryID]));
		const attachments = /** @type {any[]} */ (ids && ids.length > 0 ? await Zotero.Items.getAsync(ids) : []);
		/** @type {Array<UnavailableAttachmentInfo>} */
		const result = [];
		for (const attachment of attachments) {
			let exists = false;
			try {
				exists = await attachment.fileExists();
			} catch (_) {
				exists = false;
			}
			if (exists) continue;
			if (!indexSnapshotsEnabled && (attachment.getField ? attachment.getField('title') : '') === 'Snapshot') continue;
			if (!attachment.parentItemID) continue;
			...
```

(leave the rest of the main-loop body unchanged), then update the four sub-scan call sites further down in the same method:

```js
		const parseErrorItems = await this._getParseErrorAttachments(libraryID, indexSnapshotsEnabled);
		...
		const skippedServerItems = await this._getSkippedServerAttachments(libraryID, indexSnapshotsEnabled);
		...
		const tooLargeItems = await this._getTooLargeAttachments(libraryID, indexSnapshotsEnabled);
		...
		if (includeDownloadFailed) {
			const downloadFailedItems = await this._getDownloadFailedAttachments(libraryID, indexSnapshotsEnabled);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS, no regressions among the many pre-existing tests for these five methods (they call with 1 argument, which now defaults `indexSnapshotsEnabled` to `false` — confirm none of those pre-existing fixtures happen to use the literal title `"Snapshot"`, which they don't, per the earlier codebase check).

- [ ] **Step 5: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat(plugin): exclude Snapshot attachments from the Fix Unavailable scan"
```

---

### Task 13: Send `attachment_title` in the plugin's upload metadata

**Files:**
- Modify: `plugin/src/remote_indexer.js`
- Test: `plugin/test/remote_indexer.test.js`

- [ ] **Step 1: Write the failing test**

Add to `plugin/test/remote_indexer.test.js`, near the other `_uploadAttachment`/metadata tests (search for `makeUploader` usage and add alongside):

```js
test('_uploadAttachmentInner includes the attachment\'s own title (not the parent\'s) as attachment_title in the upload metadata', async () => {
	const { RemoteIndexer, bodies } = makeUploader({ status: 'done', chunks_added: 1 });
	const parent = { getField: (f) => (f === 'title' ? 'Parent Title' : ''), loadAllData: async () => {} };
	const zoteroItem = { getField: (f) => (f === 'title' ? 'Snapshot' : ''), getFilePathAsync: async () => '/fake/path.html' };
	await RemoteIndexer._uploadAttachment({
		att: {
			item_key: 'ITEM1', attachment_key: 'SNAP1', mime_type: 'text/html',
			item_version: 1, attachment_version: 1, zoteroItem, parentItem: parent, filePath: '/fake/path.html',
		},
		libraryId: '1', libraryType: 'user', backendURL: 'http://backend',
		getAuthHeaders: () => ({}), log: () => {},
	});
	const sentMetadata = JSON.parse(bodies[0].get('metadata'));
	assert.strictEqual(sentMetadata.attachment_title, 'Snapshot');
	assert.strictEqual(sentMetadata.title, 'Parent Title');
});
```

(If `makeUploader`'s returned shape or `bodies[0]` access differs from this — e.g. it might expose raw `FormData` needing `.get('metadata')` differently, or the helper returns `{ RemoteIndexer, bodies, urls }` with a different bodies format — check `makeUploader`'s actual implementation further down in the file, already partially read during planning at line ~86-90, and adjust the assertion's access pattern to match exactly rather than guessing. The IOUtils/Blob stubs this test needs must also match whatever `makeUploader` already sets up for its existing passing tests — extend that same stub object, don't create a parallel one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: FAIL — `sentMetadata.attachment_title` is `undefined`.

- [ ] **Step 3: Implement**

In `plugin/src/remote_indexer.js`'s `_uploadAttachmentInner` (~line 858-874), add one field to the `metadata` object:

```js
		const parent = att.parentItem || att.zoteroItem;
		if (parent && parent.loadAllData) await parent.loadAllData();
		const metadata = {
			library_id: libraryId,
			library_type: libraryType,
			item_key: att.item_key,
			attachment_key: att.attachment_key,
			mime_type: att.mime_type,
			item_version: att.item_version,
			attachment_version: att.attachment_version,
			title: parent.getField ? (parent.getField('title') || 'Untitled') : 'Untitled',
			attachment_title: att.zoteroItem && att.zoteroItem.getField ? (att.zoteroItem.getField('title') || null) : null,
			authors: Zotero.ZoteroRAG._extractAuthors(parent),
			year: Zotero.ZoteroRAG._extractYear(parent),
			item_type: parent.itemType || null,
			zotero_modified: parent.dateModified || new Date().toISOString(),
			user_id: userId ?? null,
		};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: PASS, no regressions in other `_uploadAttachment`/`_uploadAttachmentInner` tests.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/remote_indexer.js plugin/test/remote_indexer.test.js
git commit -m "feat(plugin): send the attachment's own title as attachment_title on upload"
```

---

### Task 14: Exclude Snapshot attachments from `_collectAttachments` / `indexLibrary`

**Files:**
- Modify: `plugin/src/remote_indexer.js`, `plugin/src/dialog.js`
- Test: `plugin/test/remote_indexer.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/remote_indexer.test.js`:

```js
test('_collectAttachments excludes a Snapshot-titled text/html attachment when indexSnapshotsEnabled is false', async () => {
	const snapshotItem = {
		isAttachment: () => true, attachmentContentType: 'text/html', attachmentLinkMode: 1,
		getField: (f) => (f === 'title' ? 'Snapshot' : ''), key: 'SNAP1', parentItemID: null,
		getFilePathAsync: async () => '/fake/snap.html',
	};
	const zotero = {
		Groups: { get: () => ({ libraryID: 1 }) },
		Libraries: { userLibraryID: 1 },
		Search: function () { return { libraryID: null, addCondition() {}, async search() { return [1]; } }; },
		Items: { getAsync: async () => [snapshotItem] },
	};
	const RemoteIndexer = loadRemoteIndexer(zotero);

	const { attachments } = await RemoteIndexer._collectAttachments('123', 'group', () => {}, undefined, false);

	assert.deepStrictEqual(attachments, []);
});

test('_collectAttachments includes a Snapshot-titled text/html attachment when indexSnapshotsEnabled is true', async () => {
	const snapshotItem = {
		isAttachment: () => true, attachmentContentType: 'text/html', attachmentLinkMode: 1,
		getField: (f) => (f === 'title' ? 'Snapshot' : ''), key: 'SNAP1', parentItemID: null,
		getFilePathAsync: async () => '/fake/snap.html',
	};
	const zotero = {
		Groups: { get: () => ({ libraryID: 1 }) },
		Libraries: { userLibraryID: 1 },
		Search: function () { return { libraryID: null, addCondition() {}, async search() { return [1]; } }; },
		Items: { getAsync: async () => [snapshotItem] },
	};
	const RemoteIndexer = loadRemoteIndexer(zotero);

	const { attachments } = await RemoteIndexer._collectAttachments('123', 'group', () => {}, undefined, true);

	assert.strictEqual(attachments.length, 1);
});

test('_collectAttachments defaults indexSnapshotsEnabled to false when the argument is omitted', async () => {
	const snapshotItem = {
		isAttachment: () => true, attachmentContentType: 'text/html', attachmentLinkMode: 1,
		getField: (f) => (f === 'title' ? 'Snapshot' : ''), key: 'SNAP1', parentItemID: null,
		getFilePathAsync: async () => '/fake/snap.html',
	};
	const zotero = {
		Groups: { get: () => ({ libraryID: 1 }) },
		Libraries: { userLibraryID: 1 },
		Search: function () { return { libraryID: null, addCondition() {}, async search() { return [1]; } }; },
		Items: { getAsync: async () => [snapshotItem] },
	};
	const RemoteIndexer = loadRemoteIndexer(zotero);

	const { attachments } = await RemoteIndexer._collectAttachments('123', 'group', () => {});

	assert.deepStrictEqual(attachments, []);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: FAIL — `_collectAttachments` currently has no 5th parameter and includes the Snapshot item in all three cases (first and third tests fail; second passes already for the wrong reason, revisit after implementing).

- [ ] **Step 3: Implement**

In `plugin/src/remote_indexer.js`'s `_collectAttachments` (~line 532-608), add the parameter and the check right after the `linkMode === 3` exclusion:

```js
	async _collectAttachments(libraryId, libraryType, log, downloadedFilePaths, indexSnapshotsEnabled = false) {
		...
		for (const item of items) {
			if (!item.isAttachment()) continue;

			const mimeType = item.attachmentContentType || '';
			if (!INDEXABLE_TYPES.has(mimeType)) continue;

			// linkMode=3 = linked_url: web-only link, no local file possible — exclude from indexing.
			if ((item.attachmentLinkMode ?? 0) === 3) { linkedUrls++; continue; }

			if (!indexSnapshotsEnabled && (item.getField ? item.getField('title') : '') === 'Snapshot') continue;

			...
```

In `indexLibrary` (~line 105-113), accept a new opt and resolve it once before collecting:

```js
	async indexLibrary({ libraryId, libraryType, libraryName, backendURL, mode, userId, getAuthHeaders, log, onProgress, isCancelled, signal, downloadedFilePaths, downloadAttachment, onRateLimitUpdate, getIndexSnapshotsEnabled }) {
		log(`[RemoteIndexer] Starting remote indexing for library ${libraryId}`);

		const indexSnapshotsEnabled = getIndexSnapshotsEnabled ? await getIndexSnapshotsEnabled() : false;

		// 1. Collect all indexable attachments from the local Zotero database.
		//    Items without a local file are included (filePath: null) so check-indexed
		//    can decide whether they actually need indexing before we download them.
		onProgress({ percentage: 0, message: 'Scanning library', current: 0, total: 0 });
		const { attachments, linkedUrls } = await this._collectAttachments(libraryId, libraryType, log, downloadedFilePaths, indexSnapshotsEnabled);
```

Add the new opt to `indexLibrary`'s JSDoc (right after `onRateLimitUpdate`):

```js
	 * @param {function(Record<string,string>): void} [opts.onRateLimitUpdate] - Called with fresh rate-limit headers after each upload
	 * @param {function(): Promise<boolean>} [opts.getIndexSnapshotsEnabled] - Returns whether the admin has enabled indexing of Snapshot-titled webpage attachments; defaults to false (exclude) when omitted
```

In `plugin/src/dialog.js`, at the `RemoteIndexer.indexLibrary({...})` call site (~line 1843-1850), add the new opt:

```js
				const indexResult = await RemoteIndexer.indexLibrary({
					libraryId,
					libraryType,
					libraryName,
					backendURL,
					mode,
					userId: this.plugin.getCurrentZoteroUserId(),
					getAuthHeaders: (extra) => plugin.getAuthHeaders(extra),
					getIndexSnapshotsEnabled: () => plugin.getIndexSnapshotsEnabled(),
					log: (msg) => plugin.log(msg),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: PASS (3 new tests, no regressions — in particular re-check `'_collectAttachments excludes trashed items from the search'`, which calls with only 3 args, so `downloadedFilePaths` is `undefined` and `indexSnapshotsEnabled` defaults to `false`, unaffected by this change since that test has no items at all).

- [ ] **Step 5: Commit**

```bash
git add plugin/src/remote_indexer.js plugin/src/dialog.js plugin/test/remote_indexer.test.js
git commit -m "feat(plugin): exclude Snapshot attachments from the direct indexLibrary upload path"
```

---

### Task 15: Admin UI — checkbox, standalone purge button, and the two-step confirm flow

**Files:**
- Modify: `plugin/src/autoindex-status.xhtml`, `plugin/src/autoindex-status.js`
- Test: Create `plugin/test/autoindex-status.test.js`

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/autoindex-status.test.js`, following the exact vm-context harness pattern used in `plugin/test/fix-unavailable.test.js`:

```js
// Tests for plugin/src/autoindex-status.js's admin-only Snapshot-indexing controls.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'autoindex-status.js');

/**
 * @param {Record<string, any>} elements - map of element id -> fake element object
 * @returns {any} a fresh ZoteroRAGAutoIndexStatus object
 */
function loadDialog(elements = {}) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {
		window: { confirm: () => true, addEventListener: () => {} },
		document: {
			getElementById: (id) => elements[id] || { addEventListener: () => {}, style: {} },
		},
		console,
	};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'autoindex-status.js' });
	return context.ZoteroRAGAutoIndexStatus;
}

function makeElement() {
	return { style: {}, addEventListener: () => {}, checked: false, disabled: false, textContent: '' };
}

test('toggleIndexSnapshots PUTs true with no confirmation when checking the box', async () => {
	const toggle = makeElement();
	toggle.checked = true;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = {
		backendURL: 'http://backend',
		getAuthHeaders: () => ({}),
	};
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: true }) }; };
	global.window = { confirm: () => { throw new Error('must not be called'); } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(calls.length, 1);
	assert.strictEqual(calls[0].url, 'http://backend/api/admin/settings');
	assert.strictEqual(calls[0].opts.method, 'PUT');
	assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { index_snapshots: true });
});

test('toggleIndexSnapshots unchecking runs the two-step confirm and purges only on double-yes', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: false, deleted_chunks: 3, deleted_attachments: 1 }) }; };
	let confirmCallCount = 0;
	global.window = { confirm: () => { confirmCallCount++; return true; } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(confirmCallCount, 2);
	assert.strictEqual(calls.length, 2); // PUT, then purge
	assert.strictEqual(calls[0].opts.method, 'PUT');
	assert.strictEqual(calls[1].url, 'http://backend/api/admin/settings/purge-snapshots');
	assert.strictEqual(calls[1].opts.method, 'POST');
});

test('toggleIndexSnapshots unchecking does not purge when the first confirm is declined', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: false }) }; };
	global.window = { confirm: () => false };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(calls.length, 1); // PUT only, the flag still gets turned off
	assert.strictEqual(calls[0].opts.method, 'PUT');
});

test('toggleIndexSnapshots unchecking does not purge when only the second confirm is declined', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: false }) }; };
	let confirmCallCount = 0;
	global.window = { confirm: () => { confirmCallCount++; return confirmCallCount === 1; } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(confirmCallCount, 2);
	assert.strictEqual(calls.length, 1); // PUT only, no purge call
});

test('purgeSnapshotsNow runs the same two-step confirm independent of checkbox state and reports the result', async () => {
	const banner = makeElement();
	const dialog = loadDialog({ 'run-banner': banner });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ deleted_chunks: 7, deleted_attachments: 2 }) }; };
	global.window = { confirm: () => true };

	await dialog.purgeSnapshotsNow();

	assert.strictEqual(calls.length, 1);
	assert.strictEqual(calls[0].url, 'http://backend/api/admin/settings/purge-snapshots');
	assert.strictEqual(calls[0].opts.method, 'POST');
	assert.match(banner.textContent, /7/);
	assert.match(banner.textContent, /2/);
});

test('purgeSnapshotsNow makes no network call when either confirm is declined', async () => {
	const dialog = loadDialog({ 'run-banner': makeElement() });
	let fetchCalled = false;
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async () => { fetchCalled = true; return { ok: true, json: async () => ({}) }; };
	global.window = { confirm: () => false };

	await dialog.purgeSnapshotsNow();

	assert.strictEqual(fetchCalled, false);
});
```

(This test file uses `global.fetch`/`global.window` rather than vm-context injection for `fetch`/`window.confirm`, matching how `fetch` is already a bare global reference inside `autoindex-status.js`'s existing methods — e.g. `pauseScheduler` calls bare `fetch(...)` — so stubbing the real Node global is simplest here; restore/delete `global.fetch` is not required between tests since each test reassigns it, but if the test run shows cross-test leakage, add `delete global.fetch; delete global.window;` at the end of each test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/autoindex-status.test.js`
Expected: FAIL — `dialog.toggleIndexSnapshots is not a function` / `dialog.purgeSnapshotsNow is not a function`.

- [ ] **Step 3: Implement**

In `plugin/src/autoindex-status.xhtml`, inside `#admin-controls` (after the existing `#admin-scope-toggle-label` block, ~line 67):

```html
    <label id="admin-index-snapshots-label" class="admin-scope-toggle">
      <input id="admin-index-snapshots-toggle" type="checkbox"/> Index Snapshots of webpages
    </label>
    <button id="admin-purge-snapshots-button" type="button" class="dialog-button">Delete indexed Snapshot entries…</button>
```

In `plugin/src/autoindex-status.js`:

Add to `init()` (~line 94-100, right after the `adminScopeToggle` wiring):

```js
		const adminIndexSnapshotsToggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-index-snapshots-toggle'));
		if (adminIndexSnapshotsToggle) {
			adminIndexSnapshotsToggle.addEventListener('change', () => this.toggleIndexSnapshots());
		}

		const adminPurgeSnapshotsButton = document.getElementById('admin-purge-snapshots-button');
		if (adminPurgeSnapshotsButton) {
			adminPurgeSnapshotsButton.addEventListener('click', () => this.purgeSnapshotsNow());
		}
```

Add to `updateAdminControlsVisibility(data)` (~line 213-229), fetching the live value whenever admin controls are shown (not using the plugin's cached `getIndexSnapshotsEnabled()`, since the dialog wants the current value, not a stale cache):

```js
	updateAdminControlsVisibility(data) {
		const block = document.getElementById('admin-controls');
		if (!block) return;
		const isAdmin = data.is_admin === true;
		block.style.display = isAdmin ? '' : 'none';
		if (!isAdmin) {
			this.adminScope = 'own';
			const toggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-scope-toggle'));
			if (toggle) toggle.checked = false;
			return;
		}

		const paused = data.scheduler?.paused === true;
		const pauseButton = document.getElementById('admin-pause-button');
		const resumeButton = document.getElementById('admin-resume-button');
		if (pauseButton) pauseButton.style.display = paused ? 'none' : '';
		if (resumeButton) resumeButton.style.display = paused ? '' : 'none';

		this.refreshIndexSnapshotsToggle();
	},

	/**
	 * Fetch the live index_snapshots value and reflect it in the checkbox,
	 * without going through the plugin's TTL-cached getIndexSnapshotsEnabled()
	 * — the admin dialog wants the current server value every poll.
	 * @returns {Promise<void>}
	 */
	async refreshIndexSnapshotsToggle() {
		if (!this.plugin) return;
		const toggle = /** @type {HTMLInputElement|null} */ (document.getElementById('admin-index-snapshots-toggle'));
		if (!toggle) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/admin/settings`, {
				headers: this.plugin.getAuthHeaders(),
			});
			if (response.ok) {
				const data = await response.json();
				toggle.checked = data.index_snapshots === true;
			}
		} catch (_) { /* leave checkbox at its last known state */ }
	},

	/**
	 * Checkbox change handler. Checking it simply enables the setting.
	 * Unchecking it disables the setting, then offers to purge
	 * already-indexed Snapshot content via the shared two-step confirm.
	 * @returns {Promise<void>}
	 */
	async toggleIndexSnapshots() {
		if (!this.plugin) return;
		const toggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-index-snapshots-toggle'));
		const enabled = toggle.checked;
		try {
			await fetch(`${this.plugin.backendURL}/api/admin/settings`, {
				method: 'PUT',
				headers: this.plugin.getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify({ index_snapshots: enabled }),
			});
		} catch (e) {
			this.renderBanner(`Error updating setting: ${e}`, 'crashed');
			return;
		}
		if (!enabled) {
			await this._confirmAndPurgeSnapshots();
		}
	},

	/**
	 * Standalone admin action: run the same two-step confirm and purge,
	 * independent of the checkbox's current value — covers cleaning up
	 * Snapshot content indexed before this feature existed, which never
	 * fires the checkbox's own uncheck-triggered flow.
	 * @returns {Promise<void>}
	 */
	async purgeSnapshotsNow() {
		await this._confirmAndPurgeSnapshots();
	},

	/**
	 * Two sequential confirms ("delete them?", then "this cannot be undone"),
	 * only calling the purge endpoint if both are accepted.
	 * @returns {Promise<void>}
	 */
	async _confirmAndPurgeSnapshots() {
		if (!this.plugin) return;
		if (!window.confirm('Delete all already-indexed Snapshot entries from the index now?')) return;
		if (!window.confirm('This cannot be undone. Continue?')) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/admin/settings/purge-snapshots`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not purge Snapshot entries (HTTP ${response.status}).`, 'crashed');
				return;
			}
			const data = await response.json();
			this.renderBanner(`Deleted ${data.deleted_chunks} chunk(s) across ${data.deleted_attachments} attachment(s).`, 'idle');
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},
```

Also extend the `AutoIndexStatusResponse` typedef at the top of the file with the new field used by `refreshIndexSnapshotsToggle`'s caller context (`is_admin` already exists; no new field is actually read from the *status* response itself for this feature — `index_snapshots` comes from the separate `/api/admin/settings` endpoint — so no typedef change is needed here; skip this if nothing references a new status field).

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/autoindex-status.test.js`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add plugin/src/autoindex-status.xhtml plugin/src/autoindex-status.js plugin/test/autoindex-status.test.js
git commit -m "feat(plugin): add admin UI for the Index Snapshots setting and purge action"
```

---

## Task 16: Full test-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full plugin test suite**

Run: `node --test "plugin/test/*.test.js"`
Expected: all tests PASS, including every test added in Tasks 11-15 and all pre-existing tests (no regressions).

- [ ] **Step 2: Run the full backend test suite**

Run: `uv run pytest backend/tests/ -v`
Expected: all tests PASS, including every test added in Tasks 1-10 and all pre-existing tests (no regressions).

- [ ] **Step 3: If anything fails**

Do not move on. Identify which task's change caused the regression (check `git log --oneline` against this plan's commit messages to find the right commit), fix it with a new commit on top (never amend a prior task's commit), and re-run both full suites until both are clean.

- [ ] **Step 4: Report completion**

Summarize: all 16 tasks complete, both test suites green, branch `feature/configurable-snapshot-indexing` ready for the user to review before opening a PR (do not push or open a PR — that's a separate, explicit step the user asks for when ready).
