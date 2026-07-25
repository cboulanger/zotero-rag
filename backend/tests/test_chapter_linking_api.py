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


class TestRetrofitEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_returns_job_id_immediately(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "ambiguous": [], "no_match": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())


if __name__ == "__main__":
    unittest.main()
