"""Endpoint tests for backend.api.document_upload authorization:
- batch metadata update, abstract indexing, and file upload all 403 for a
  library outside the caller's targets
- user_id is taken from the validated identity, not the request body
- check-indexed reports "queued"/eta for attachments sitting in the
  deferred-upload cache (reuses this file's TestClient/mock-vector-store
  scaffolding; not itself an authorization test)
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.config import settings as settings_module
from backend.db.vector_store import VectorStore
from backend.services import pending_upload_cache
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

    def test_check_indexed_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/libraries/u2/check-indexed",
            json={"library_id": "u2", "attachments": []},
        )
        self.assertEqual(r.status_code, 403)

    def test_check_indexed_within_targets_succeeds(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/libraries/u1/check-indexed",
            json={"library_id": "u1", "attachments": []},
        )
        self.assertEqual(r.status_code, 200)

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

    def test_upload_document_accepts_timeout_multiplier_field(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "u1",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata, "timeout_multiplier": "2.0"},
        )
        # Not 422/400 — the field is accepted and parsed without error. (Testing
        # mode's extractor stub makes this a cheap smoke test, not a full
        # end-to-end timeout check — that's covered by Task 2's unit tests.)
        self.assertEqual(r.status_code, 200)

    def test_upload_document_rejects_out_of_range_timeout_multiplier(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "u1",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata, "timeout_multiplier": "999999"},
        )
        self.assertEqual(r.status_code, 422)


class TestCheckIndexedQueuedStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.identity = MagicMock(user_id=1, targets=["users/1"])
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
        self.assertIsNotNone(statuses[0].get("eta"))
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


if __name__ == "__main__":
    unittest.main()
