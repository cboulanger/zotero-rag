"""Tests for the opt-in include_diagnostics upload flag."""

import json
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.api.document_upload import _execute_upload
from backend.models.document import DocumentMetadata
from backend.services import diagnostics_collector as dc
from backend.services.document_processor import AttachmentProcessingResult
from backend.services.extraction.kreuzberg import KreuzbergExtractor, KreuzbergTimeoutError


def _kwargs(**over):
    vs = MagicMock()
    vs.check_duplicate.return_value = None
    vs.get_item_version.return_value = None
    vs.get_library_metadata.return_value = None
    vs.count_library_chunks.return_value = 0
    emb = MagicMock()
    emb.rate_limit_retries = 0
    emb.get_rate_limit_info = AsyncMock(return_value=None)
    base = dict(
        file_bytes=b"%PDF fake", content_hash="a" * 64,
        doc_metadata=DocumentMetadata(library_id="lib", item_key="I", attachment_key="A", title="t"),
        library_id="lib", library_type="user", item_key="I", attachment_key="A",
        mime_type="application/pdf", item_version=3, attachment_version=1,
        item_modified="", library_name="", vector_store=vs, embedding_service=emb,
    )
    base.update(over)
    return base


def _patch_processor(result=None, exc=None):
    async def fake(self, **kw):
        with dc.stage("extraction") as st:
            st.set(mime_type=kw["mime_type"])
        if exc:
            raise exc
        return result
    return patch("backend.api.document_upload.DocumentProcessor._process_attachment_bytes", fake)


class TestExecuteUploadDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_default_has_no_diagnostics(self):
        with _patch_processor(AttachmentProcessingResult(chunks_written=2, status="indexed_fresh")):
            result = await _execute_upload(**_kwargs())
        self.assertIsNone(result.diagnostics)
        self.assertNotIn("diagnostics", {k for k, v in result.model_dump().items() if v is not None})

    async def test_success_payload_has_stages_and_server_allowlist(self):
        with _patch_processor(AttachmentProcessingResult(chunks_written=2, status="indexed_fresh")):
            result = await _execute_upload(include_diagnostics=True, **_kwargs())
        d = result.diagnostics
        self.assertEqual(d.final_status, "indexed")
        names = [s.name for s in d.stages]
        for expected in ("dedup_check", "stale_chunk_purge", "extraction", "library_metadata"):
            self.assertIn(expected, names)
        self.assertIsNone(d.error)
        blob = json.dumps(d.server).lower()
        self.assertNotIn("secret", blob)
        self.assertNotIn("api_key", blob)

    async def test_skipped_timeout_carries_status(self):
        res = AttachmentProcessingResult(chunks_written=0, status="skipped_timeout", error_detail="timed out")
        with _patch_processor(res):
            result = await _execute_upload(include_diagnostics=True, **_kwargs())
        self.assertEqual(result.status, "skipped_timeout")
        self.assertEqual(result.diagnostics.final_status, "skipped_timeout")

    async def test_error_result_still_carries_diagnostics(self):
        with _patch_processor(exc=RuntimeError("kreuzberg sidecar exploded")):
            result = await _execute_upload(include_diagnostics=True, **_kwargs())
        self.assertEqual(result.status, "error")
        d = result.diagnostics
        self.assertEqual(d.final_status, "error")
        self.assertEqual(d.error["type"], "RuntimeError")
        self.assertIn("exploded", d.error["traceback"])

    async def test_generic_error_carries_type_but_no_rate_limit_timestamp(self):
        with _patch_processor(exc=RuntimeError("kreuzberg sidecar exploded")):
            result = await _execute_upload(**_kwargs())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_type, "RuntimeError")
        self.assertIsNone(result.rate_limit_available_at)

    async def test_skipped_too_large_merges_into_library_metadata(self):
        # Surfaced to the plugin's Fix Unavailable tool via
        # LibraryIndexMetadata.last_scan_skipped_too_large, the same mechanism
        # used for download failures — covers both this endpoint and
        # CronIndexer._drain_pending_uploads, which both funnel through
        # _execute_upload_impl.
        res = AttachmentProcessingResult(chunks_written=0, status="skipped_too_large", error_detail="too big")
        kwargs = _kwargs()
        with _patch_processor(res):
            result = await _execute_upload(**kwargs)
        self.assertEqual(result.status, "skipped_too_large")
        saved_metadata = kwargs["vector_store"].update_library_metadata.call_args.args[0]
        self.assertEqual(
            saved_metadata.last_scan_skipped_too_large,
            [{"item_key": "I", "attachment_key": "A", "detail": "too big"}],
        )

    async def test_rate_limit_exhausted_error_carries_type_and_available_at(self):
        # CronIndexer._drain_pending_uploads detects an exhausted embedding quota
        # via these two fields (not by string-matching `message`) to stop
        # processing the rest of a pending-upload backlog instead of wastefully
        # re-extracting (and re-failing on) every remaining queued attachment.
        from datetime import datetime, timezone
        from backend.services.embeddings import EmbeddingRateLimitExhaustedError
        available_at = datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)
        with _patch_processor(exc=EmbeddingRateLimitExhaustedError("quota exhausted", available_at=available_at)):
            result = await _execute_upload(**_kwargs())
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_type, "EmbeddingRateLimitExhaustedError")
        self.assertEqual(result.rate_limit_available_at, available_at.isoformat())


