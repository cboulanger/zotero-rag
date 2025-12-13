"""
Tests for sync API endpoints.
"""

import unittest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend.api.sync import router, get_sync_service
from backend.services.vector_sync import SyncStatus


class TestSyncAPI(unittest.TestCase):
    """Test sync API endpoints."""

    def setUp(self):
        """Set up test fixtures."""
        # Create FastAPI test app
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api")
        self.client = TestClient(self.app)

        # Mock sync service
        self.mock_sync_service = AsyncMock()

    @patch('backend.api.sync.get_settings')
    def test_check_sync_enabled_true(self, mock_get_settings):
        """Test checking sync enabled when enabled."""
        mock_settings = Mock()
        mock_settings.sync_enabled = True
        mock_settings.sync_backend = "webdav"
        mock_settings.sync_auto_pull = True
        mock_settings.sync_auto_push = False
        mock_get_settings.return_value = mock_settings

        response = self.client.get("/api/vectors/sync/enabled")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["backend"], "webdav")
        self.assertTrue(data["auto_pull"])
        self.assertFalse(data["auto_push"])

    @patch('backend.api.sync.get_settings')
    def test_check_sync_enabled_false(self, mock_get_settings):
        """Test checking sync enabled when disabled."""
        mock_settings = Mock()
        mock_settings.sync_enabled = False
        mock_get_settings.return_value = mock_settings

        response = self.client.get("/api/vectors/sync/enabled")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["enabled"])
        self.assertIsNone(data["backend"])

    @patch('backend.api.sync.get_sync_service')
    def test_list_remote_libraries_success(self, mock_get_service):
        """Test listing remote libraries successfully."""
        # Mock sync service
        self.mock_sync_service.list_remote_libraries.return_value = [
            {
                "library_id": "123",
                "library_version": 100,
                "snapshot_file": "library_123_v100.tar.gz",
                "uploaded_at": "2025-01-01T00:00:00Z",
                "total_chunks": 1000,
                "total_items": 50,
            },
            {
                "library_id": "456",
                "library_version": 200,
                "snapshot_file": "library_456_v200.tar.gz",
                "uploaded_at": "2025-01-02T00:00:00Z",
                "total_chunks": 2000,
                "total_items": 100,
            },
        ]
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.get("/api/vectors/remote")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["libraries"]), 2)
        self.assertEqual(data["libraries"][0]["library_id"], "123")
        self.assertEqual(data["libraries"][1]["library_id"], "456")

    @patch('backend.api.sync.get_sync_service')
    def test_list_remote_libraries_sync_disabled(self, mock_get_service):
        """Test listing remote libraries when sync is disabled."""
        mock_get_service.return_value = None

        response = self.client.get("/api/vectors/remote")

        self.assertEqual(response.status_code, 400)
        self.assertIn("not enabled", response.json()["detail"])

    @patch('backend.api.sync.get_sync_service')
    def test_get_sync_status_success(self, mock_get_service):
        """Test getting sync status successfully."""
        self.mock_sync_service.get_sync_status.return_value = {
            "local_exists": True,
            "remote_exists": True,
            "local_version": 100,
            "remote_version": 200,
            "sync_status": SyncStatus.REMOTE_NEWER,
            "local_chunks": 500,
            "remote_chunks": 1000,
            "local_last_indexed": "2025-01-01T00:00:00Z",
            "remote_uploaded_at": "2025-01-02T00:00:00Z",
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.get("/api/vectors/123/sync-status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["library_id"], "123")
        self.assertTrue(data["local_exists"])
        self.assertTrue(data["remote_exists"])
        self.assertEqual(data["sync_status"], SyncStatus.REMOTE_NEWER)
        self.assertTrue(data["needs_pull"])
        self.assertFalse(data["needs_push"])

    @patch('backend.api.sync.get_sync_service')
    def test_pull_library_success(self, mock_get_service):
        """Test pulling library successfully."""
        self.mock_sync_service.pull_library.return_value = {
            "success": True,
            "message": "Successfully pulled library",
            "downloaded_bytes": 1024000,
            "restore_time": 5.5,
            "chunks_restored": 1000,
            "library_version": 100,
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/pull")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["operation"], "pull")
        self.assertEqual(data["library_id"], "123")
        self.assertEqual(data["chunks_restored"], 1000)
        self.assertEqual(data["library_version"], 100)

    @patch('backend.api.sync.get_sync_service')
    def test_pull_library_with_force(self, mock_get_service):
        """Test pulling library with force flag."""
        self.mock_sync_service.pull_library.return_value = {
            "success": True,
            "message": "Successfully pulled library",
            "downloaded_bytes": 1024000,
            "restore_time": 5.5,
            "chunks_restored": 1000,
            "library_version": 100,
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/pull?force=true")

        self.assertEqual(response.status_code, 200)
        # Verify force=True was passed
        self.mock_sync_service.pull_library.assert_called_once_with("123", force=True)

    @patch('backend.api.sync.get_sync_service')
    def test_push_library_success(self, mock_get_service):
        """Test pushing library successfully."""
        self.mock_sync_service.push_library.return_value = {
            "success": True,
            "message": "Successfully pushed library",
            "uploaded_bytes": 1024000,
            "snapshot_time": 3.2,
            "chunks_pushed": 1000,
            "library_version": 100,
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/push")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["operation"], "push")
        self.assertEqual(data["library_id"], "123")
        self.assertEqual(data["chunks_pushed"], 1000)
        self.assertEqual(data["uploaded_bytes"], 1024000)

    @patch('backend.api.sync.get_sync_service')
    def test_sync_library_auto_pull(self, mock_get_service):
        """Test auto sync that results in pull."""
        self.mock_sync_service.sync_library.return_value = {
            "success": True,
            "message": "Successfully pulled library",
            "downloaded_bytes": 1024000,
            "restore_time": 5.5,
            "chunks_restored": 1000,
            "library_version": 100,
            "operation": "pull",
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/sync?direction=auto")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["operation"], "pull")
        self.mock_sync_service.sync_library.assert_called_once_with("123", direction="auto")

    @patch('backend.api.sync.get_sync_service')
    def test_sync_library_auto_push(self, mock_get_service):
        """Test auto sync that results in push."""
        self.mock_sync_service.sync_library.return_value = {
            "success": True,
            "message": "Successfully pushed library",
            "uploaded_bytes": 1024000,
            "snapshot_time": 3.2,
            "chunks_pushed": 1000,
            "library_version": 100,
            "operation": "push",
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/sync?direction=auto")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["operation"], "push")

    @patch('backend.api.sync.get_sync_service')
    def test_sync_library_explicit_pull(self, mock_get_service):
        """Test sync with explicit pull direction."""
        self.mock_sync_service.sync_library.return_value = {
            "success": True,
            "message": "Successfully pulled library",
            "operation": "pull",
        }
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/sync?direction=pull")

        self.assertEqual(response.status_code, 200)
        self.mock_sync_service.sync_library.assert_called_once_with("123", direction="pull")

    @patch('backend.api.sync.get_sync_service')
    def test_sync_library_conflict(self, mock_get_service):
        """Test sync with conflict error."""
        self.mock_sync_service.sync_library.side_effect = ValueError(
            "Libraries have diverged. Manual conflict resolution required."
        )
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/123/sync?direction=auto")

        self.assertEqual(response.status_code, 409)  # Conflict
        self.assertIn("diverged", response.json()["detail"])

    @patch('backend.api.sync.get_settings')
    @patch('backend.api.sync.VectorStore')
    @patch('backend.api.sync.get_sync_service')
    def test_sync_all_libraries_success(self, mock_get_service, mock_vector_store_class, mock_get_settings):
        """Test syncing all libraries successfully."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.vector_db_path = "/tmp/test"
        mock_settings.embedding_dimension = 768
        mock_get_settings.return_value = mock_settings

        # Mock vector store to return library metadata
        mock_vector_store = Mock()
        mock_lib1 = Mock()
        mock_lib1.library_id = "123"
        mock_lib2 = Mock()
        mock_lib2.library_id = "456"
        mock_vector_store.get_all_library_metadata.return_value = [mock_lib1, mock_lib2]
        mock_vector_store_class.return_value = mock_vector_store

        # Mock sync service
        async def mock_sync(library_id, direction):
            if library_id == "123":
                return {"success": True, "message": "Synced", "operation": "pull"}
            else:
                return {"success": True, "message": "Synced", "operation": "push"}

        self.mock_sync_service.sync_library = mock_sync
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/sync-all?direction=auto")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_libraries"], 2)
        self.assertEqual(data["successful"], 2)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(len(data["results"]), 2)

    @patch('backend.api.sync.get_settings')
    @patch('backend.api.sync.VectorStore')
    @patch('backend.api.sync.get_sync_service')
    def test_sync_all_libraries_partial_failure(self, mock_get_service, mock_vector_store_class, mock_get_settings):
        """Test syncing all libraries with partial failure."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.vector_db_path = "/tmp/test"
        mock_settings.embedding_dimension = 768
        mock_get_settings.return_value = mock_settings

        # Mock vector store
        mock_vector_store = Mock()
        mock_lib1 = Mock()
        mock_lib1.library_id = "123"
        mock_lib2 = Mock()
        mock_lib2.library_id = "456"
        mock_vector_store.get_all_library_metadata.return_value = [mock_lib1, mock_lib2]
        mock_vector_store_class.return_value = mock_vector_store

        # Mock sync service with one failure
        async def mock_sync(library_id, direction):
            if library_id == "123":
                return {"success": True, "message": "Synced", "operation": "pull"}
            else:
                raise Exception("Network error")

        self.mock_sync_service.sync_library = mock_sync
        mock_get_service.return_value = self.mock_sync_service

        response = self.client.post("/api/vectors/sync-all?direction=auto")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_libraries"], 2)
        self.assertEqual(data["successful"], 1)
        self.assertEqual(data["failed"], 1)

        # Check results
        results = {r["library_id"]: r for r in data["results"]}
        self.assertTrue(results["123"]["success"])
        self.assertFalse(results["456"]["success"])
        self.assertIn("Network error", results["456"]["message"])

    @patch('backend.api.sync.get_sync_service')
    def test_endpoint_sync_disabled(self, mock_get_service):
        """Test that endpoints return 400 when sync is disabled."""
        mock_get_service.return_value = None

        endpoints = [
            ("/api/vectors/remote", "get"),
            ("/api/vectors/123/sync-status", "get"),
            ("/api/vectors/123/pull", "post"),
            ("/api/vectors/123/push", "post"),
            ("/api/vectors/123/sync", "post"),
            ("/api/vectors/sync-all", "post"),
        ]

        for url, method in endpoints:
            if method == "get":
                response = self.client.get(url)
            else:
                response = self.client.post(url)

            self.assertEqual(
                response.status_code,
                400,
                f"Expected 400 for {method.upper()} {url}, got {response.status_code}"
            )
            self.assertIn("not enabled", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
