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
from unittest.mock import AsyncMock, MagicMock, patch, call

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
    """VectorStore.add_chunks_batch must split large payloads into ≤100-point calls."""

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

    def test_single_batch_when_chunks_le_100(self):
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(80)]
        vs.add_chunks_batch(chunks)
        self.assertEqual(vs.client.upsert.call_count, 1)
        args, kwargs = vs.client.upsert.call_args
        self.assertEqual(len(kwargs.get("points", args[1] if len(args) > 1 else [])), 80)

    def test_splits_into_multiple_batches(self):
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(250)]
        vs.add_chunks_batch(chunks)
        # 250 chunks → 3 calls: 100 + 100 + 50
        self.assertEqual(vs.client.upsert.call_count, 3)
        sizes = [
            len(c.kwargs.get("points", c.args[1] if len(c.args) > 1 else []))
            for c in vs.client.upsert.call_args_list
        ]
        self.assertEqual(sizes, [100, 100, 50])

    def test_exact_multiple_of_batch_size(self):
        vs = self._make_store_with_mock_client()
        chunks = [_make_chunk(i) for i in range(200)]
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


# ---------------------------------------------------------------------------
# Orphaned deduplication-record self-healing
#
# A dedup record can outlive its chunks (e.g. a chunk-collection wipe leaves the
# separate `deduplication` collection intact).  When that happens, check_duplicate
# matches but check-indexed / count_indexed_items see no chunks, so the item is
# both "already indexed" (dedup hit) and "not indexed" (no chunks) — permanently
# un-reindexable.  The upload path must detect this, purge the stale record, and
# proceed to index instead of returning skipped_duplicate.
# ---------------------------------------------------------------------------

class TestOrphanedDedupSelfHeal(unittest.TestCase):
    _METADATA = json.dumps({
        "library_id": "lib1",
        "item_key": "ITEM001",
        "attachment_key": "ATT001",
        "mime_type": "application/pdf",
        "item_version": 5,
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

    def _dedup_record(self, item_key="ITEM001"):
        from backend.models.document import DeduplicationRecord
        return DeduplicationRecord(
            content_hash="hash123",
            library_id="lib1",
            item_key=item_key,
            relation_uri=None,
        )

    def _post(self, pdf_bytes=b"%PDF fake"):
        return self.client.post(
            "/api/index/document",
            files={"file": ("test.pdf", BytesIO(pdf_bytes), "application/pdf")},
            data={"metadata": self._METADATA},
        )

    def _run(self, mock_vs):
        from backend.services.document_processor import AttachmentProcessingResult
        self.app.dependency_overrides[self.get_vector_store] = lambda: mock_vs
        with (
            patch("backend.api.document_upload.make_embedding_service") as mock_emb_factory,
            patch("backend.api.document_upload.DocumentProcessor") as mock_proc_cls,
            patch("backend.api.document_upload._check_registration"),
        ):
            mock_emb = MagicMock()
            mock_emb.rate_limit_retries = 0
            mock_emb.get_rate_limit_info = AsyncMock(return_value=None)
            mock_emb_factory.return_value = mock_emb

            mock_proc = MagicMock()
            mock_proc_cls.return_value = mock_proc
            mock_proc._process_attachment_bytes = AsyncMock(
                return_value=AttachmentProcessingResult(chunks_written=7, status="indexed_fresh")
            )
            self._mock_proc = mock_proc
            return self._post()

    def test_orphaned_dedup_record_is_purged_and_item_reindexed(self):
        """Dedup hit but no chunks for the item → purge stale record, index fresh."""
        mock_vs = MagicMock()
        mock_vs.check_duplicate.return_value = self._dedup_record()
        # No chunks exist for the dedup record's item → orphaned record.
        mock_vs.get_item_version.return_value = None
        mock_vs.get_library_metadata.return_value = None
        mock_vs.count_library_chunks.return_value = 7

        resp = self._run(mock_vs)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "indexed")
        self.assertEqual(data["chunks_added"], 7)
        # The stale dedup record must have been purged for the orphaned item.
        mock_vs.delete_item_deduplication_records.assert_called_once_with("lib1", "ITEM001")
        # The document must actually have been processed (not short-circuited).
        self._mock_proc._process_attachment_bytes.assert_called_once()

    def test_genuine_duplicate_is_still_skipped(self):
        """Dedup hit AND chunks present for the item → keep skipping, don't reprocess."""
        mock_vs = MagicMock()
        mock_vs.check_duplicate.return_value = self._dedup_record()
        # Chunks exist for the item → genuine duplicate.
        mock_vs.get_item_version.return_value = 5

        resp = self._run(mock_vs)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "skipped_duplicate")
        mock_vs.delete_item_deduplication_records.assert_not_called()
        self._mock_proc._process_attachment_bytes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
