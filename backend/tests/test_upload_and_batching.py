"""
Unit tests for:
- VectorStore.add_chunks_batch batching behaviour (sub-batches of 200)
- upload_and_index_document timeout warning (no stacktrace on WriteTimeout /
  ResponseHandlingException)
"""

import json
import logging
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch, call

from fastapi.testclient import TestClient
from httpx import WriteTimeout

from backend.models.document import (
    DocumentChunk,
    ChunkMetadata,
    DocumentMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(index: int) -> DocumentChunk:
    return DocumentChunk(
        text=f"chunk {index}",
        metadata=ChunkMetadata(
            chunk_id=f"chunk-{index:04d}",
            document_metadata=DocumentMetadata(
                library_id="lib1",
                item_key="ITEM001",
                attachment_key="ATT001",
            ),
            page_number=1,
            text_preview=f"chunk {index}",
            chunk_index=index,
            content_hash=f"hash{index}",
        ),
        embedding=[0.1] * 384,
    )


# ---------------------------------------------------------------------------
# Batching tests
# ---------------------------------------------------------------------------

class TestAddChunksBatching(unittest.TestCase):
    """VectorStore.add_chunks_batch must split large payloads into ≤200-point calls."""

    def _make_store_with_mock_client(self):
        """Return a VectorStore whose qdrant client is fully mocked."""
        import tempfile, shutil
        from pathlib import Path
        from qdrant_client.models import Distance
        from backend.db.vector_store import VectorStore

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        vs = VectorStore(
            storage_path=Path(tmp) / "qdrant",
            embedding_dim=384,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )
        # Replace the real client with a mock AFTER init (collections already created)
        vs.client = MagicMock()
        return vs

    def test_single_batch_when_chunks_le_200(self):
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(150)]
        vs.add_chunks_batch(chunks)
        self.assertEqual(vs.client.upsert.call_count, 1)
        args, kwargs = vs.client.upsert.call_args
        self.assertEqual(len(kwargs.get("points", args[1] if len(args) > 1 else [])), 150)

    def test_splits_into_multiple_batches(self):
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(450)]
        vs.add_chunks_batch(chunks)
        # 450 chunks → 3 calls: 200 + 200 + 50
        self.assertEqual(vs.client.upsert.call_count, 3)
        sizes = [
            len(c.kwargs.get("points", c.args[1] if len(c.args) > 1 else []))
            for c in vs.client.upsert.call_args_list
        ]
        self.assertEqual(sizes, [200, 200, 50])

    def test_exact_multiple_of_batch_size(self):
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(400)]
        vs.add_chunks_batch(chunks)
        self.assertEqual(vs.client.upsert.call_count, 2)

    def test_all_points_are_sent(self):
        """No chunk is silently dropped across batches."""
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(550)]
        vs.add_chunks_batch(chunks)
        total_sent = sum(
            len(c.kwargs.get("points", c.args[1] if len(c.args) > 1 else []))
            for c in vs.client.upsert.call_args_list
        )
        self.assertEqual(total_sent, 550)


# ---------------------------------------------------------------------------
# Timeout warning tests
# ---------------------------------------------------------------------------

class TestUploadTimeoutWarning(unittest.TestCase):
    """upload_and_index_document must log a WARNING (not ERROR+stacktrace) on timeout."""

    _METADATA = json.dumps({
        "library_id": "lib1",
        "item_key": "ITEM001",
        "attachment_key": "ATT001",
        "mime_type": "application/pdf",
        "item_version": 1,
        "attachment_version": 1,
    })

    def setUp(self):
        from backend.main import app
        from backend.dependencies import get_vector_store
        self.app = app
        self.get_vector_store = get_vector_store
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _mock_vector_store(self):
        vs = MagicMock()
        vs.check_duplicate.return_value = None
        vs.get_item_version.return_value = None
        vs.get_library_metadata.return_value = None
        vs.count_library_chunks.return_value = 0
        return vs

    def _post(self, pdf_bytes=b"%PDF fake"):
        return self.client.post(
            "/api/index/document",
            files={"file": ("test.pdf", BytesIO(pdf_bytes), "application/pdf")},
            data={"metadata": self._METADATA},
        )

    def test_write_timeout_logs_warning_not_error(self):
        mock_vs = self._mock_vector_store()
        self.app.dependency_overrides[self.get_vector_store] = lambda: mock_vs

        with (
            patch("backend.api.document_upload.make_embedding_service") as mock_emb,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
            patch("backend.api.document_upload._check_registration"),
        ):
            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = MagicMock(
                side_effect=WriteTimeout("timed out")
            )

            with self.assertLogs("backend.api.document_upload", level="WARNING") as cm:
                resp = self._post()

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "error")

        # Must have a WARNING, must NOT have an ERROR
        levels = [r.split(":")[0] for r in cm.output]
        self.assertIn("WARNING", levels)
        self.assertNotIn("ERROR", levels)

    def test_qdrant_response_handling_exception_logs_warning(self):
        from qdrant_client.http.exceptions import ResponseHandlingException

        mock_vs = self._mock_vector_store()
        self.app.dependency_overrides[self.get_vector_store] = lambda: mock_vs

        with (
            patch("backend.api.document_upload.make_embedding_service"),
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
            patch("backend.api.document_upload._check_registration"),
        ):
            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = MagicMock(
                side_effect=ResponseHandlingException(WriteTimeout("timed out"))
            )

            with self.assertLogs("backend.api.document_upload", level="WARNING") as cm:
                resp = self._post()

        self.assertEqual(resp.status_code, 200)
        levels = [r.split(":")[0] for r in cm.output]
        self.assertIn("WARNING", levels)
        self.assertNotIn("ERROR", levels)

    def test_unexpected_exception_still_logs_error(self):
        mock_vs = self._mock_vector_store()
        self.app.dependency_overrides[self.get_vector_store] = lambda: mock_vs

        with (
            patch("backend.api.document_upload.make_embedding_service"),
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
            patch("backend.api.document_upload._check_registration"),
        ):
            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = MagicMock(
                side_effect=RuntimeError("something unexpected")
            )

            with self.assertLogs("backend.api.document_upload", level="WARNING") as cm:
                resp = self._post()

        self.assertEqual(resp.status_code, 200)
        levels = [r.split(":")[0] for r in cm.output]
        self.assertIn("ERROR", levels)


if __name__ == "__main__":
    unittest.main()
