"""
Tests for the item_vector_pending flag on DocumentUploadResult.

Verifies:
- Successful (fresh) indexing returns item_vector_pending=True
- Duplicate-skip responses return item_vector_pending=False
- Error responses return item_vector_pending=False
- upload_and_index_abstract follows the same rules
"""

import dataclasses
import json
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.api.document_upload import DocumentUploadResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_METADATA = json.dumps({
    "library_id": "lib1",
    "item_key": "ITEM001",
    "attachment_key": "ATT001",
    "mime_type": "application/pdf",
    "item_version": 1,
    "attachment_version": 1,
    "title": "Test Item",
    "authors": ["Author, A."],
    "year": 2024,
})


def _mock_vector_store(*, is_duplicate: bool = False) -> MagicMock:
    vs = MagicMock()
    vs.check_duplicate.return_value = is_duplicate
    vs.get_item_version.return_value = None
    vs.delete_item_chunks.return_value = 0
    vs.get_library_metadata.return_value = None
    vs.count_library_chunks.return_value = 5
    vs.update_library_metadata.return_value = None
    return vs


def _make_proc_result(status: str, chunks: int = 3):
    """Return a minimal ProcessingResult-like object."""
    r = MagicMock()
    r.status = status
    r.chunks_written = chunks
    r.error_detail = None
    return r


# ---------------------------------------------------------------------------
# DocumentUploadResult model field tests
# ---------------------------------------------------------------------------

class TestDocumentUploadResultField(unittest.TestCase):
    """item_vector_pending must default to False and accept True."""

    def test_default_is_false(self):
        result = DocumentUploadResult(
            library_id="lib1", item_key="K1", attachment_key="A1",
            chunks_added=0, status="skipped_duplicate",
        )
        self.assertFalse(result.item_vector_pending)

    def test_can_be_set_true(self):
        result = DocumentUploadResult(
            library_id="lib1", item_key="K1", attachment_key="A1",
            chunks_added=5, status="indexed",
            item_vector_pending=True,
        )
        self.assertTrue(result.item_vector_pending)

    def test_serialises_in_json(self):
        result = DocumentUploadResult(
            library_id="lib1", item_key="K1", attachment_key="A1",
            chunks_added=5, status="indexed",
            item_vector_pending=True,
        )
        data = result.model_dump()
        self.assertIn("item_vector_pending", data)
        self.assertTrue(data["item_vector_pending"])


# ---------------------------------------------------------------------------
# Sync upload endpoint (/api/index/document)
# ---------------------------------------------------------------------------

class TestSyncUploadItemVectorPending(unittest.TestCase):

    def setUp(self):
        from backend.main import app
        from backend.dependencies import get_vector_store
        self.app = app
        self.get_vector_store = get_vector_store
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _post(self, pdf_bytes: bytes = b"%PDF fake"):
        return self.client.post(
            "/api/index/document",
            files={"file": ("test.pdf", BytesIO(pdf_bytes), "application/pdf")},
            data={"metadata": _METADATA},
        )

    def test_indexed_fresh_sets_item_vector_pending_true(self):
        """A freshly indexed document must return item_vector_pending=True."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()

        with (
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
        ):
            mock_emb = MagicMock()
            mock_emb.rate_limit_retries = 0
            mock_emb.get_rate_limit_info = AsyncMock(return_value=None)
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = AsyncMock(
                return_value=_make_proc_result("indexed_fresh", chunks=3)
            )

            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "indexed")
        self.assertTrue(data["item_vector_pending"], "indexed response must have item_vector_pending=True")

    def test_duplicate_sets_item_vector_pending_false(self):
        """A content-hash duplicate must return item_vector_pending=False."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store(is_duplicate=True)

        with patch("backend.api.document_upload._check_registration"):
            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "skipped_duplicate")
        self.assertFalse(data["item_vector_pending"], "duplicate response must have item_vector_pending=False")

    def test_error_sets_item_vector_pending_false(self):
        """An error response must return item_vector_pending=False."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()

        with (
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
        ):
            mock_emb = MagicMock()
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = AsyncMock(
                side_effect=RuntimeError("embedding service unavailable")
            )

            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["item_vector_pending"], "error response must have item_vector_pending=False")

    def test_skipped_empty_sets_item_vector_pending_false(self):
        """A skipped_empty result (no text extracted) must return item_vector_pending=False."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()

        with (
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
        ):
            mock_emb = MagicMock()
            mock_emb.rate_limit_retries = 0
            mock_emb.get_rate_limit_info = AsyncMock(return_value=None)
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = AsyncMock(
                return_value=_make_proc_result("skipped_empty", chunks=0)
            )

            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "skipped_empty")
        self.assertFalse(data["item_vector_pending"], "skipped_empty response must have item_vector_pending=False")


