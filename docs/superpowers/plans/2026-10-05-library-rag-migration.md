# Cross-Instance Library RAG Data Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator copy one Zotero library's indexed RAG data (vectors, dedup records, index metadata) from one zotero-rag backend instance to another (e.g. production → local dev) via a new CLI script and a set of admin-gated backend endpoints, fully overwriting the destination's existing data for that library.

**Architecture:** Three new `VectorStore` methods (`export_points`, `import_points`, `count_library_points`) back six new FastAPI endpoints under `/api/migration/*` (`backend/api/migration.py`), all gated by the existing `require_authorized_group_admin` dependency. A new script, `bin/migrate_library.py`, drives both instances purely over HTTP (never touching Qdrant directly): it checks embedding-model compatibility, clears the destination's existing data for the library, then pages through `document_chunks` and `deduplication` export/import calls before transferring the single `library_metadata` point last.

**Tech Stack:** FastAPI, Pydantic v2, `qdrant-client`, `httpx`, Python `unittest` (matching the existing test suite's convention — this project does not use `pytest`-style bare functions for backend tests even though it runs under `pytest`).

**Full design spec:** `docs/superpowers/specs/2026-10-05-library-rag-migration-design.md`

---

### Task 1: `VectorStore` migration support methods

**Files:**
- Modify: `backend/db/vector_store.py` (insert new methods between `get_collection_info` and the `# Library Metadata Methods` comment, around line 1140)
- Test: `backend/tests/test_vector_store.py` (append a new test class at the end of the file)

- [ ] **Step 1: Write the failing tests**

Append to the end of `backend/tests/test_vector_store.py`:

```python
class MigrationExportImportTest(unittest.TestCase):
    """Tests for export_points/import_points/count_library_points, which back
    the cross-instance library migration endpoints in backend/api/migration.py.
    See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.vector_store = VectorStore(
            storage_path=Path(self.temp_dir) / "qdrant",
            embedding_dim=4,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )
        self._add_chunk("u1", "item-a", [0.1, 0.2, 0.3, 0.4])
        self._add_chunk("u1", "item-b", [0.5, 0.6, 0.7, 0.8])
        self._add_chunk("u2", "item-c", [0.9, 0.9, 0.9, 0.9])
        self.vector_store.add_deduplication_record(
            DeduplicationRecord(content_hash="hash-1", library_id="u1", item_key="item-a")
        )
        self.vector_store.add_deduplication_record(
            DeduplicationRecord(content_hash="hash-2", library_id="u2", item_key="item-c")
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _add_chunk(self, library_id, item_key, embedding):
        chunk = DocumentChunk(
            text="some text",
            embedding=embedding,
            metadata=ChunkMetadata(
                chunk_id=f"{item_key}-chunk-0",
                document_metadata=DocumentMetadata(library_id=library_id, item_key=item_key),
                text_preview="some text",
                chunk_index=0,
                content_hash=f"hash-of-{item_key}",
            ),
        )
        self.vector_store.add_chunk(chunk)

    def test_count_library_points_scopes_to_library(self):
        self.assertEqual(self.vector_store.count_library_points("chunks", "u1"), 2)
        self.assertEqual(self.vector_store.count_library_points("chunks", "u2"), 1)
        self.assertEqual(self.vector_store.count_library_points("dedup", "u1"), 1)

    def test_export_points_paginates_and_filters_by_library(self):
        page1, offset1 = self.vector_store.export_points("chunks", "u1", offset=None, limit=1)
        self.assertEqual(len(page1), 1)
        self.assertIsNotNone(offset1)

        page2, offset2 = self.vector_store.export_points("chunks", "u1", offset=offset1, limit=1)
        self.assertEqual(len(page2), 1)
        self.assertIsNone(offset2)

        all_item_keys = {p["payload"]["item_key"] for p in (page1 + page2)}
        self.assertEqual(all_item_keys, {"item-a", "item-b"})
        self.assertEqual(len(page1[0]["vector"]), 4)

    def test_export_points_unknown_collection_raises(self):
        with self.assertRaises(ValueError):
            self.vector_store.export_points("not-a-collection", "u1", offset=None, limit=10)

    def test_import_points_round_trips_into_a_fresh_store(self):
        points, _ = self.vector_store.export_points("chunks", "u1", offset=None, limit=10)
        dest_dir = tempfile.mkdtemp()
        try:
            dest_store = VectorStore(
                storage_path=Path(dest_dir) / "qdrant",
                embedding_dim=4,
                embedding_model_name="test-model",
                distance=Distance.COSINE,
            )
            imported = dest_store.import_points("chunks", points)
            self.assertEqual(imported, 2)
            self.assertEqual(dest_store.count_library_points("chunks", "u1"), 2)
            dest_points, _ = dest_store.export_points("chunks", "u1", offset=None, limit=10)
            dest_by_id = {p["id"]: p for p in dest_points}
            for original in points:
                self.assertEqual(dest_by_id[original["id"]]["payload"], original["payload"])
                self.assertEqual(dest_by_id[original["id"]]["vector"], original["vector"])
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_import_points_empty_list_is_noop(self):
        self.assertEqual(self.vector_store.import_points("chunks", []), 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_vector_store.py -v -k MigrationExportImportTest`
Expected: FAIL with `AttributeError: 'VectorStore' object has no attribute 'count_library_points'` (or similar for `export_points`/`import_points`).

- [ ] **Step 3: Implement the methods**

In `backend/db/vector_store.py`, find this existing block:

```python
    def get_collection_info(self) -> dict:
        """
        Get information about the vector store collections.

        Returns:
            Dictionary with collection statistics
        """
        chunks_info = self.client.get_collection(self.CHUNKS_COLLECTION)
        dedup_info = self.client.get_collection(self.DEDUP_COLLECTION)
        metadata_info = self.client.get_collection(self.METADATA_COLLECTION)

        return {
            "chunks_count": chunks_info.points_count,
            "dedup_count": dedup_info.points_count,
            "metadata_count": metadata_info.points_count,
            "embedding_dim": self.embedding_dim,
            "embedding_model_name": self.embedding_model_name,
            "distance": self.distance.value if hasattr(self.distance, 'value') else str(self.distance),
        }

    # Library Metadata Methods
```

Replace it with (adding four new methods before the `# Library Metadata Methods` comment):

```python
    def get_collection_info(self) -> dict:
        """
        Get information about the vector store collections.

        Returns:
            Dictionary with collection statistics
        """
        chunks_info = self.client.get_collection(self.CHUNKS_COLLECTION)
        dedup_info = self.client.get_collection(self.DEDUP_COLLECTION)
        metadata_info = self.client.get_collection(self.METADATA_COLLECTION)

        return {
            "chunks_count": chunks_info.points_count,
            "dedup_count": dedup_info.points_count,
            "metadata_count": metadata_info.points_count,
            "embedding_dim": self.embedding_dim,
            "embedding_model_name": self.embedding_model_name,
            "distance": self.distance.value if hasattr(self.distance, 'value') else str(self.distance),
        }

    # ---------------------------------------------------------------------
    # Cross-instance migration support (backend/api/migration.py,
    # bin/migrate_library.py). See
    # docs/superpowers/specs/2026-10-05-library-rag-migration-design.md.
    # ---------------------------------------------------------------------

    def _resolve_migration_collection(self, collection: str) -> str:
        """Map the migration API's short collection name to the actual Qdrant collection.

        Only document_chunks and deduplication are ever paginated/transferred
        this way — library_metadata is a single point per library, handled
        separately via get_library_metadata/update_library_metadata.
        """
        if collection == "chunks":
            return self.CHUNKS_COLLECTION
        if collection == "dedup":
            return self.DEDUP_COLLECTION
        raise ValueError(f"Unknown migration collection: {collection!r} (expected 'chunks' or 'dedup')")

    def count_library_points(self, collection: str, library_id: str) -> int:
        """Count points for a library in document_chunks or deduplication.

        Used by the migration script to show progress against a total while
        paginating export_points.
        """
        collection_name = self._resolve_migration_collection(collection)
        return self.client.count(
            collection_name=collection_name,
            count_filter=Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=library_id))]),
        ).count

    def export_points(
        self,
        collection: str,
        library_id: str,
        offset: Optional[str],
        limit: int,
    ) -> tuple[list[dict], Optional[str]]:
        """Return one page of raw points (id, vector, payload) for a library.

        Used by GET /api/migration/export to export a library's
        document_chunks or deduplication records to another instance
        without either side touching Qdrant directly. `offset` is Qdrant's
        own opaque scroll cursor from a previous call's returned
        next_offset, passed through unmodified.
        """
        collection_name = self._resolve_migration_collection(collection)
        points, next_offset = self.client.scroll(
            collection_name=collection_name,
            scroll_filter=Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=library_id))]),
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        return (
            [{"id": p.id, "vector": p.vector, "payload": p.payload} for p in points],
            next_offset,
        )

    def import_points(self, collection: str, points: list[dict]) -> int:
        """Upsert a batch of raw points (as returned by export_points) into a collection.

        Used by POST /api/migration/import — writes the source instance's
        exact id/vector/payload with no transformation.
        """
        if not points:
            return 0
        collection_name = self._resolve_migration_collection(collection)
        point_structs = [
            PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"])
            for p in points
        ]
        self.client.upsert(collection_name=collection_name, points=point_structs)
        return len(point_structs)

    # Library Metadata Methods
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_vector_store.py -v -k MigrationExportImportTest`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full vector store test file to check for regressions**

Run: `uv run pytest backend/tests/test_vector_store.py -v`
Expected: PASS (all tests, including the pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add backend/db/vector_store.py backend/tests/test_vector_store.py
git commit -m "$(cat <<'EOF'
feat(vector-store): add export/import/count methods for cross-instance library migration

Backs the new /api/migration/* endpoints (next commit) with raw
point-level scroll/upsert against document_chunks and deduplication,
scoped by library_id.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Migration API endpoints

**Files:**
- Create: `backend/api/migration.py`
- Modify: `backend/main.py:20-21,213-222` (register the new router)
- Test: `backend/tests/test_migration_api.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_migration_api.py`:

```python
"""Endpoint tests for backend.api.migration — the cross-instance library RAG
data migration endpoints consumed by bin/migrate_library.py. See
docs/superpowers/specs/2026-10-05-library-rag-migration-design.md."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from qdrant_client.models import Distance

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.dependencies import require_authorized_group_admin
from backend.models.document import DocumentChunk, ChunkMetadata, DocumentMetadata, DeduplicationRecord
from backend.models.library import LibraryIndexMetadata
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache


class MigrationApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.testing = True
        self.client = TestClient(app)

        self.vector_store = VectorStore(
            storage_path=Path(self.tmp.name) / "qdrant",
            embedding_dim=4,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )
        app.state.vector_store = self.vector_store
        self._add_chunk("u1", "item-a", [0.1, 0.2, 0.3, 0.4])
        self._add_chunk("u1", "item-b", [0.5, 0.6, 0.7, 0.8])
        self.vector_store.add_deduplication_record(
            DeduplicationRecord(content_hash="hash-1", library_id="u1", item_key="item-a")
        )
        self.vector_store.update_library_metadata(
            LibraryIndexMetadata(library_id="u1", library_type="user", library_name="Mine", total_chunks=2)
        )

        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _override_admin(self, identity):
        app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def _add_chunk(self, library_id, item_key, embedding):
        chunk = DocumentChunk(
            text="some text",
            embedding=embedding,
            metadata=ChunkMetadata(
                chunk_id=f"{item_key}-chunk-0",
                document_metadata=DocumentMetadata(library_id=library_id, item_key=item_key),
                text_preview="some text",
                chunk_index=0,
                content_hash=f"hash-of-{item_key}",
            ),
        )
        self.vector_store.add_chunk(chunk)

    def test_embedding_info_reports_current_config(self):
        r = self.client.get("/api/migration/embedding-info")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"embedding_model_name": "test-model", "embedding_dim": 4})

    def test_export_metadata_404_for_unindexed_library(self):
        r = self.client.get("/api/migration/export/metadata", params={"library_id": "u404"})
        self.assertEqual(r.status_code, 404)

    def test_export_metadata_returns_payload(self):
        r = self.client.get("/api/migration/export/metadata", params={"library_id": "u1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["library_id"], "u1")
        self.assertEqual(r.json()["total_chunks"], 2)

    def test_export_points_paginates_chunks(self):
        r1 = self.client.get(
            "/api/migration/export", params={"library_id": "u1", "collection": "chunks", "limit": 1}
        )
        self.assertEqual(r1.status_code, 200)
        page1 = r1.json()
        self.assertEqual(len(page1["points"]), 1)
        self.assertIsNotNone(page1["next_offset"])

        r2 = self.client.get(
            "/api/migration/export",
            params={"library_id": "u1", "collection": "chunks", "limit": 1, "offset": page1["next_offset"]},
        )
        page2 = r2.json()
        self.assertEqual(len(page2["points"]), 1)
        self.assertIsNone(page2["next_offset"])

    def test_import_begin_clears_only_target_library(self):
        self._add_chunk("u2", "item-c", [0.9, 0.9, 0.9, 0.9])
        r = self.client.post("/api/migration/import/begin", json={"library_id": "u1"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["chunks_deleted"], 2)
        self.assertEqual(body["dedup_deleted"], 1)
        self.assertTrue(body["metadata_deleted"])
        self.assertEqual(self.vector_store.count_library_points("chunks", "u1"), 0)
        self.assertEqual(self.vector_store.count_library_points("chunks", "u2"), 1)

    def test_import_points_then_export_round_trips(self):
        export = self.client.get(
            "/api/migration/export", params={"library_id": "u1", "collection": "chunks", "limit": 10}
        ).json()

        dest_dir = tempfile.mkdtemp()
        try:
            dest_store = VectorStore(
                storage_path=Path(dest_dir) / "qdrant",
                embedding_dim=4,
                embedding_model_name="test-model",
                distance=Distance.COSINE,
            )
            app.state.vector_store = dest_store
            r = self.client.post(
                "/api/migration/import",
                params={"collection": "chunks"},
                json={"library_id": "u1", "points": export["points"]},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["imported"], 2)
            self.assertEqual(dest_store.count_library_points("chunks", "u1"), 2)
        finally:
            app.state.vector_store = self.vector_store
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_import_metadata_round_trips(self):
        metadata_payload = self.client.get(
            "/api/migration/export/metadata", params={"library_id": "u1"}
        ).json()
        self.vector_store.delete_library_metadata("u1")
        r = self.client.post(
            "/api/migration/import/metadata",
            json={"library_id": "u1", "payload": metadata_payload},
        )
        self.assertEqual(r.status_code, 200)
        restored = self.vector_store.get_library_metadata("u1")
        self.assertEqual(restored.total_chunks, 2)

    def test_import_metadata_rejects_library_id_mismatch(self):
        metadata_payload = self.client.get(
            "/api/migration/export/metadata", params={"library_id": "u1"}
        ).json()
        r = self.client.post(
            "/api/migration/import/metadata",
            json={"library_id": "u2", "payload": metadata_payload},
        )
        self.assertEqual(r.status_code, 400)

    def test_endpoints_require_admin_when_not_loopback(self):
        app.dependency_overrides.clear()
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = None
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)):
            r = self.client.get("/api/migration/embedding-info", headers={"X-Zotero-API-Key": "K"})
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_migration_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.api.migration'` (or a 404 from the TestClient once the import error is worked around) since neither the module nor the route exist yet.

- [ ] **Step 3: Create `backend/api/migration.py`**

```python
"""
Cross-instance library RAG data migration endpoints.

Lets an operator copy one Zotero library's indexed vectors
(document_chunks), dedup records, and index metadata from one zotero-rag
instance to another (e.g. production -> local dev) via
bin/migrate_library.py, without either side needing direct Qdrant access.
See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md.

Every endpoint requires require_authorized_group_admin — the same
dependency gating the autoindex scheduler's admin controls — evaluated
against *this* instance's own AUTHORIZED_GROUP_ID. The source and
destination admin accounts need not match; this is an instance-admin
action, not a per-library-access check (unlike assert_can_access used by
the normal library/query endpoints).
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store, require_authorized_group_admin
from backend.models.library import LibraryIndexMetadata
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()
logger = logging.getLogger(__name__)

MigrationCollection = Literal["chunks", "dedup"]


class EmbeddingInfoResponse(BaseModel):
    """This instance's embedding model/dimension, for the migration script's compatibility check."""
    embedding_model_name: str
    embedding_dim: int


class ExportPoint(BaseModel):
    """One raw Qdrant point as transferred between instances."""
    id: str
    vector: list[float]
    payload: dict


class ExportPageResponse(BaseModel):
    """One page of exported points plus the cursor for the next page (None when done)."""
    points: list[ExportPoint]
    next_offset: Optional[str] = None


class ImportBeginRequest(BaseModel):
    library_id: str


class ImportBeginResponse(BaseModel):
    """Counts of what import/begin deleted on the destination before any new data is written."""
    library_id: str
    chunks_deleted: int
    dedup_deleted: int
    metadata_deleted: bool


class ImportPointsRequest(BaseModel):
    library_id: str
    points: list[ExportPoint]


class ImportMetadataRequest(BaseModel):
    library_id: str
    payload: dict


@router.get("/migration/embedding-info", response_model=EmbeddingInfoResponse)
def get_embedding_info(
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Report this instance's embedding model/dimension (admin only)."""
    info = vector_store.get_collection_info()
    return EmbeddingInfoResponse(
        embedding_model_name=info["embedding_model_name"],
        embedding_dim=info["embedding_dim"],
    )


@router.get("/migration/export/metadata", response_model=LibraryIndexMetadata)
def export_library_metadata(
    library_id: str,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Export a library's index metadata point (admin only). 404 if never indexed here."""
    metadata = vector_store.get_library_metadata(library_id)
    if metadata is None:
        raise HTTPException(
            status_code=404,
            detail=f"Library {library_id!r} has never been indexed on this instance.",
        )
    return metadata


@router.get("/migration/export", response_model=ExportPageResponse)
def export_library_points(
    library_id: str,
    collection: MigrationCollection,
    offset: Optional[str] = None,
    limit: int = 200,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Export one page of a library's document_chunks or deduplication points (admin only)."""
    points, next_offset = vector_store.export_points(collection, library_id, offset, limit)
    return ExportPageResponse(points=points, next_offset=next_offset)


@router.post("/migration/import/begin", response_model=ImportBeginResponse)
def import_begin(
    body: ImportBeginRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Clear this instance's existing chunks/dedup/metadata for a library (admin only).

    Always called before any import batch — the migration overwrites the
    destination's data for the library rather than merging into it.
    """
    chunks_deleted = vector_store.delete_library_chunks(body.library_id)
    dedup_deleted = vector_store.delete_library_deduplication_records(body.library_id)
    metadata_deleted = vector_store.delete_library_metadata(body.library_id)
    return ImportBeginResponse(
        library_id=body.library_id,
        chunks_deleted=chunks_deleted,
        dedup_deleted=dedup_deleted,
        metadata_deleted=metadata_deleted,
    )


@router.post("/migration/import")
def import_points_batch(
    collection: MigrationCollection,
    body: ImportPointsRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> dict:
    """Import one batch of exported points into document_chunks or deduplication (admin only)."""
    imported = vector_store.import_points(
        collection,
        [p.model_dump() for p in body.points],
    )
    return {"imported": imported}


@router.post("/migration/import/metadata")
def import_library_metadata(
    body: ImportMetadataRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> dict:
    """Import a library's index metadata point, exactly as returned by export/metadata (admin only)."""
    if body.payload.get("library_id") != body.library_id:
        raise HTTPException(status_code=400, detail="payload.library_id does not match library_id.")
    metadata = LibraryIndexMetadata(**body.payload)
    vector_store.update_library_metadata(metadata)
    return {"library_id": body.library_id}
```

- [ ] **Step 4: Register the router in `backend/main.py`**

Find:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth
```

Replace with:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, migration
```

Find:

```python
app.include_router(autoindex.router, prefix="/api", tags=["autoindex"])
app.include_router(auth.router, prefix="/api", tags=["auth"])
```

Replace with:

```python
app.include_router(autoindex.router, prefix="/api", tags=["autoindex"])
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(migration.router, prefix="/api", tags=["migration"])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_migration_api.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Run the full backend test suite to check for regressions**

Run: `uv run pytest backend/tests -v -x`
Expected: PASS (no regressions in libraries/autoindex/other API tests)

- [ ] **Step 7: Commit**

```bash
git add backend/api/migration.py backend/main.py backend/tests/test_migration_api.py
git commit -m "$(cat <<'EOF'
feat(migration): add admin-gated /api/migration/* endpoints

Exports and imports one library's document_chunks, deduplication
records, and index metadata so bin/migrate_library.py (next commit)
can copy a library's RAG data between instances over HTTP only, never
touching Qdrant directly on either side.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `bin/migrate_library.py` script

**Files:**
- Create: `bin/migrate_library.py`
- Test: `backend/tests/test_migrate_library.py` (new file)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_migrate_library.py`:

```python
"""Unit tests for bin/migrate_library.py."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "migrate_library.py"
_SPEC = importlib.util.spec_from_file_location("migrate_library_script", _SCRIPT_PATH)
migrate_library = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate_library)


class FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self):
        return self._json_body


class FakeClient:
    """Minimal stand-in for httpx.Client. `responses` is a list of (status_code,
    json_body) tuples or Exception instances, consumed in order, one per
    .request() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status_code, body = item
        return FakeResponse(status_code, body)


class ParseArgsTest(unittest.TestCase):
    def test_required_positional_args(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        self.assertEqual(args.slug, "users/39226")
        self.assertEqual(args.source_url, "http://source")
        self.assertEqual(args.dest_url, "http://dest")
        self.assertEqual(args.batch_size, 200)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.source_key)

    def test_optional_flags(self):
        args = migrate_library._parse_args([
            "groups/6297749", "http://source", "http://dest",
            "--source-key", "SK", "--dest-key", "DK", "--batch-size", "50", "--dry-run",
        ])
        self.assertEqual(args.source_key, "SK")
        self.assertEqual(args.dest_key, "DK")
        self.assertEqual(args.batch_size, 50)
        self.assertTrue(args.dry_run)


class RequestRetryTest(unittest.TestCase):
    def test_retries_on_connect_error_then_succeeds(self):
        client = FakeClient([httpx.ConnectError("boom"), (200, {"ok": True})])
        with patch.object(migrate_library.time, "sleep"):
            result = migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.calls), 2)

    def test_raises_migration_error_after_max_attempts(self):
        client = FakeClient([httpx.ConnectError("boom")] * migrate_library._MAX_ATTEMPTS)
        with patch.object(migrate_library.time, "sleep"):
            with self.assertRaises(migrate_library.MigrationError):
                migrate_library._get(client, "http://source", "/x", "KEY")

    def test_raises_migration_error_on_non_200(self):
        client = FakeClient([(403, {"detail": "nope"})])
        with self.assertRaises(migrate_library.MigrationError):
            migrate_library._get(client, "http://source", "/x", "KEY")

    def test_get_omits_none_params(self):
        client = FakeClient([(200, {"ok": True})])
        migrate_library._get(client, "http://source", "/x", "KEY", offset=None, limit=5)
        _, _, params, _ = client.calls[0]
        self.assertEqual(params, {"limit": 5})

    def test_post_sends_json_body_and_query_params(self):
        client = FakeClient([(200, {"ok": True})])
        migrate_library._post(client, "http://dest", "/x", "KEY", {"a": 1}, collection="chunks")
        method, url, params, json_body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(params, {"collection": "chunks"})
        self.assertEqual(json_body, {"a": 1})


class RunMigrationTest(unittest.TestCase):
    def setUp(self):
        self.embedding_info = {"embedding_model_name": "m", "embedding_dim": 8}
        self.metadata = {
            "library_id": "u39226", "library_type": "user", "library_name": "Mine",
            "last_indexed_version": 1, "total_items_indexed": 2, "total_chunks": 3,
        }

    def test_embedding_mismatch_raises_before_any_write(self):
        def fake_get(client, base_url, path, api_key, **params):
            if base_url == "http://source":
                return {"embedding_model_name": "m", "embedding_dim": 8}
            return {"embedding_model_name": "m", "embedding_dim": 16}

        with patch.object(migrate_library, "_get", side_effect=fake_get), \
             patch.object(migrate_library, "_post") as mock_post:
            with self.assertRaises(migrate_library.MigrationError):
                migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK",
                )
        mock_post.assert_not_called()

    def test_dry_run_returns_without_posting(self):
        def fake_get(client, base_url, path, api_key, **params):
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            return self.metadata

        with patch.object(migrate_library, "_get", side_effect=fake_get), \
             patch.object(migrate_library, "_post") as mock_post:
            result = migrate_library.run_migration(
                client=object(), slug="users/39226",
                source_url="http://source", dest_url="http://dest",
                source_key="SK", dest_key="DK", dry_run=True,
            )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["library_id"], "u39226")
        mock_post.assert_not_called()

    def test_full_run_paginates_and_calls_import_begin_and_metadata(self):
        chunk_pages = [
            {"points": [{"id": "1", "vector": [0.1] * 8, "payload": {}}], "next_offset": "cursor-1"},
            {"points": [{"id": "2", "vector": [0.2] * 8, "payload": {}}], "next_offset": None},
        ]
        dedup_pages = [{"points": [], "next_offset": None}]
        get_calls = []

        def fake_get(client, base_url, path, api_key, **params):
            get_calls.append((path, params))
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            if path == "/api/migration/export/metadata":
                return self.metadata
            if path == "/api/migration/export" and params["collection"] == "chunks":
                return chunk_pages.pop(0)
            if path == "/api/migration/export" and params["collection"] == "dedup":
                return dedup_pages.pop(0)
            raise AssertionError(f"unexpected GET {path} {params}")

        post_calls = []

        def fake_post(client, base_url, path, api_key, body, **params):
            post_calls.append((path, params, body))
            if path == "/api/migration/import/begin":
                return {"library_id": "u39226", "chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False}
            return {}

        with patch.object(migrate_library, "_get", side_effect=fake_get), \
             patch.object(migrate_library, "_post", side_effect=fake_post):
            result = migrate_library.run_migration(
                client=object(), slug="users/39226",
                source_url="http://source", dest_url="http://dest",
                source_key="SK", dest_key="DK", batch_size=1,
            )

        self.assertEqual(result["transferred"], {"chunks": 2, "dedup": 0})

        import_paths = [p for p, _, _ in post_calls]
        self.assertEqual(
            import_paths,
            [
                "/api/migration/import/begin",
                "/api/migration/import",
                "/api/migration/import",
                "/api/migration/import/metadata",
            ],
        )

        chunk_offsets = [
            params["offset"] for path, params in get_calls
            if path == "/api/migration/export" and params["collection"] == "chunks"
        ]
        self.assertEqual(chunk_offsets, [None, "cursor-1"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_migrate_library.py -v`
Expected: FAIL — `FileNotFoundError` from `spec_from_file_location`/`exec_module` since `bin/migrate_library.py` doesn't exist yet.

- [ ] **Step 3: Create `bin/migrate_library.py`**

```python
"""
Migrate one Zotero library's indexed RAG data (document_chunks,
deduplication records, and index metadata) from one zotero-rag backend
instance to another.

Usage:
    uv run python bin/migrate_library.py <slug> <source-url> <dest-url> \
        --source-key <source-admin-zotero-api-key> \
        --dest-key <dest-admin-zotero-api-key> \
        [--batch-size 200] [--dry-run]

<slug> is a Zotero.org library slug, e.g. users/39226 or groups/6297749.
--source-key/--dest-key must belong to an account that is an owner/admin of
the respective instance's AUTHORIZED_GROUP_ID (omit for a loopback-mode
instance, which needs no admin key).

The destination's existing data for this library is fully overwritten. A
failed run is recovered by simply re-running the script — the destination
is re-cleared on every run; there is no resume-from-cursor logic.

See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md.
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx  # noqa: E402

from backend.api.public_query import slug_to_backend_id  # noqa: E402

MIGRATION_COLLECTIONS = ("chunks", "dedup")
_MAX_ATTEMPTS = 4


class MigrationError(RuntimeError):
    """Raised for any unrecoverable failure during migration; main() catches it and exits 1."""


def _headers(api_key: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Zotero-API-Key"] = api_key
    return headers


def _request(
    client, method: str, base_url: str, path: str, api_key: Optional[str],
    params: Optional[dict] = None, json_body: Optional[dict] = None,
) -> dict:
    url = base_url.rstrip("/") + path
    query = {k: v for k, v in (params or {}).items() if v is not None}
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.request(method, url, headers=_headers(api_key), params=query, json=json_body, timeout=120.0)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            if attempt >= _MAX_ATTEMPTS:
                raise MigrationError(f"{method} {url} failed after {_MAX_ATTEMPTS} attempts: {exc}") from exc
            time.sleep(2 ** (attempt - 1))
            continue
        break
    if response.status_code != 200:
        raise MigrationError(f"{method} {url} -> HTTP {response.status_code}: {response.text}")
    return response.json()


def _get(client, base_url: str, path: str, api_key: Optional[str], **params) -> dict:
    return _request(client, "GET", base_url, path, api_key, params=params)


def _post(client, base_url: str, path: str, api_key: Optional[str], json_body: dict, **params) -> dict:
    return _request(client, "POST", base_url, path, api_key, params=params, json_body=json_body)


def run_migration(
    client,
    slug: str,
    source_url: str,
    dest_url: str,
    source_key: Optional[str],
    dest_key: Optional[str],
    batch_size: int = 200,
    dry_run: bool = False,
) -> dict:
    """Run the full migration (or, if dry_run, just the compatibility/size check).

    Returns a summary dict used by main() for its console output and by
    tests for assertions.
    """
    library_id = slug_to_backend_id(slug)

    source_info = _get(client, source_url, "/api/migration/embedding-info", source_key)
    dest_info = _get(client, dest_url, "/api/migration/embedding-info", dest_key)
    source_model = (source_info["embedding_model_name"], source_info["embedding_dim"])
    dest_model = (dest_info["embedding_model_name"], dest_info["embedding_dim"])
    if source_model != dest_model:
        raise MigrationError(
            f"Embedding mismatch: source uses {source_model[0]} ({source_model[1]}-dim), "
            f"destination uses {dest_model[0]} ({dest_model[1]}-dim). "
            "Re-index on the destination instead of migrating vectors between incompatible models."
        )

    metadata = _get(client, source_url, "/api/migration/export/metadata", source_key, library_id=library_id)

    if dry_run:
        return {"library_id": library_id, "dry_run": True, "metadata": metadata}

    begin_result = _post(client, dest_url, "/api/migration/import/begin", dest_key, {"library_id": library_id})

    transferred = {}
    for collection in MIGRATION_COLLECTIONS:
        count = 0
        offset = None
        while True:
            page = _get(
                client, source_url, "/api/migration/export", source_key,
                library_id=library_id, collection=collection, offset=offset, limit=batch_size,
            )
            points = page["points"]
            if points:
                _post(
                    client, dest_url, "/api/migration/import", dest_key,
                    {"library_id": library_id, "points": points}, collection=collection,
                )
                count += len(points)
            offset = page["next_offset"]
            if offset is None:
                break
        transferred[collection] = count

    _post(client, dest_url, "/api/migration/import/metadata", dest_key, {"library_id": library_id, "payload": metadata})

    return {
        "library_id": library_id,
        "dry_run": False,
        "begin_result": begin_result,
        "transferred": transferred,
        "metadata": metadata,
    }


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug", help="Zotero library slug, e.g. users/39226 or groups/6297749")
    parser.add_argument("source_url", help="Source instance base URL, e.g. https://rag.example.com")
    parser.add_argument("dest_url", help="Destination instance base URL, e.g. http://localhost:8119")
    parser.add_argument("--source-key", default=None, help="Admin Zotero API key for the source instance")
    parser.add_argument("--dest-key", default=None, help="Admin Zotero API key for the destination instance")
    parser.add_argument("--batch-size", type=int, default=200, help="Points per export/import page (default: 200)")
    parser.add_argument("--dry-run", action="store_true", help="Check compatibility and report counts without writing anything")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    start = time.monotonic()
    try:
        with httpx.Client() as client:
            result = run_migration(
                client, args.slug, args.source_url, args.dest_url,
                args.source_key, args.dest_key, args.batch_size, args.dry_run,
            )
    except MigrationError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)

    if result["dry_run"]:
        meta = result["metadata"]
        print(
            f"[OK] --dry-run: {args.slug} ({result['library_id']}) has {meta['total_chunks']} chunks, "
            f"{meta['total_items_indexed']} items on {args.source_url}. No data written."
        )
        return

    print(
        f"[OK] Cleared destination: {result['begin_result']['chunks_deleted']} chunks, "
        f"{result['begin_result']['dedup_deleted']} dedup records."
    )
    for collection, count in result["transferred"].items():
        print(f"[OK] {collection}: {count} points transferred")
    print(f"[OK] Metadata transferred. Done in {time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_migrate_library.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/migrate_library.py backend/tests/test_migrate_library.py
git commit -m "$(cat <<'EOF'
feat(migration): add bin/migrate_library.py CLI

Drives the new /api/migration/* endpoints over HTTP to copy one
library's RAG data between two zotero-rag instances: checks embedding
compatibility, clears the destination, pages through chunks and
dedup records, then transfers index metadata last.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Document the new tool in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Insert a new operational section**

Find this exact text (the end of the "Debugging the cron indexer" section, right before "## Python Environment"):

```markdown
**Host swap:** An 8 GB swapfile lives at `/swapfile` (persistent via `/etc/fstab`). It must be set up manually on each new server — it is intentionally not in the deploy scripts because disk capacity varies per host.

## Python Environment
```

Replace it with:

```markdown
**Host swap:** An 8 GB swapfile lives at `/swapfile` (persistent via `/etc/fstab`). It must be set up manually on each new server — it is intentionally not in the deploy scripts because disk capacity varies per host.

## Migrating Library RAG Data Between Instances

`bin/migrate_library.py` copies one Zotero library's indexed RAG data
(vectors, dedup records, index metadata) from one zotero-rag backend
instance to another — e.g. pulling a production library's real indexed
data down to a local dev instance to test retrieval/routing changes
against it, without re-indexing or copying the entire shared vector
database. See `docs/superpowers/specs/2026-10-05-library-rag-migration-design.md`
for the full design.

The destination's existing data for that library is **fully overwritten**.
Both instances are reached only through their HTTP APIs
(`/api/migration/*`, `backend/api/migration.py`) — never direct Qdrant
access — and every endpoint requires an admin of that instance's own
`AUTHORIZED_GROUP_ID` (the same `require_authorized_group_admin`
dependency gating the autoindex scheduler's admin controls). A
loopback-mode instance (local dev with no `AUTHORIZED_GROUP_ID`
configured) needs no admin key for that side.

```bash
uv run python bin/migrate_library.py <slug> <source-url> <dest-url> \
  --source-key <source-admin-zotero-api-key> \
  --dest-key <dest-admin-zotero-api-key> \
  [--batch-size 200] [--dry-run]
```

`<slug>` is a Zotero.org library slug (`users/<id>` or `groups/<id>`), e.g.
`groups/6297749` for the `test-rag-plugin` library. `--dry-run` reports the
source library's indexed size and checks embedding-model compatibility
without writing anything. The script aborts before any write if source
and destination use different embedding models/dimensions — re-index on
the destination in that case rather than migrating incompatible vectors.
A failed run is recovered by simply re-running the script; the
destination is re-cleared on every run, so there is no
resume-from-cursor logic.

## Python Environment
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(migration): document bin/migrate_library.py in CLAUDE.md

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Manual verification (not a subagent task)

Automated tests cover the API endpoints and the script's own logic in isolation, but not a true two-live-instance run. After Tasks 1–4 are merged, the user should manually verify against two real running backend instances (e.g. local dev on two different ports/`DATA_PATH`s, or local dev ← production) using the `test-rag-plugin` group library (`groups/6297749`) per this project's existing live-query-debugging conventions:

```bash
uv run python bin/migrate_library.py groups/6297749 <source-url> <dest-url> \
  --source-key <key> --dest-key <key> --dry-run
# then, without --dry-run, and confirm the destination's library status matches:
curl <dest-url>/api/libraries/6297749/index-status -H "X-Zotero-API-Key: <key>"
```

This step requires live credentials and two reachable instances, so it is intentionally left out of the automated task list above.
