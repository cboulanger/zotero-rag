import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.identity = MagicMock(user_id=1, targets=["users/1"])
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

    def test_rejects_access_to_a_library_outside_the_identity_s_targets(self):
        # self.identity.targets is ["users/1"] — "u2" maps to slug "users/2",
        # which isn't in it, so the real (unmocked) assert_can_access inside
        # _parse_upload_request must reject this with a genuine 403, the same
        # way test_document_upload_authorization.py checks the sibling
        # sync/async upload endpoints.
        response = self._upload(library_id="u2")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(pending_upload_cache.has_entry(self.data_path, "u2", "ATT1"))


class TestProcessNowEndpoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.identity = MagicMock(user_id=1, targets=["users/1"])
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
        mock_processor._process_attachment_bytes = AsyncMock(return_value=proc_result)

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
