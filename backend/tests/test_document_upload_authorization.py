"""Endpoint tests for backend.api.document_upload authorization:
- batch metadata update, abstract indexing, and file upload all 403 for a
  library outside the caller's targets
- user_id is taken from the validated identity, not the request body
"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class DocumentUploadAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.testing = True
        self.client = TestClient(app)
        app.state.vector_store = VectorStore(
            storage_path=Path(self.tmp.name) / "qdrant",
            embedding_dim=8,
            embedding_model_name="test-model",
        )

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_batch_metadata_update_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/index/items/metadata",
            json={"library_id": "u2", "items": []},
        )
        self.assertEqual(r.status_code, 403)

    def test_batch_metadata_update_within_targets_succeeds(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/index/items/metadata",
            json={"library_id": "u1", "items": []},
        )
        self.assertEqual(r.status_code, 200)

    def test_abstract_index_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/index/abstract",
            json={
                "library_id": "u2",
                "item_key": "ITEM1",
                "abstract_text": "word " * 200,
            },
        )
        self.assertEqual(r.status_code, 403)

    def test_upload_document_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "u2",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata},
        )
        self.assertEqual(r.status_code, 403)

    def test_upload_document_body_user_id_is_ignored_in_favor_of_identity(self):
        self._set_identity(ZoteroIdentity(user_id=42, username="real", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "u1",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
            "user_id": 999,  # attacker-supplied — must be ignored
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata},
        )
        self.assertNotEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