class TestKreuzbergStageRecording(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_details_recorded(self):
        extractor = KreuzbergExtractor(timeout_cap=600)
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        c = dc.DiagnosticsCollector()
        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=cm):
            with dc.activate(c):
                with self.assertRaises(KreuzbergTimeoutError):
                    await extractor.extract_and_chunk(b"x" * 1000, "application/pdf", timeout_multiplier=2.0)
        stage = next(s for s in c.finalize().stages if s.name == "kreuzberg_request")
        self.assertEqual(stage.outcome, "error")
        self.assertEqual(stage.details["timeout_multiplier"], 2.0)
        self.assertEqual(stage.details["timeout_cap_seconds"], 600)
        self.assertEqual(stage.details["failure"], "timeout")

    async def test_works_without_collector(self):
        extractor = KreuzbergExtractor()
        client = MagicMock()
        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=[{"chunks": []}])
        client.post = AsyncMock(return_value=resp)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=cm):
            self.assertEqual(await extractor.extract_and_chunk(b"x", "text/html"), [])


class TestAsyncEndpointFlag(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        from backend.dependencies import get_vector_store
        self.app, self.gvs = app, get_vector_store
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _post(self, **data):
        from backend.models.document import DeduplicationRecord
        vs = MagicMock()
        vs.check_duplicate.return_value = DeduplicationRecord(
            content_hash="h", library_id="lib1", item_key="ITEM001", relation_uri=None)
        vs.get_item_version.return_value = 1
        self.app.dependency_overrides[self.gvs] = lambda: vs
        meta = json.dumps({"library_id": "lib1", "item_key": "ITEM001", "attachment_key": "A1"})
        return self.client.post(
            "/api/index/document/async",
            files={"file": ("t.pdf", BytesIO(b"%PDF"), "application/pdf")},
            data={"metadata": meta, **data},
        ).json()

    def test_duplicate_fast_path_omits_diagnostics_by_default(self):
        self.assertIsNone(self._post()["result"]["diagnostics"])

    def test_duplicate_fast_path_includes_diagnostics_when_requested(self):
        d = self._post(include_diagnostics="true")["result"]["diagnostics"]
        self.assertEqual(d["final_status"], "skipped_duplicate")
        self.assertEqual(d["stages"][0]["name"], "dedup_check")


if __name__ == "__main__":
    unittest.main()
