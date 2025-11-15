"""
Tests for incremental indexing API endpoints.
"""

import unittest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime

from backend.models.library import LibraryIndexMetadata
from backend.db.vector_store import VectorStore


class TestLibraryAPIEndpoints(unittest.TestCase):
    """Test library API endpoints."""

    def setUp(self):
        """Set up test fixtures."""
        # Import here to avoid circular imports
        from backend.main import app
        self.client = TestClient(app)

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_get_index_status_success(
        self,
        mock_get_settings,
        mock_create_embedding,
        mock_vector_store_class
    ):
        """Test getting index status for an indexed library."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        # Mock embedding service
        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        # Mock vector store with metadata
        mock_vector_store = MagicMock()
        mock_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=12345,
            last_indexed_at="2025-01-12T10:30:00Z",
            total_items_indexed=250,
            total_chunks=12500,
            indexing_mode="incremental",
            force_reindex=False
        )
        mock_vector_store.__enter__.return_value.get_library_metadata.return_value = mock_metadata
        mock_vector_store_class.return_value = mock_vector_store

        # Make request
        response = self.client.get("/api/libraries/1/index-status")

        # Assert response
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["library_id"], "1")
        self.assertEqual(data["last_indexed_version"], 12345)
        self.assertEqual(data["total_items_indexed"], 250)
        self.assertEqual(data["indexing_mode"], "incremental")
        self.assertFalse(data["force_reindex"])

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_get_index_status_not_found(
        self,
        mock_get_settings,
        mock_create_embedding,
        mock_vector_store_class
    ):
        """Test getting index status for a library that hasn't been indexed."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        # Mock embedding service
        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        # Mock vector store returning None (not indexed)
        mock_vector_store = MagicMock()
        mock_vector_store.__enter__.return_value.get_library_metadata.return_value = None
        mock_vector_store_class.return_value = mock_vector_store

        # Make request
        response = self.client.get("/api/libraries/999/index-status")

        # Assert 404
        self.assertEqual(response.status_code, 404)
        self.assertIn("not been indexed", response.json()["detail"])

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_reset_library_index(
        self,
        mock_get_settings,
        mock_create_embedding,
        mock_vector_store_class
    ):
        """Test marking a library for hard reset."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        # Mock embedding service
        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        # Mock vector store
        mock_vector_store = MagicMock()
        mock_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=12345,
            force_reindex=True  # Now marked for reset
        )
        mock_vector_store.__enter__.return_value.get_library_metadata.return_value = mock_metadata
        mock_vector_store_class.return_value = mock_vector_store

        # Make request
        response = self.client.post("/api/libraries/1/reset-index")

        # Assert response
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("hard reset", data["message"])
        self.assertTrue(data["force_reindex"])
        self.assertEqual(data["next_index_mode"], "full")

        # Verify mark_library_for_reset was called
        mock_vector_store.__enter__.return_value.mark_library_for_reset.assert_called_once_with("1")

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_list_indexed_libraries(
        self,
        mock_get_settings,
        mock_create_embedding,
        mock_vector_store_class
    ):
        """Test listing all indexed libraries."""
        # Mock settings
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        # Mock embedding service
        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        # Mock vector store with multiple libraries
        mock_vector_store = MagicMock()
        mock_libraries = [
            LibraryIndexMetadata(
                library_id="1",
                library_type="user",
                library_name="User Library",
                last_indexed_version=100
            ),
            LibraryIndexMetadata(
                library_id="2",
                library_type="group",
                library_name="Group Library",
                last_indexed_version=200
            )
        ]
        mock_vector_store.__enter__.return_value.get_all_library_metadata.return_value = mock_libraries
        mock_vector_store_class.return_value = mock_vector_store

        # Make request
        response = self.client.get("/api/libraries/indexed")

        # Assert response
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["library_id"], "1")
        self.assertEqual(data[1]["library_id"], "2")


class TestIndexingAPIEndpoints(unittest.TestCase):
    """Test indexing API endpoints with mode parameter."""

    def setUp(self):
        """Set up test fixtures."""
        from backend.main import app
        self.client = TestClient(app)

    @patch('backend.api.indexing.ZoteroLocalAPI')
    @patch('asyncio.create_task')
    def test_start_indexing_with_auto_mode(
        self,
        mock_create_task,
        mock_zotero_api
    ):
        """Test starting indexing with auto mode."""
        # Mock Zotero API
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = True
        mock_zotero_api.return_value.__aenter__.return_value = mock_client

        # Make request with auto mode
        response = self.client.post(
            "/api/index/library/1",
            params={
                "library_name": "Test Library",
                "mode": "auto"
            }
        )

        # Assert response
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["library_id"], "1")
        self.assertEqual(data["status"], "started")
        self.assertIn("mode: auto", data["message"])

        # Verify task was created
        mock_create_task.assert_called_once()

    @patch('backend.api.indexing.ZoteroLocalAPI')
    @patch('asyncio.create_task')
    def test_start_indexing_with_incremental_mode(
        self,
        mock_create_task,
        mock_zotero_api
    ):
        """Test starting indexing with incremental mode."""
        # Mock Zotero API
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = True
        mock_zotero_api.return_value.__aenter__.return_value = mock_client

        # Make request with incremental mode
        response = self.client.post(
            "/api/index/library/1",
            params={
                "library_name": "Test Library",
                "mode": "incremental"
            }
        )

        # Assert response
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("mode: incremental", data["message"])

    @patch('backend.api.indexing.ZoteroLocalAPI')
    @patch('asyncio.create_task')
    def test_start_indexing_with_full_mode(
        self,
        mock_create_task,
        mock_zotero_api
    ):
        """Test starting indexing with full mode."""
        # Mock Zotero API
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = True
        mock_zotero_api.return_value.__aenter__.return_value = mock_client

        # Make request with full mode
        response = self.client.post(
            "/api/index/library/1",
            params={
                "library_name": "Test Library",
                "mode": "full"
            }
        )

        # Assert response
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("mode: full", data["message"])

    @patch('backend.api.indexing.ZoteroLocalAPI')
    def test_start_indexing_already_running(self, mock_zotero_api):
        """Test that starting indexing twice returns 409 error."""
        from backend.api.indexing import active_jobs

        # Mock Zotero API
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = True
        mock_zotero_api.return_value.__aenter__.return_value = mock_client

        # Set job as already running
        active_jobs["index_1"] = {"status": "running"}

        try:
            # Make request
            response = self.client.post("/api/index/library/1")

            # Assert 409 Conflict
            self.assertEqual(response.status_code, 409)
            self.assertIn("already being indexed", response.json()["detail"])
        finally:
            # Clean up
            active_jobs.pop("index_1", None)

    @patch('backend.api.indexing.ZoteroLocalAPI')
    def test_start_indexing_zotero_unavailable(self, mock_zotero_api):
        """Test error when Zotero is not accessible."""
        # Mock Zotero API - make check_connection return False
        mock_client = AsyncMock()
        mock_client.check_connection.return_value = False
        mock_zotero_api.return_value.__aenter__.return_value = mock_client

        # Make request
        response = self.client.post("/api/index/library/1")

        # Assert 503 Service Unavailable
        self.assertEqual(response.status_code, 503)
        self.assertIn("not accessible", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
