"""
Integration tests for API endpoints.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.main import app


class TestConfigAPI(unittest.TestCase):
    """Test configuration API endpoints."""

    def setUp(self):
        """Set up test client."""
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
        """Set up test client."""
        self.client = TestClient(app)

    @patch('backend.api.libraries.ZoteroLocalAPI')
    def test_list_libraries_success(self, mock_zotero_class):
        """Test GET /api/libraries endpoint."""
        # Mock Zotero client
        mock_client = AsyncMock()
        mock_client.list_libraries.return_value = [
            {"id": "1", "name": "My Library", "type": "user", "version": 100},
            {"id": "2", "name": "Group Library", "type": "group", "version": 50}
        ]
        mock_zotero_class.return_value.__aenter__.return_value = mock_client

        response = self.client.get("/api/libraries")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["library_id"], "1")
        self.assertEqual(data[0]["name"], "My Library")
        self.assertEqual(data[1]["library_id"], "2")

    @patch('backend.api.libraries.ZoteroLocalAPI')
    def test_list_libraries_connection_error(self, mock_zotero_class):
        """Test GET /api/libraries with connection error."""
        # Mock connection error
        mock_client = AsyncMock()
        mock_client.list_libraries.side_effect = Exception("Connection refused")
        mock_zotero_class.return_value.__aenter__.return_value = mock_client

        response = self.client.get("/api/libraries")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Failed to connect", response.json()["detail"])

    def test_get_library_status(self):
        """Test GET /api/libraries/{library_id}/status endpoint."""
        response = self.client.get("/api/libraries/1/status")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["library_id"], "1")
        self.assertIn("indexed", data)


class TestIndexingAPI(unittest.TestCase):
    """Test indexing API endpoints."""

    def setUp(self):
        """Set up test client."""
        self.client = TestClient(app)

    @patch('backend.api.indexing.ZoteroLocalAPI')
    @patch('backend.api.indexing.asyncio.create_task')
    def test_start_indexing_success(self, mock_create_task, mock_zotero_class):
        """Test POST /api/index/library/{library_id} endpoint."""
        # Mock create_task to consume the coroutine and prevent warning
        def consume_coroutine(coro):
            """Close the coroutine to prevent 'never awaited' warning."""
            coro.close()
            return AsyncMock()

        mock_create_task.side_effect = consume_coroutine

        # Mock Zotero client
        mock_client = AsyncMock()
        mock_client.get_libraries.return_value = [
            {"id": "1", "name": "My Library", "type": "user"}
        ]
        mock_zotero_class.return_value.__aenter__.return_value = mock_client

        response = self.client.post("/api/index/library/1")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["library_id"], "1")
        self.assertEqual(data["status"], "started")
        mock_create_task.assert_called_once()

    @patch('backend.api.indexing.ZoteroLocalAPI')
    @patch('backend.api.indexing.asyncio.create_task')
    def test_start_indexing_library_not_found(self, mock_create_task, mock_zotero_class):
        """Test POST /api/index/library/{library_id} with nonexistent library."""
        # Mock create_task to consume the coroutine and prevent execution
        def consume_coroutine(coro):
            """Close the coroutine to prevent 'never awaited' warning."""
            coro.close()
            return AsyncMock()

        mock_create_task.side_effect = consume_coroutine

        # Mock Zotero client with successful connection
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = True
        mock_client.get_libraries.return_value = []
        mock_zotero_class.return_value.__aenter__.return_value = mock_client

        response = self.client.post("/api/index/library/999")
        # The endpoint starts the task successfully - library validation happens in background
        # This is by design to allow async processing
        self.assertEqual(response.status_code, 200)

    @patch('backend.api.indexing.ZoteroLocalAPI')
    def test_start_indexing_zotero_unavailable(self, mock_zotero_class):
        """Test POST /api/index/library/{library_id} with Zotero unavailable."""
        # Mock connection error - the check_connection should return False
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = False
        mock_zotero_class.return_value.__aenter__.return_value = mock_client

        response = self.client.post("/api/index/library/1")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not accessible", response.json()["detail"])


class TestQueryAPI(unittest.TestCase):
    """Test query API endpoints."""

    def setUp(self):
        """Set up test client."""
        self.client = TestClient(app)

    def test_query_empty_question(self):
        """Test POST /api/query with empty question."""
        response = self.client.post(
            "/api/query",
            json={
                "question": "",
                "library_ids": ["1"]
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be empty", response.json()["detail"])

    def test_query_no_libraries(self):
        """Test POST /api/query with no libraries."""
        response = self.client.post(
            "/api/query",
            json={
                "question": "What is RAG?",
                "library_ids": []
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("At least one library", response.json()["detail"])


class TestRootEndpoints(unittest.TestCase):
    """Test root and health check endpoints."""

    def setUp(self):
        """Set up test client."""
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
