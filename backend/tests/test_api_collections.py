"""
Tests for collections API endpoints.

Covers:
- GET  /api/collections/vectors/status
- POST /api/collections/vectors/sync
- GET  /api/collections/suggest
"""

import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.dependencies import get_vector_store


class TestCollectionsAPI(unittest.TestCase):
    """Test suite for the /api/collections/* endpoints."""

    def setUp(self):
        from backend.main import app
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _override_vs(self, mock_vs):
        """Register mock VectorStore as a FastAPI dependency override."""
        self.app.dependency_overrides[get_vector_store] = lambda: mock_vs

    # ------------------------------------------------------------------
    # GET /api/collections/vectors/status
    # ------------------------------------------------------------------

    def test_get_collection_vectors_status_returns_counts(self):
        """Status endpoint returns correct item and collection vector counts."""
        mock_vs = MagicMock()
        mock_vs.count_item_vectors.return_value = 42
        mock_vs.count_collection_vectors.return_value = 7
        self._override_vs(mock_vs)

        response = self.client.get(
            "/api/collections/vectors/status",
            params={"library_id": "lib1"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["library_id"], "lib1")
        self.assertEqual(data["item_vectors_count"], 42)
        self.assertEqual(data["collection_vectors_count"], 7)
        self.assertTrue(data["computed"])
        mock_vs.count_item_vectors.assert_called_once_with("lib1")
        mock_vs.count_collection_vectors.assert_called_once_with("lib1")

    def test_get_collection_vectors_status_computed_false_when_zero(self):
        """computed flag is False when collection_vectors_count is 0."""
        mock_vs = MagicMock()
        mock_vs.count_item_vectors.return_value = 10
        mock_vs.count_collection_vectors.return_value = 0
        self._override_vs(mock_vs)

        response = self.client.get(
            "/api/collections/vectors/status",
            params={"library_id": "lib2"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["collection_vectors_count"], 0)
        self.assertFalse(data["computed"])

    def test_get_collection_vectors_status_503_when_no_store(self):
        """Returns 503 when vector store is unavailable."""
        self._override_vs(None)

        response = self.client.get(
            "/api/collections/vectors/status",
            params={"library_id": "lib1"},
        )

        self.assertEqual(response.status_code, 503)

    # ------------------------------------------------------------------
    # GET /api/collections/suggest
    # ------------------------------------------------------------------

    def test_suggest_returns_empty_list_when_no_item_vector(self):
        """Returns [] (not 404) when no pre-computed item vector exists."""
        mock_vs = MagicMock()
        mock_vs.get_item_vector.return_value = None
        self._override_vs(mock_vs)

        response = self.client.get(
            "/api/collections/suggest",
            params={"library_id": "lib1", "item_key": "MISSING"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        mock_vs.get_item_vector.assert_called_once_with("lib1", "MISSING")

    def test_suggest_returns_suggestions_when_vectors_exist(self):
        """Returns suggestions sorted by score when both item and collection vectors exist."""
        mock_vs = MagicMock()
        item_vec = [0.1] * 8
        mock_vs.get_item_vector.return_value = (item_vec, {"item_key": "ITEMKEY"})
        mock_vs.search_collection_vectors.return_value = [
            ("col1", 0.95, {"collection_name": "Physics", "collection_id": "col1"}),
            ("col2", 0.80, {"collection_name": "Chemistry", "collection_id": "col2"}),
        ]
        self._override_vs(mock_vs)

        response = self.client.get(
            "/api/collections/suggest",
            params={"library_id": "lib1", "item_key": "ITEMKEY", "limit": "5"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["collection_id"], "col1")
        self.assertEqual(data[0]["collection_name"], "Physics")
        self.assertAlmostEqual(data[0]["score"], 0.95)
        self.assertEqual(data[0]["library_id"], "lib1")
        self.assertEqual(data[1]["collection_id"], "col2")

        mock_vs.search_collection_vectors.assert_called_once_with(item_vec, limit=5)

    def test_suggest_clamps_limit_to_20(self):
        """limit parameter above 20 is rejected with 422 (FastAPI Query validation)."""
        mock_vs = MagicMock()
        mock_vs.get_item_vector.return_value = ([0.0] * 8, {})
        mock_vs.search_collection_vectors.return_value = []
        self._override_vs(mock_vs)

        response = self.client.get(
            "/api/collections/suggest",
            params={"library_id": "lib1", "item_key": "KEY", "limit": "100"},
        )

        self.assertEqual(response.status_code, 422)

    def test_suggest_503_when_no_store(self):
        """Returns 503 when vector store is unavailable."""
        self._override_vs(None)

        response = self.client.get(
            "/api/collections/suggest",
            params={"library_id": "lib1", "item_key": "KEY"},
        )

        self.assertEqual(response.status_code, 503)

    def test_suggest_missing_collection_name_defaults_to_empty_string(self):
        """Collection name defaults to '' when absent from payload."""
        mock_vs = MagicMock()
        mock_vs.get_item_vector.return_value = ([0.1] * 8, {})
        # payload without collection_name
        mock_vs.search_collection_vectors.return_value = [
            ("col9", 0.70, {"collection_id": "col9"}),
        ]
        self._override_vs(mock_vs)

        response = self.client.get(
            "/api/collections/suggest",
            params={"library_id": "lib1", "item_key": "KEY"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data[0]["collection_name"], "")

    # ------------------------------------------------------------------
    # POST /api/collections/vectors/sync
    # ------------------------------------------------------------------

    def test_sync_processes_collection_map_and_returns_stats(self):
        """Sync endpoint delegates to CollectionVectorService and returns stats dict."""
        mock_vs = MagicMock()
        self._override_vs(mock_vs)

        expected_stats = {
            "items_computed": 2,
            "items_skipped": 0,
            "collections_computed": 1,
            "collections_skipped": 0,
        }

        with patch(
            "backend.api.collections.CollectionVectorService"
        ) as MockSvc, patch(
            "backend.api.collections.make_embedding_service"
        ) as mock_make_emb:
            mock_svc_instance = MagicMock()
            mock_svc_instance.sync_library = AsyncMock(return_value=expected_stats)
            MockSvc.return_value = mock_svc_instance
            mock_make_emb.return_value = MagicMock()

            response = self.client.post(
                "/api/collections/vectors/sync",
                json={
                    "library_id": "lib1",
                    "collection_map": {
                        "ITEM1": ["col1"],
                        "ITEM2": ["col1"],
                    },
                    "collection_names": {"col1": "Physics"},
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["items_computed"], 2)
        self.assertEqual(data["collections_computed"], 1)

        # Verify service was constructed and called correctly
        MockSvc.assert_called_once_with(mock_vs, mock_make_emb.return_value)
        mock_svc_instance.sync_library.assert_awaited_once_with(
            library_id="lib1",
            collection_map={"ITEM1": ["col1"], "ITEM2": ["col1"]},
            collection_names={"col1": "Physics"},
        )

    def test_sync_503_when_no_store(self):
        """Sync returns 503 when vector store is unavailable."""
        self._override_vs(None)

        response = self.client.post(
            "/api/collections/vectors/sync",
            json={
                "library_id": "lib1",
                "collection_map": {},
                "collection_names": {},
            },
        )

        self.assertEqual(response.status_code, 503)

    def test_sync_uses_default_empty_collection_names(self):
        """Sync request works without collection_names field (defaults to {})."""
        mock_vs = MagicMock()
        self._override_vs(mock_vs)

        stats = {"items_computed": 0, "items_skipped": 0, "collections_computed": 0, "collections_skipped": 0}

        with patch("backend.api.collections.CollectionVectorService") as MockSvc, \
             patch("backend.api.collections.make_embedding_service"):
            mock_svc_instance = MagicMock()
            mock_svc_instance.sync_library = AsyncMock(return_value=stats)
            MockSvc.return_value = mock_svc_instance

            response = self.client.post(
                "/api/collections/vectors/sync",
                json={"library_id": "lib1", "collection_map": {}},
            )

        self.assertEqual(response.status_code, 200)
        mock_svc_instance.sync_library.assert_awaited_once_with(
            library_id="lib1",
            collection_map={},
            collection_names={},
        )


if __name__ == "__main__":
    unittest.main()
