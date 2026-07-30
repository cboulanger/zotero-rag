"""Unit tests for backend.api.chapter_linking (job-polling endpoint)."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.api import chapter_linking
from backend.main import app


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


if __name__ == "__main__":
    unittest.main()
