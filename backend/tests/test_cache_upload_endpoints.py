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

    def _upload(self, library_id="u1", attachment_key="ATT1", attachment_title=None):
        metadata = {
            "library_id": library_id, "library_type": "user", "item_key": "ITEM1",
            "attachment_key": attachment_key, "mime_type": "application/pdf",
            "item_version": 3, "attachment_version": 1,
        }
        if attachment_title is not None:
            metadata["attachment_title"] = attachment_title
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

    def test_persists_attachment_title_through_the_cache_write(self):
        # Regression: upload_document_to_cache used to hand-build a separate
        # dict for write_entry that dropped attachment_title even though
        # _parse_upload_request correctly parsed it into DocumentMetadata.
        # Confirm it now survives the round trip through the pending-upload
        # cache, since that's what the purge-snapshots admin endpoint later
        # filters on.
        response = self._upload(attachment_title="Snapshot")
        self.assertEqual(response.status_code, 200)
        _, meta = pending_upload_cache.read_entry(self.data_path, "u1", "ATT1")
        self.assertEqual(meta["attachment_title"], "Snapshot")

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

    @patch("backend.api.document_upload.DocumentProcessor")
    def test_overrides_the_cached_item_version_with_a_live_one_from_the_caller(self, mock_processor_cls):
        # The cached entry (set up in setUp) was written with item_version=3,
        # attachment_version=1 — simulating a deferral that happened a while
        # ago. If the user edited the item since then, forcing it to index
        # now must use the CURRENT version the plugin supplies, not the stale
        # one frozen in the cache — otherwise the vector store would think
        # the item is indexed "as of" version 3, and the next full autoindex
        # scan would see the real (higher) live version and treat it as
        # changed again, re-triggering the same download failure.
        mock_processor = mock_processor_cls.return_value
        proc_result = MagicMock(status="indexed_fresh", chunks_written=1, error_detail=None)
        mock_processor._process_attachment_bytes = AsyncMock(return_value=proc_result)

        response = self.client.post(
            "/api/index/document/cache/u1/ATT1/process-now",
            params={"item_version": 9, "attachment_version": 4},
        )
        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_processor._process_attachment_bytes.call_args
        self.assertEqual(kwargs["item_version"], 9)
        self.assertEqual(kwargs["attachment_version"], 4)

    @patch("backend.api.document_upload.DocumentProcessor")
    def test_falls_back_to_the_cached_version_when_no_override_is_given(self, mock_processor_cls):
        mock_processor = mock_processor_cls.return_value
        proc_result = MagicMock(status="indexed_fresh", chunks_written=1, error_detail=None)
        mock_processor._process_attachment_bytes = AsyncMock(return_value=proc_result)

        response = self.client.post("/api/index/document/cache/u1/ATT1/process-now")
        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_processor._process_attachment_bytes.call_args
        self.assertEqual(kwargs["item_version"], 3)
        self.assertEqual(kwargs["attachment_version"], 1)

    @patch("backend.api.document_upload.DocumentProcessor")
    def test_constructs_the_processor_with_the_preset_s_chunk_size_not_the_class_default(self, mock_processor_cls):
        # Regression: DocumentProcessor's own generic defaults (512 chars /
        # 1500 chars for max_chunk_size/chunk_merge_target_size) are
        # disconnected from any particular embedding model's real token
        # limit. A merged chunk sized against the 1500-char default can
        # exceed a small-context model's safe budget even though the active
        # preset's rag.max_chunk_size was hand-tuned for exactly that model
        # (see e.g. the apple-silicon-kisski preset's comment) — this
        # construction must use the preset's value for both parameters.
        mock_processor = mock_processor_cls.return_value
        proc_result = MagicMock(status="indexed_fresh", chunks_written=1, error_detail=None)
        mock_processor._process_attachment_bytes = AsyncMock(return_value=proc_result)
        fake_preset = MagicMock()
        fake_preset.rag.max_chunk_size = 777

        with patch.object(settings_module.Settings, "get_hardware_preset", return_value=fake_preset):
            response = self.client.post("/api/index/document/cache/u1/ATT1/process-now")

        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_processor_cls.call_args
        self.assertEqual(kwargs["max_chunk_size"], 777)
        self.assertEqual(kwargs["chunk_merge_target_size"], 777)

    @patch("backend.api.document_upload.DocumentProcessor")
    def test_threads_attachment_title_from_the_cached_entry_into_doc_metadata(self, mock_processor_cls):
        # Regression: process_cached_upload_now rebuilt DocumentMetadata from
        # the persisted cache entry without copying attachment_title, so a
        # Snapshot attachment queued via the deferred path would never be
        # findable by the purge-snapshots admin endpoint once forced through
        # process-now.
        pending_upload_cache.write_entry(
            self.data_path, "u1", "ATT_SNAPSHOT", b"cached bytes",
            {"item_key": "ITEM_SNAPSHOT", "mime_type": "application/pdf", "item_version": 3,
             "attachment_version": 1, "title": "T", "authors": [], "year": None,
             "item_type": None, "library_type": "user", "library_name": "",
             "attachment_title": "Snapshot"},
        )
        mock_processor = mock_processor_cls.return_value
        proc_result = MagicMock(status="indexed_fresh", chunks_written=1, error_detail=None)
        mock_processor._process_attachment_bytes = AsyncMock(return_value=proc_result)

        response = self.client.post("/api/index/document/cache/u1/ATT_SNAPSHOT/process-now")
        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_processor._process_attachment_bytes.call_args
        self.assertEqual(kwargs["doc_metadata"].attachment_title, "Snapshot")

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


class TestParseUploadRequestAttachmentTitle(unittest.IsolatedAsyncioTestCase):
    async def test_parse_upload_request_stores_attachment_title(self):
        from backend.api.document_upload import _parse_upload_request
        from fastapi import UploadFile
        import io

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
        import io

        metadata = json.dumps({"library_id": "1", "item_key": "ITEM1", "attachment_key": "ATT1"})
        upload_file = UploadFile(filename="paper.pdf", file=io.BytesIO(b"%PDF-1.4"))
        result = await _parse_upload_request(upload_file, metadata, identity=None)
        doc_metadata = result[1]
        self.assertIsNone(doc_metadata.attachment_title)


if __name__ == "__main__":
    unittest.main()
