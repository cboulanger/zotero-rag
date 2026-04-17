"""
Tests for library and indexing API endpoints.
"""

import unittest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from backend.models.library import LibraryIndexMetadata
from backend.dependencies import get_vector_store


class TestLibraryAPIEndpoints(unittest.TestCase):
    """Test library management API endpoints."""

    def setUp(self):
        from backend.main import app
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _override_vs(self, mock_vs):
        self.app.dependency_overrides[get_vector_store] = lambda: mock_vs

    def test_get_index_status_success(self):
        """Test getting index status for an indexed library."""
        mock_vs = MagicMock()
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
        mock_vs.get_library_metadata.return_value = mock_metadata
        self._override_vs(mock_vs)

        response = self.client.get("/api/libraries/1/index-status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["library_id"], "1")
        self.assertEqual(data["last_indexed_version"], 12345)
        self.assertEqual(data["total_items_indexed"], 250)
        self.assertEqual(data["indexing_mode"], "incremental")
        self.assertFalse(data["force_reindex"])

    def test_get_index_status_not_found(self):
        """Test getting index status for a library that hasn't been indexed."""
        mock_vs = MagicMock()
        mock_vs.get_library_metadata.return_value = None
        self._override_vs(mock_vs)

        response = self.client.get("/api/libraries/999/index-status")

        self.assertEqual(response.status_code, 404)
        self.assertIn("not been indexed", response.json()["detail"])

    def test_reset_library_index(self):
        """Test marking a library for hard reset."""
        mock_vs = MagicMock()
        mock_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=12345,
            force_reindex=True
        )
        mock_vs.get_library_metadata.return_value = mock_metadata
        self._override_vs(mock_vs)

        response = self.client.post("/api/libraries/1/reset-index")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("hard reset", data["message"])
        self.assertTrue(data["force_reindex"])
        self.assertEqual(data["next_index_mode"], "full")
        mock_vs.mark_library_for_reset.assert_called_once_with("1")

    def test_list_indexed_libraries(self):
        """Test listing all indexed libraries."""
        mock_vs = MagicMock()
        mock_libraries = [
            LibraryIndexMetadata(library_id="1", library_type="user",
                                 library_name="User Library", last_indexed_version=100),
            LibraryIndexMetadata(library_id="2", library_type="group",
                                 library_name="Group Library", last_indexed_version=200)
        ]
        mock_vs.get_all_library_metadata.return_value = mock_libraries
        self._override_vs(mock_vs)

        response = self.client.get("/api/libraries/indexed")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["library_id"], "1")
        self.assertEqual(data[1]["library_id"], "2")


if __name__ == "__main__":
    unittest.main()
