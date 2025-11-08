"""
Unit tests for Zotero integration module.

Note: These tests use mocking since they don't require a live Zotero instance.
Integration tests with actual Zotero should be in a separate file.
"""

import unittest
from unittest.mock import Mock, patch, AsyncMock
import aiohttp

from backend.zotero.local_api import ZoteroLocalAPI


class TestZoteroLocalAPI(unittest.IsolatedAsyncioTestCase):
    """Test ZoteroLocalAPI class."""

    async def asyncSetUp(self):
        """Set up test fixtures."""
        self.api = ZoteroLocalAPI("http://localhost:23119")

    async def asyncTearDown(self):
        """Clean up after tests."""
        await self.api.close()

    async def test_init(self):
        """Test API initialization."""
        self.assertEqual(self.api.base_url, "http://localhost:23119")
        self.assertIsNone(self.api.session)

    async def test_ensure_session(self):
        """Test session creation."""
        await self.api._ensure_session()
        self.assertIsNotNone(self.api.session)
        self.assertIsInstance(self.api.session, aiohttp.ClientSession)

    async def test_context_manager(self):
        """Test async context manager."""
        async with ZoteroLocalAPI() as api:
            self.assertIsNotNone(api.session)

    @patch("aiohttp.ClientSession.get")
    async def test_check_connection_success(self, mock_get):
        """Test successful connection check."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_get.return_value.__aenter__.return_value = mock_response

        result = await self.api.check_connection()
        self.assertTrue(result)

    @patch("aiohttp.ClientSession.get")
    async def test_check_connection_failure(self, mock_get):
        """Test failed connection check."""
        mock_get.side_effect = Exception("Connection refused")

        result = await self.api.check_connection()
        self.assertFalse(result)

    @patch("aiohttp.ClientSession.get")
    async def test_list_libraries(self, mock_get):
        """Test listing libraries."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[])
        mock_get.return_value.__aenter__.return_value = mock_response

        libraries = await self.api.list_libraries()
        self.assertIsInstance(libraries, list)

    @patch("aiohttp.ClientSession.get")
    async def test_get_library_items(self, mock_get):
        """Test getting library items."""
        mock_items = [
            {"key": "ABC123", "data": {"title": "Test Paper"}},
            {"key": "DEF456", "data": {"title": "Another Paper"}},
        ]

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_items)
        mock_get.return_value.__aenter__.return_value = mock_response

        items = await self.api.get_library_items("1", limit=10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["key"], "ABC123")

    @patch("aiohttp.ClientSession.get")
    async def test_get_library_items_with_pagination(self, mock_get):
        """Test getting library items with pagination."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[])
        mock_get.return_value.__aenter__.return_value = mock_response

        await self.api.get_library_items("1", limit=50, start=100)

        # Verify pagination parameters were passed
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertEqual(call_args[1]["params"]["start"], 100)
        self.assertEqual(call_args[1]["params"]["limit"], 50)

    @patch("aiohttp.ClientSession.get")
    async def test_get_item(self, mock_get):
        """Test getting a specific item."""
        mock_item = {"key": "ABC123", "data": {"title": "Test Paper"}}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_item)
        mock_get.return_value.__aenter__.return_value = mock_response

        item = await self.api.get_item("1", "ABC123")
        self.assertIsNotNone(item)
        self.assertEqual(item["key"], "ABC123")

    @patch("aiohttp.ClientSession.get")
    async def test_get_item_not_found(self, mock_get):
        """Test getting non-existent item."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_get.return_value.__aenter__.return_value = mock_response

        item = await self.api.get_item("1", "NONEXISTENT")
        self.assertIsNone(item)

    @patch("aiohttp.ClientSession.get")
    async def test_get_item_children(self, mock_get):
        """Test getting item children (attachments)."""
        mock_children = [
            {"key": "ATT1", "data": {"itemType": "attachment"}},
            {"key": "NOTE1", "data": {"itemType": "note"}},
        ]

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_children)
        mock_get.return_value.__aenter__.return_value = mock_response

        children = await self.api.get_item_children("1", "ABC123")
        self.assertEqual(len(children), 2)

    @patch("aiohttp.ClientSession.get")
    async def test_get_attachment_file(self, mock_get):
        """Test getting attachment file content."""
        mock_content = b"PDF file content here"

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read = AsyncMock(return_value=mock_content)
        mock_get.return_value.__aenter__.return_value = mock_response

        content = await self.api.get_attachment_file("1", "ATT1")
        self.assertEqual(content, mock_content)

    @patch("aiohttp.ClientSession.get")
    async def test_get_attachment_file_not_found(self, mock_get):
        """Test getting non-existent attachment file."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_get.return_value.__aenter__.return_value = mock_response

        content = await self.api.get_attachment_file("1", "NONEXISTENT")
        self.assertIsNone(content)

    @patch("aiohttp.ClientSession.get")
    async def test_get_fulltext(self, mock_get):
        """Test getting full-text content."""
        mock_fulltext = {
            "content": "This is the full text of the document.",
            "indexedPages": 10,
        }

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_fulltext)
        mock_get.return_value.__aenter__.return_value = mock_response

        fulltext = await self.api.get_fulltext("1", "ABC123")
        self.assertIsNotNone(fulltext)
        self.assertIn("content", fulltext)
        self.assertEqual(fulltext["indexedPages"], 10)

    @patch("aiohttp.ClientSession.get")
    async def test_get_fulltext_not_indexed(self, mock_get):
        """Test getting fulltext for non-indexed item."""
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_get.return_value.__aenter__.return_value = mock_response

        fulltext = await self.api.get_fulltext("1", "ABC123")
        self.assertIsNone(fulltext)


if __name__ == "__main__":
    unittest.main()