# ---------------------------------------------------------------------------
# Abstract index endpoint (/api/index/abstract)
# ---------------------------------------------------------------------------

_ABSTRACT_PAYLOAD = {
    "library_id": "lib1",
    "library_type": "user",
    "item_key": "ITEM002",
    "item_version": 1,
    "title": "Abstract Test Item",
    "authors": ["Doe, J."],
    "year": 2023,
    "item_type": "journalArticle",
    "zotero_modified": "2023-01-01T00:00:00Z",
    "abstract_text": (
        "This study investigates the relationship between machine learning models and "
        "information retrieval in academic research libraries. We present a novel approach "
        "that combines dense vector embeddings with traditional keyword search to improve "
        "recall and precision for scientific literature discovery. Our experiments on a "
        "corpus of over one hundred thousand academic papers demonstrate statistically "
        "significant improvements over baseline retrieval methods. The proposed system "
        "integrates seamlessly with existing reference management workflows and requires "
        "minimal configuration from end users. Results suggest that hybrid retrieval "
        "strategies outperform single-modality approaches across all evaluated domains. "
        "Future work will explore multilingual retrieval and cross-domain transfer learning "
        "to further broaden the applicability of the proposed framework."
    ),
    "library_name": "Test Library",
}


class TestAbstractIndexItemVectorPending(unittest.TestCase):

    def setUp(self):
        from backend.main import app
        from backend.dependencies import get_vector_store
        self.app = app
        self.get_vector_store = get_vector_store
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _post(self, payload: dict | None = None):
        return self.client.post(
            "/api/index/abstract",
            json=payload or _ABSTRACT_PAYLOAD,
        )

    def test_abstract_indexed_sets_item_vector_pending_true(self):
        """Freshly indexed abstract must return item_vector_pending=True."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()

        with (
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
        ):
            mock_emb = MagicMock()
            mock_emb.rate_limit_retries = 0
            mock_emb.get_rate_limit_info = AsyncMock(return_value=None)
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            # _index_from_abstract returns a chunk count (int)
            mock_proc._index_from_abstract = AsyncMock(return_value=2)

            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "indexed")
        self.assertTrue(data["item_vector_pending"], "indexed abstract must have item_vector_pending=True")

    def test_abstract_zero_chunks_sets_item_vector_pending_false(self):
        """Abstract returning 0 chunks (treated as duplicate) must return item_vector_pending=False."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()

        with (
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
        ):
            mock_emb = MagicMock()
            mock_emb.rate_limit_retries = 0
            mock_emb.get_rate_limit_info = AsyncMock(return_value=None)
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._index_from_abstract = AsyncMock(return_value=0)

            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "skipped_duplicate")
        self.assertFalse(data["item_vector_pending"], "skipped abstract must have item_vector_pending=False")

    def test_abstract_error_sets_item_vector_pending_false(self):
        """An error during abstract indexing must return item_vector_pending=False."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()

        with (
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
        ):
            mock_emb = MagicMock()
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._index_from_abstract = AsyncMock(
                side_effect=RuntimeError("embedding service failed")
            )

            resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["item_vector_pending"], "error abstract response must have item_vector_pending=False")


if __name__ == "__main__":
    unittest.main()
