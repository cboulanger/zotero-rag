"""
Unit tests for the async upload flow:
- POST /api/index/document/async
- GET /api/index/tasks/{task_id}
- _run_task progress_message callback wiring
"""

import json
import time
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.api.document_upload import (
    _UploadTask,
    _upload_tasks,
    _upload_tasks_lock,
    DocumentUploadResult,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_METADATA = json.dumps({
    "library_id": "lib1",
    "item_key": "ITEM001",
    "attachment_key": "ATT001",
    "mime_type": "application/pdf",
    "item_version": 1,
    "attachment_version": 1,
})


def _mock_vector_store(*, is_duplicate: bool = False) -> MagicMock:
    vs = MagicMock()
    vs.check_duplicate.return_value = is_duplicate
    vs.get_item_version.return_value = None
    vs.get_library_metadata.return_value = None
    vs.count_library_chunks.return_value = 0
    return vs


# ---------------------------------------------------------------------------
# POST /api/index/document/async  +  GET /api/index/tasks/{task_id}
# ---------------------------------------------------------------------------

class TestAsyncUploadEndpoint(unittest.TestCase):

    def setUp(self):
        from backend.main import app
        from backend.dependencies import get_vector_store
        self.app = app
        self.get_vector_store = get_vector_store
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        with _upload_tasks_lock:
            _upload_tasks.clear()

    def _post(self, pdf_bytes: bytes = b"%PDF fake"):
        return self.client.post(
            "/api/index/document/async",
            files={"file": ("test.pdf", BytesIO(pdf_bytes), "application/pdf")},
            data={"metadata": _METADATA},
        )

    # -- POST tests -------------------------------------------------------

    def test_duplicate_returns_immediately_no_task_id(self):
        """Content-hash duplicate resolves instantly; no background task is created."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store(is_duplicate=True)
        with patch("backend.api.document_upload._check_registration"):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "skipped_duplicate")
        self.assertIsNone(data["task_id"])
        self.assertIsNotNone(data["result"])
        self.assertEqual(data["result"]["status"], "skipped_duplicate")

    def test_non_duplicate_returns_task_id_and_processing_status(self):
        """Non-duplicate upload returns status=processing with a non-null task_id."""
        self.app.dependency_overrides[self.get_vector_store] = lambda: _mock_vector_store()
        def _discard_task(coro):
            coro.close()  # prevent "coroutine never awaited" warning
            return MagicMock()

        with (
            patch("backend.api.document_upload.make_embedding_service"),
            patch("backend.api.document_upload._check_registration"),
            patch("backend.api.document_upload.asyncio.create_task", side_effect=_discard_task),
        ):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "processing")
        self.assertIsNotNone(data["task_id"])
        self.assertIsNone(data["result"])

    # -- GET /api/index/tasks/{task_id} tests -----------------------------

    def test_poll_unknown_task_returns_404(self):
        resp = self.client.get("/api/index/tasks/nonexistent-task-id")
        self.assertEqual(resp.status_code, 404)

    def test_poll_processing_task_returns_status_and_progress_message(self):
        """Processing task: poll must echo status=processing and any progress_message."""
        task_id = "task-in-progress"
        with _upload_tasks_lock:
            _upload_tasks[task_id] = _UploadTask(
                status="processing",
                result=None,
                created_at=time.monotonic(),
                progress_message="Extracting text (part 2/5)...",
            )
        resp = self.client.get(f"/api/index/tasks/{task_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "processing")
        self.assertIsNone(data["result"])
        self.assertEqual(data["progress_message"], "Extracting text (part 2/5)...")

    def test_poll_processing_task_with_no_progress_yet(self):
        """Processing task with no progress yet returns progress_message=null."""
        task_id = "task-no-progress-yet"
        with _upload_tasks_lock:
            _upload_tasks[task_id] = _UploadTask(
                status="processing",
                result=None,
                created_at=time.monotonic(),
            )
        resp = self.client.get(f"/api/index/tasks/{task_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data["progress_message"])

    def test_poll_done_task_returns_result(self):
        """Completed task: poll returns status=done with the full DocumentUploadResult."""
        task_id = "task-done"
        result = DocumentUploadResult(
            library_id="lib1",
            item_key="ITEM001",
            attachment_key="ATT001",
            chunks_added=7,
            status="indexed",
            message="indexed: 7 chunks",
        )
        with _upload_tasks_lock:
            _upload_tasks[task_id] = _UploadTask(
                status="done",
                result=result,
                created_at=time.monotonic(),
            )
        resp = self.client.get(f"/api/index/tasks/{task_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "done")
        self.assertIsNotNone(data["result"])
        self.assertEqual(data["result"]["chunks_added"], 7)
        self.assertEqual(data["result"]["status"], "indexed")

    def test_stale_task_pruned_on_poll(self):
        """Tasks older than 1 hour are pruned; the queried task returns 404."""
        stale_id = "task-stale"
        fresh_id = "task-fresh"
        with _upload_tasks_lock:
            _upload_tasks[stale_id] = _UploadTask(
                status="processing", result=None, created_at=time.monotonic() - 7201  # 2 hours ago
            )
            _upload_tasks[fresh_id] = _UploadTask(
                status="processing", result=None, created_at=time.monotonic()
            )
        resp = self.client.get(f"/api/index/tasks/{stale_id}")
        self.assertEqual(resp.status_code, 404)
        with _upload_tasks_lock:
            self.assertNotIn(stale_id, _upload_tasks, "stale task must be pruned")
            self.assertIn(fresh_id, _upload_tasks, "fresh task must survive pruning")


# ---------------------------------------------------------------------------
# _run_task — progress_message callback wiring
# ---------------------------------------------------------------------------

class TestRunTaskProgressCallback(unittest.IsolatedAsyncioTestCase):
    """Verifies that _run_task creates an on_progress callback that writes to progress_message."""

    def setUp(self):
        self.task_id = "task-progress-cb"
        with _upload_tasks_lock:
            _upload_tasks[self.task_id] = _UploadTask(
                status="processing",
                result=None,
                created_at=time.monotonic(),
            )

    def tearDown(self):
        with _upload_tasks_lock:
            _upload_tasks.pop(self.task_id, None)

    async def test_callback_updates_progress_message_and_task_completes(self):
        from backend.api.document_upload import _run_task

        emitted: list[str] = []

        async def fake_execute(**kwargs):
            on_progress = kwargs.get("on_progress")
            self.assertIsNotNone(on_progress, "_execute_upload must receive an on_progress callback")

            on_progress("Splitting PDF (12 MB)...")
            emitted.append("Splitting PDF (12 MB)...")
            # Verify the message is immediately visible in the task store
            with _upload_tasks_lock:
                self.assertEqual(_upload_tasks[self.task_id].progress_message, "Splitting PDF (12 MB)...")

            on_progress("Extracting text (part 1/3)...")
            emitted.append("Extracting text (part 1/3)...")

            return DocumentUploadResult(
                library_id="lib1",
                item_key="ITEM001",
                attachment_key="ATT001",
                chunks_added=3,
                status="indexed",
                message="indexed: 3 chunks",
            )

        dummy_kwargs = dict(
            file_bytes=b"fake",
            content_hash="abc123",
            doc_metadata=MagicMock(),
            library_id="lib1",
            library_type="user",
            item_key="ITEM001",
            attachment_key="ATT001",
            mime_type="application/pdf",
            item_version=1,
            attachment_version=1,
            item_modified="2026-01-01T00:00:00Z",
            library_name="Test Library",
            vector_store=MagicMock(),
            embedding_service=MagicMock(),
        )

        with patch("backend.api.document_upload._execute_upload", side_effect=fake_execute):
            await _run_task(self.task_id, **dummy_kwargs)

        with _upload_tasks_lock:
            task = _upload_tasks[self.task_id]
        self.assertEqual(task.status, "done")
        self.assertIsNotNone(task.result)
        self.assertEqual(task.result.chunks_added, 3)
        self.assertEqual(emitted, ["Splitting PDF (12 MB)...", "Extracting text (part 1/3)..."])


if __name__ == "__main__":
    unittest.main()
