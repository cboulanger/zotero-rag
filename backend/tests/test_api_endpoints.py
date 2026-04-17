"""
Tests for library and indexing API endpoints.
"""

import unittest
from unittest.mock import Mock, MagicMock, patch
from fastapi.testclient import TestClient
from datetime import datetime

from backend.models.library import LibraryIndexMetadata
from backend.db.vector_store import VectorStore


class TestLibraryAPIEndpoints(unittest.TestCase):
    """Test library management API endpoints."""

    def setUp(self):
        from backend.main import app
        self.client = TestClient(app)

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_get_index_status_success(self, mock_get_settings, mock_create_embedding, mock_vector_store_class):
        """Test getting index status for an indexed library."""
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

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

        response = self.client.get("/api/libraries/1/index-status")

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
    def test_get_index_status_not_found(self, mock_get_settings, mock_create_embedding, mock_vector_store_class):
        """Test getting index status for a library that hasn't been indexed."""
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        mock_vector_store = MagicMock()
        mock_vector_store.__enter__.return_value.get_library_metadata.return_value = None
        mock_vector_store_class.return_value = mock_vector_store

        response = self.client.get("/api/libraries/999/index-status")

        self.assertEqual(response.status_code, 404)
        self.assertIn("not been indexed", response.json()["detail"])

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_reset_library_index(self, mock_get_settings, mock_create_embedding, mock_vector_store_class):
        """Test marking a library for hard reset."""
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        mock_vector_store = MagicMock()
        mock_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=12345,
            force_reindex=True
        )
        mock_vector_store.__enter__.return_value.get_library_metadata.return_value = mock_metadata
        mock_vector_store_class.return_value = mock_vector_store

        response = self.client.post("/api/libraries/1/reset-index")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("hard reset", data["message"])
        self.assertTrue(data["force_reindex"])
        self.assertEqual(data["next_index_mode"], "full")
        mock_vector_store.__enter__.return_value.mark_library_for_reset.assert_called_once_with("1")

    @patch('backend.db.vector_store.VectorStore')
    @patch('backend.services.embeddings.create_embedding_service')
    @patch('backend.config.settings.get_settings')
    def test_list_indexed_libraries(self, mock_get_settings, mock_create_embedding, mock_vector_store_class):
        """Test listing all indexed libraries."""
        mock_settings = Mock()
        mock_settings.get_hardware_preset.return_value = Mock(embedding=Mock())
        mock_settings.model_weights_path = "/tmp/models"
        mock_settings.get_api_key.return_value = None
        mock_settings.vector_db_path = "/tmp/vectordb"
        mock_get_settings.return_value = mock_settings

        mock_embedding = Mock()
        mock_embedding.get_embedding_dim.return_value = 384
        mock_create_embedding.return_value = mock_embedding

        mock_vector_store = MagicMock()
        mock_libraries = [
            LibraryIndexMetadata(library_id="1", library_type="user",
                                 library_name="User Library", last_indexed_version=100),
            LibraryIndexMetadata(library_id="2", library_type="group",
                                 library_name="Group Library", last_indexed_version=200)
        ]
        mock_vector_store.__enter__.return_value.get_all_library_metadata.return_value = mock_libraries
        mock_vector_store_class.return_value = mock_vector_store

        response = self.client.get("/api/libraries/indexed")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["library_id"], "1")
        self.assertEqual(data[1]["library_id"], "2")


if __name__ == "__main__":
    unittest.main()
