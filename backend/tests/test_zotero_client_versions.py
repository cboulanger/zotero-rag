"""
Unit tests for version-aware Zotero API client methods.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock
from backend.zotero.local_api import ZoteroLocalAPI


class TestVersionAwareFetching(unittest.IsolatedAsyncioTestCase):
    """Test version-aware fetching functionality in Zotero API client."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = ZoteroLocalAPI()

    async def asyncTearDown(self):
        """Clean up after tests."""
        # Only close if session is a real aiohttp session, not a mock
        if self.client.session and hasattr(self.client.session, '_connector'):
            await self.client.close()

    def _mock_session_get(self, mock_response):
        """Helper to create a properly mocked session.get() call."""
        # Create async context manager mock
        async_cm = AsyncMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_response)
        async_cm.__aexit__ = AsyncMock(return_value=None)
        return MagicMock(return_value=async_cm)

    def _create_mock_item(self, key: str, version: int, item_type: str = "journalArticle") -> dict:
        """Create a mock Zotero item for testing."""
        return {
            "key": key,
            "version": version,
            "library": {
                "type": "user",
                "id": 1,
                "name": "My Library"
            },
            "data": {
                "key": key,
                "version": version,
                "itemType": item_type,
                "title": f"Test Item {key}",
                "dateModified": "2025-01-12T10:00:00Z",
                "creators": [
                    {"creatorType": "author", "firstName": "John", "lastName": "Doe"}
                ]
            }
        }

    async def test_get_library_items_since_without_version(self):
        """Test fetching all items without version filter."""
        # Mock the response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[
            self._create_mock_item("ITEM001", 100),
            self._create_mock_item("ITEM002", 101),
            self._create_mock_item("ITEM003", 102),
        ])

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Fetch items
        items = await self.client.get_library_items_since(
            library_id="1",
            library_type="user",
            since_version=None,
            limit=100
        )

        # Assert
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["version"], 100)
        self.assertEqual(items[2]["version"], 102)

    async def test_get_library_items_since_with_version(self):
        """Test fetching items since a specific version."""
        # Mock the response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[
            self._create_mock_item("ITEM003", 102),
            self._create_mock_item("ITEM004", 103),
        ])

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Fetch items since version 101
        items = await self.client.get_library_items_since(
            library_id="1",
            library_type="user",
            since_version=101,
            limit=100
        )

        # Assert
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["version"] > 101 for item in items))

    async def test_get_library_items_since_pagination(self):
        """Test automatic pagination when no limit specified."""
        # Mock first response (100 items)
        mock_response_1 = AsyncMock()
        mock_response_1.status = 200
        mock_response_1.json = AsyncMock(return_value=[
            self._create_mock_item(f"ITEM{i:03d}", 100 + i) for i in range(100)
        ])

        # Mock second response (50 items)
        mock_response_2 = AsyncMock()
        mock_response_2.status = 200
        mock_response_2.json = AsyncMock(return_value=[
            self._create_mock_item(f"ITEM{i:03d}", 200 + i) for i in range(50)
        ])

        # Mock the session with multiple responses
        mock_session = AsyncMock()
        # Create separate async context managers for each response
        cm1 = AsyncMock()
        cm1.__aenter__ = AsyncMock(return_value=mock_response_1)
        cm1.__aexit__ = AsyncMock(return_value=None)

        cm2 = AsyncMock()
        cm2.__aenter__ = AsyncMock(return_value=mock_response_2)
        cm2.__aexit__ = AsyncMock(return_value=None)

        mock_session.get = MagicMock(side_effect=[cm1, cm2])
        mock_session.closed = False

        self.client.session = mock_session

        # Fetch all items (should paginate)
        items = await self.client.get_library_items_since(
            library_id="1",
            library_type="user",
            since_version=None,
            limit=None  # No limit, should paginate
        )

        # Assert
        self.assertEqual(len(items), 150)

    async def test_get_library_version_range(self):
        """Test getting min and max version numbers."""
        # Mock the response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[
            self._create_mock_item("ITEM001", 50),
            self._create_mock_item("ITEM002", 100),
            self._create_mock_item("ITEM003", 75),
            self._create_mock_item("ITEM004", 200),
        ])

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Get version range
        min_version, max_version = await self.client.get_library_version_range(
            library_id="1",
            library_type="user"
        )

        # Assert
        self.assertEqual(min_version, 50)
        self.assertEqual(max_version, 200)

    async def test_get_library_version_range_empty(self):
        """Test version range with empty library."""
        # Mock empty response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[])

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Get version range
        min_version, max_version = await self.client.get_library_version_range(
            library_id="1",
            library_type="user"
        )

        # Assert
        self.assertEqual(min_version, 0)
        self.assertEqual(max_version, 0)

    async def test_get_item_with_version(self):
        """Test fetching a single item with version info."""
        # Mock the response
        mock_item = self._create_mock_item("ABCD1234", 150)
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_item)

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Get item
        item = await self.client.get_item_with_version(
            library_id="1",
            item_key="ABCD1234",
            library_type="user"
        )

        # Assert
        self.assertIsNotNone(item)
        self.assertEqual(item["key"], "ABCD1234")
        self.assertEqual(item["version"], 150)

    async def test_get_item_with_version_not_found(self):
        """Test fetching a non-existent item."""
        # Mock 404 response
        mock_response = AsyncMock()
        mock_response.status = 404

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Get item
        item = await self.client.get_item_with_version(
            library_id="1",
            item_key="NOTFOUND",
            library_type="user"
        )

        # Assert
        self.assertIsNone(item)

    async def test_get_attachment_with_version(self):
        """Test fetching attachment with version info."""
        # Mock the response
        mock_attachment = self._create_mock_item("ATTACH01", 120, "attachment")
        mock_attachment["data"]["contentType"] = "application/pdf"
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_attachment)

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Get attachment
        attachment = await self.client.get_attachment_with_version(
            library_id="1",
            attachment_key="ATTACH01",
            library_type="user"
        )

        # Assert
        self.assertIsNotNone(attachment)
        self.assertEqual(attachment["key"], "ATTACH01")
        self.assertEqual(attachment["version"], 120)

    async def test_backward_compatibility_get_library_items(self):
        """Test that old get_library_items method still works."""
        # Mock the response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[
            self._create_mock_item("ITEM001", 100),
            self._create_mock_item("ITEM002", 101),
        ])

        # Mock the session
        mock_session = AsyncMock()
        mock_session.get = self._mock_session_get(mock_response)
        mock_session.closed = False

        self.client.session = mock_session

        # Use old method
        items = await self.client.get_library_items(
            library_id="1",
            library_type="user",
            limit=100
        )

        # Assert - should work exactly as before
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["version"], 100)


if __name__ == "__main__":
    unittest.main()
