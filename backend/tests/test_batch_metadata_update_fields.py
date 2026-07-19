"""Tests for the extended /api/index/items/metadata endpoint: tags,
item_version, and zotero_modified are now accepted and patched onto
existing Qdrant chunks, in addition to the fields it already supported
(title/authors/year/item_type)."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from qdrant_client.models import Distance

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class BatchMetadataUpdateFieldsTest(unittest.TestCase):
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
            embedding_dim=8,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )
        app.state.vector_store = self.vector_store
        app.dependency_overrides[dependencies.get_zotero_identity] = (
            lambda: ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        )

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _seed_chunk(self, tags, item_version):
        self.vector_store.add_chunk(DocumentChunk(
            text="Chunk 0",
            metadata=ChunkMetadata(
                chunk_id="chunk-0",
                document_metadata=DocumentMetadata(
                    library_id="u1",
                    item_key="ITEM1",
                    title="Old Title",
                    authors=["Old Author"],
                    tags=tags,
                    year=2000,
                    item_type="book",
                ),
                page_number=1,
                text_preview="Chunk 0",
                chunk_index=0,
                content_hash="hash0",
                item_version=item_version,
            ),
            embedding=[0.1] * 8,
        ))

    def test_tags_item_version_and_zotero_modified_are_patched(self):
        self._seed_chunk(tags=["OldTag"], item_version=1)

        r = self.client.post(
            "/api/index/items/metadata",
            json={
                "library_id": "u1",
                "items": [{
                    "item_key": "ITEM1",
                    "title": "New Title",
                    "authors": ["New Author"],
                    "tags": ["NewTag", "SecondTag"],
                    "year": 2020,
                    "item_type": "journalArticle",
                    "item_version": 5,
                    "zotero_modified": "2026-01-01T00:00:00Z",
                }],
            },
        )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["updated_items"], 1)
        self.assertEqual(body["updated_chunks"], 1)

        chunks = self.vector_store.get_item_chunks("u1", "ITEM1")
        self.assertEqual(len(chunks), 1)
        payload = chunks[0]["payload"]
        self.assertEqual(payload["tags"], ["NewTag", "SecondTag"])
        self.assertEqual(payload["tags_lower"], ["newtag", "secondtag"])
        self.assertEqual(payload["item_version"], 5)
        self.assertEqual(payload["zotero_modified"], "2026-01-01T00:00:00Z")

    def test_omitting_the_new_fields_leaves_existing_values_untouched(self):
        """A caller that doesn't send tags/item_version/zotero_modified (e.g.
        a hypothetical legacy caller) must not wipe them — this is the bug
        the wholesale-delegation approach in the design spec would have
        introduced; the conditional-field pattern here avoids it."""
        self._seed_chunk(tags=["KeepMe"], item_version=3)

        r = self.client.post(
            "/api/index/items/metadata",
            json={"library_id": "u1", "items": [{"item_key": "ITEM1", "title": "New Title"}]},
        )

        self.assertEqual(r.status_code, 200)
        chunks = self.vector_store.get_item_chunks("u1", "ITEM1")
        payload = chunks[0]["payload"]
        self.assertEqual(payload["title"], "New Title")
        self.assertEqual(payload["tags"], ["KeepMe"])
        self.assertEqual(payload["item_version"], 3)


if __name__ == "__main__":
    unittest.main()
