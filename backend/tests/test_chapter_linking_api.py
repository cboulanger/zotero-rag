"""Unit tests for backend.api.chapter_linking (job-polling endpoint)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.api import chapter_linking
from backend.config.settings import get_settings, reset_settings
from backend.dependencies import require_authorized_group_admin
from backend.main import app
from backend.services import review_queue_store
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
from backend.zotero.group_roles import reset_admin_role_cache
from backend.zotero.key_validator import KeyValidation


class TestJobPolling(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unknown_job_returns_404(self):
        response = self.client.get("/api/chapter-linking/jobs/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_known_job_returns_status(self):
        job_id = chapter_linking.tracker.create()
        chapter_linking.tracker.update(job_id, progress=0.5, message="working")
        response = self.client.get(f"/api/chapter-linking/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progress"], 0.5)
        self.assertEqual(body["message"], "working")


class TestAnalyzeEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_returns_job_id_immediately(self, mock_run, mock_web_api):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())

    @patch("backend.api.chapter_linking.make_llm_service")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_passes_llm_service_when_fallback_enabled(self, mock_run, mock_web_api, mock_make_llm_service):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        fake_llm = object()
        mock_make_llm_service.return_value = fake_llm
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key", "enable_llm_fallback": True},
        )
        self.assertEqual(response.status_code, 200)
        mock_make_llm_service.assert_called_once()
        self.assertIs(mock_run.call_args.kwargs["llm_service"], fake_llm)

    @patch("backend.api.chapter_linking.make_llm_service")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_skips_llm_service_by_default(self, mock_run, mock_web_api, mock_make_llm_service):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key"},
        )
        self.assertEqual(response.status_code, 200)
        mock_make_llm_service.assert_not_called()
        self.assertIsNone(mock_run.call_args.kwargs["llm_service"])

    @patch("backend.api.chapter_linking.make_llm_service")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_passes_auto_select_model_flag_through(self, mock_run, mock_web_api, mock_make_llm_service):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={
                "library_slug": "groups/1", "api_key": "fake-key",
                "enable_llm_fallback": True, "auto_select_model": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        mock_make_llm_service.assert_called_once_with(auto_select_model=True)

    @patch("backend.api.chapter_linking.make_llm_service")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_auto_select_model_defaults_to_false(self, mock_run, mock_web_api, mock_make_llm_service):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key", "enable_llm_fallback": True},
        )
        self.assertEqual(response.status_code, 200)
        mock_make_llm_service.assert_called_once_with(auto_select_model=False)

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_passes_ocr_cache_dir_through_with_default(self, mock_run, mock_web_api):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(mock_run.call_args.kwargs["ocr_cache_dir"]), "data/ocr_cache")


class TestRetrofitEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_returns_job_id_immediately(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_defaults_to_dry_run(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(mock_run.call_args.kwargs["commit"])

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_passes_committed_flag_through(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key", "committed": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_run.call_args.kwargs["commit"])

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_passes_would_link_through(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        would_link = [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}]
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key", "committed": True, "would_link": would_link},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_run.call_args.kwargs["would_link"], would_link)

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_would_link_defaults_to_none(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(mock_run.call_args.kwargs["would_link"])

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_passes_target_collection_through(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key", "target_collection": "Book Chapters"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_run.call_args.kwargs["target_collection"], "Book Chapters")

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_target_collection_defaults_to_none(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(mock_run.call_args.kwargs["target_collection"])


class TestOcrEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.create_document_extractor")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.ocr_run", new_callable=AsyncMock)
    def test_returns_job_id_immediately(self, mock_run, mock_web_api, mock_create_extractor):
        mock_run.return_value = {"results": []}
        response = self.client.post(
            "/api/chapter-linking/ocr",
            json={
                "library_slug": "groups/1",
                "api_key": "fake-key",
                "attachment_specs": [{"item_key": "B1", "attachment_key": "A1"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())


class TestSegmentUploadEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.upload_run", new_callable=AsyncMock)
    def test_defaults_to_dry_run(self, mock_run, mock_zotero_module, mock_web_api):
        mock_run.return_value = {"would_create": [], "created": [], "skipped_low_confidence": []}
        response = self.client.post(
            "/api/chapter-linking/segment-upload",
            json={"library_slug": "groups/1", "api_key": "fake-key", "analyses": []},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
        _, kwargs = mock_run.call_args
        self.assertFalse(kwargs["commit"])


class TestReviewEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.review_queue_path = Path(self.tmp.name) / "review_queue.json"
        s.authorized_group_id = 999
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _override_admin(self):
        app.dependency_overrides[require_authorized_group_admin] = lambda: ZoteroIdentity(
            user_id=1, username="admin", targets=["groups/1"]
        )

    def test_lists_pending_entries_for_library(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {"book_key": "BOOK1", "attachment_key": "ATT1"}},
        ])
        response = self.client.get("/api/chapter-linking/review/pending", params={"library_slug": "groups/1"})
        self.assertEqual(response.status_code, 200)
        entries = response.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["queue_id"], "ocr:ATT1")

    def test_filters_by_bucket(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {}},
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit", "payload": {}},
        ])
        response = self.client.get(
            "/api/chapter-linking/review/pending", params={"library_slug": "groups/1", "bucket": "commit"}
        )
        entries = response.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["queue_id"], "match:CHAP1")

    def test_rejects_non_admin(self):
        get_settings().api_host = "rag.example.com"
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            response = self.client.get(
                "/api/chapter-linking/review/pending",
                params={"library_slug": "groups/1"},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(response.status_code, 403)

    def test_approve_chapter_entry_calls_upload_run_and_marks_approved(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "chapter:BOOK1:2-4", "type": "chapter", "bucket": "review",
             "payload": {"book_key": "BOOK1", "attachment_key": "ATT1", "title": "T", "authors": [],
                         "pdf_start_index": 2, "pdf_end_index": 4, "citation_pages": None,
                         "confidence": 0.7, "target_collection": "Book Chapters"}},
        ])
        with patch("backend.api.chapter_linking.upload_run", new=AsyncMock(
            return_value={"created": [{"book_key": "BOOK1", "chapter_key": "CHAP1"}], "failed": []}
        )) as mock_upload:
            response = self.client.post(
                "/api/chapter-linking/review/chapter:BOOK1:2-4/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        self.assertEqual(call_kwargs["confidence_threshold"], 0.0)
        self.assertEqual(call_kwargs["analyses"][0]["chapters"][0]["title"], "T")
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "chapter:BOOK1:2-4")
        self.assertEqual(entry["status"], "approved")

    def test_approve_chapter_entry_applies_edits(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "chapter:BOOK1:2-4", "type": "chapter", "bucket": "review",
             "payload": {"book_key": "BOOK1", "attachment_key": "ATT1", "title": "Original", "authors": [],
                         "pdf_start_index": 2, "pdf_end_index": 4, "citation_pages": None,
                         "confidence": 0.7, "target_collection": "Book Chapters"}},
        ])
        with patch("backend.api.chapter_linking.upload_run", new=AsyncMock(
            return_value={"created": [], "failed": []}
        )) as mock_upload:
            self.client.post(
                "/api/chapter-linking/review/chapter:BOOK1:2-4/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "title": "Edited Title", "pdf_end_index": 5},
            )
        chapter = mock_upload.call_args.kwargs["analyses"][0]["chapters"][0]
        self.assertEqual(chapter["title"], "Edited Title")
        self.assertEqual(chapter["pdf_start_index"], 2)
        self.assertEqual(chapter["pdf_end_index"], 5)

    def test_approve_match_entry_requires_book_key_for_ambiguous(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "review",
             "payload": {"chapter_key": "CHAP1", "candidates": [{"key": "BOOK1", "title": "T", "year": 2020}]}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/match:CHAP1/approve",
            json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
        )
        self.assertEqual(response.status_code, 502)

    def test_approve_match_entry_with_book_key_calls_commit_links(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "review",
             "payload": {"chapter_key": "CHAP1", "candidates": [{"key": "BOOK1", "title": "T", "year": 2020}]}},
        ])
        with patch("backend.api.chapter_linking.commit_links", return_value={"linked": [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}], "failed": []}) as mock_commit:
            response = self.client.post(
                "/api/chapter-linking/review/match:CHAP1/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "book_key": "BOOK1"},
            )
        self.assertEqual(response.status_code, 200)
        mock_commit.assert_called_once()
        would_link = mock_commit.call_args.args[2]
        self.assertEqual(would_link, [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

    def test_approve_ocr_entry_runs_ocr_and_reanalyzes_then_removes_entry(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review",
             "payload": {"book_key": "BOOK1", "attachment_key": "ATT1"}},
        ])
        with patch("backend.api.chapter_linking.ocr_run", new=AsyncMock(return_value={"results": []})), \
             patch("backend.api.chapter_linking.analyze_run", new=AsyncMock(return_value={"slug": "groups/1", "attachments": []})) as mock_analyze:
            response = self.client.post(
                "/api/chapter-linking/review/ocr:ATT1/approve",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT1"))
        self.assertEqual(mock_analyze.call_args.kwargs["ocr_cache_dir"], Path("data/ocr_cache"))

    def test_approve_unknown_queue_id_returns_404(self):
        self._override_admin()
        response = self.client.post(
            "/api/chapter-linking/review/does-not-exist/approve",
            json={"library_slug": "groups/1", "api_key": "WRITE-KEY"},
        )
        self.assertEqual(response.status_code, 404)

    def test_reject_marks_entry_rejected(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/ocr:ATT1/reject", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 200)
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT1")
        self.assertEqual(entry["status"], "rejected")

    def test_reject_unknown_queue_id_returns_404(self):
        self._override_admin()
        response = self.client.post(
            "/api/chapter-linking/review/does-not-exist/reject", params={"library_slug": "groups/1"}
        )
        self.assertEqual(response.status_code, 404)

    def test_execute_runs_each_commit_entry_and_marks_approved(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit",
             "payload": {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 0.95}},
        ])
        with patch("backend.api.chapter_linking.commit_links", return_value={"linked": [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 0.95}], "failed": []}):
            response = self.client.post(
                "/api/chapter-linking/review/execute",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "queue_ids": ["match:CHAP1"]},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["executed"], ["match:CHAP1"])
        self.assertEqual(body["failed"], [])
        entry = review_queue_store.get_entry(get_settings().review_queue_path, "groups/1", "match:CHAP1")
        self.assertEqual(entry["status"], "approved")

    def test_execute_isolates_per_item_failures(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit",
             "payload": {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 0.95}},
        ])
        with patch("backend.api.chapter_linking.commit_links", side_effect=RuntimeError("boom")):
            response = self.client.post(
                "/api/chapter-linking/review/execute",
                json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "queue_ids": ["match:CHAP1"]},
            )
        body = response.json()
        self.assertEqual(body["executed"], [])
        self.assertEqual(len(body["failed"]), 1)
        self.assertEqual(body["failed"][0]["queue_id"], "match:CHAP1")

    def test_execute_rejects_review_bucket_entries(self):
        self._override_admin()
        review_queue_store.upsert_many(get_settings().review_queue_path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "review",
             "payload": {"chapter_key": "CHAP1", "candidates": []}},
        ])
        response = self.client.post(
            "/api/chapter-linking/review/execute",
            json={"library_slug": "groups/1", "api_key": "WRITE-KEY", "queue_ids": ["match:CHAP1"]},
        )
        body = response.json()
        self.assertEqual(body["executed"], [])
        self.assertEqual(len(body["failed"]), 1)


if __name__ == "__main__":
    unittest.main()
