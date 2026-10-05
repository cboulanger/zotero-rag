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

    def test_import_metadata_rejects_malformed_payload(self):
        r = self.client.post(
            "/api/migration/import/metadata",
            json={"library_id": "u1", "payload": {"library_id": "u1"}},
        )
        self.assertEqual(r.status_code, 400)

    def test_import_points_rejects_library_id_mismatch(self):
        export = self.client.get(
            "/api/migration/export", params={"library_id": "u1", "collection": "chunks", "limit": 10}
        ).json()
        points = export["points"]
        points[0]["payload"]["library_id"] = "u2"

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
                json={"library_id": "u1", "points": points},
            )
            self.assertEqual(r.status_code, 400)
            self.assertEqual(dest_store.count_library_points("chunks", "u1"), 0)
        finally:
            app.state.vector_store = self.vector_store
            shutil.rmtree(dest_dir, ignore_errors=True)

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
