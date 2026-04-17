"""
Integration tests for API endpoints.
"""

import unittest
from fastapi.testclient import TestClient
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.main import app


class TestConfigAPI(unittest.TestCase):
    """Test configuration API endpoints."""

    def setUp(self):
        self.client = TestClient(app)

    def test_get_config(self):
        """Test GET /api/config endpoint."""
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertIn("preset_name", data)
        self.assertIn("api_version", data)
        self.assertIn("embedding_model", data)
        self.assertIn("llm_model", data)
        self.assertIn("available_presets", data)

    def test_get_version(self):
        """Test GET /api/version endpoint."""
        response = self.client.get("/api/version")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertIn("api_version", data)
        self.assertIn("service", data)

    def test_update_config_invalid_preset(self):
        """Test POST /api/config with invalid preset."""
        response = self.client.post(
            "/api/config",
            json={"preset_name": "invalid-preset"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid preset", response.json()["detail"])


class TestLibrariesAPI(unittest.TestCase):
    """Test libraries API endpoints."""

    def setUp(self):
        self.client = TestClient(app)

    def test_get_library_status(self):
        """Test GET /api/libraries/{library_id}/status endpoint."""
        response = self.client.get("/api/libraries/1/status")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["library_id"], "1")
        self.assertIn("indexed", data)


class TestPullIndexingRemoved(unittest.TestCase):
    """Verify that pull-based indexing endpoints return 410 Gone."""

    def setUp(self):
        self.client = TestClient(app)

    def test_start_library_indexing_removed(self):
        """POST /api/index/library/{id} must return 410 — pull model removed."""
        response = self.client.post("/api/index/library/1")
        self.assertEqual(response.status_code, 410)

    def test_progress_stream_removed(self):
        """GET /api/index/library/{id}/progress must return 410."""
        response = self.client.get("/api/index/library/1/progress")
        self.assertEqual(response.status_code, 410)

    def test_cancel_indexing_removed(self):
        """POST /api/index/library/{id}/cancel must return 410."""
        response = self.client.post("/api/index/library/1/cancel")
        self.assertEqual(response.status_code, 410)


class TestQueryAPI(unittest.TestCase):
    """Test query API endpoints."""

    def setUp(self):
        self.client = TestClient(app)

    def test_query_empty_question(self):
        """Test POST /api/query with empty question."""
        response = self.client.post(
            "/api/query",
            json={"question": "", "library_ids": ["1"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be empty", response.json()["detail"])

    def test_query_no_libraries(self):
        """Test POST /api/query with no libraries."""
        response = self.client.post(
            "/api/query",
            json={"question": "What is RAG?", "library_ids": []}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("At least one library", response.json()["detail"])


class TestRootEndpoints(unittest.TestCase):
    """Test root and health check endpoints."""

    def setUp(self):
        self.client = TestClient(app)

    def test_root(self):
        """Test GET / endpoint."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertIn("service", data)
        self.assertIn("version", data)
        self.assertEqual(data["status"], "running")

    def test_health_check(self):
        """Test GET /health endpoint."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")


if __name__ == '__main__':
    unittest.main()
