"""Unit tests for backend.zotero.web_api.ZoteroWebAPI."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.zotero.web_api import ZoteroWebAPI


def _make_response(status: int, body=None, headers: dict | None = None) -> MagicMock:
    """Build a mock aiohttp response suitable for use as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}

    if isinstance(body, (dict, list)):
        resp.json = AsyncMock(return_value=body)
        resp.read = AsyncMock(return_value=json.dumps(body).encode())
    elif isinstance(body, bytes):
        resp.read = AsyncMock(return_value=body)
        resp.json = AsyncMock(return_value={})
    else:
        resp.json = AsyncMock(return_value={})
        resp.read = AsyncMock(return_value=b"")

    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session(*responses) -> MagicMock:
    """Build a mock aiohttp.ClientSession that returns the given responses in order.

    Sets session.closed = False so ZoteroWebAPI._ensure_session() keeps the mock
    rather than creating a real aiohttp.ClientSession.
    """
    session = MagicMock()
    session.closed = False
    if len(responses) == 1:
        session.get = MagicMock(return_value=responses[0])
    else:
        session.get = MagicMock(side_effect=list(responses))
    return session


class TestZoteroWebAPIIdConversion(unittest.TestCase):
    """Test backend-ID → numeric-ID conversion and URL building."""

    def setUp(self):
        self.api = ZoteroWebAPI(api_key="testkey")

    def test_parse_backend_id_user(self):
        self.assertEqual(self.api._backend_id_to_numeric("u12345", "user"), "12345")

    def test_parse_backend_id_group(self):
        self.assertEqual(self.api._backend_id_to_numeric("678", "group"), "678")

    def test_base_url_user(self):
        url = self.api._base_url("u12345", "user")
        self.assertIn("/users/12345", url)

    def test_base_url_group(self):
        url = self.api._base_url("678", "group")
        self.assertIn("/groups/678", url)


class TestZoteroWebAPIGetLibraryVersion(unittest.IsolatedAsyncioTestCase):
    async def test_get_library_version_success(self):
        api = ZoteroWebAPI(api_key="testkey")
        resp = _make_response(200, {}, headers={"Last-Modified-Version": "42"})
        api.session = _make_session(resp)

        version = await api.get_library_version("u12345", "user")
        self.assertEqual(version, 42)

    async def test_get_library_version_error_returns_zero(self):
        api = ZoteroWebAPI(api_key="testkey")
        resp = _make_response(403, {})
        api.session = _make_session(resp)

        version = await api.get_library_version("u12345", "user")
        self.assertEqual(version, 0)


class TestZoteroWebAPIGetLibraryItemsSince(unittest.IsolatedAsyncioTestCase):
    async def test_get_library_items_since_no_version(self):
        """Without since_version, fetches all items (single page)."""
        items = [{"key": "AAAA", "version": 1, "data": {}}]
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, items))

        result = await api.get_library_items_since("u12345", "user")
        self.assertEqual(result, items)

    async def test_get_library_items_since_with_version(self):
        """since_version is passed as query parameter."""
        api = ZoteroWebAPI(api_key="testkey")
        session = _make_session(_make_response(200, []))
        api.session = session

        await api.get_library_items_since("u12345", "user", since_version=10)
        params = session.get.call_args.kwargs.get("params", {})
        self.assertIn("since", params)
        self.assertEqual(params["since"], 10)

    async def test_get_library_items_pagination(self):
        """Two pages of 100 items each are combined into one list."""
        page1 = [{"key": f"ITEM{i}", "version": i, "data": {}} for i in range(100)]
        page2 = [{"key": f"ITEM{i}", "version": i, "data": {}} for i in range(100, 150)]
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, page1), _make_response(200, page2))

        result = await api.get_library_items_since("u12345", "user")
        self.assertEqual(len(result), 150)

    async def test_get_library_items_limit_respected(self):
        """When limit=5, only 5 items are returned."""
        items = [{"key": f"ITEM{i}", "version": i, "data": {}} for i in range(5)]
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, items))

        result = await api.get_library_items_since("u12345", "user", limit=5)
        self.assertEqual(len(result), 5)


class TestZoteroWebAPIGetItemChildren(unittest.IsolatedAsyncioTestCase):
    async def test_get_item_children_success(self):
        children = [{"key": "CHILD1", "data": {"itemType": "attachment"}}]
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, children))

        result = await api.get_item_children("u12345", "PARENT", "user")
        self.assertEqual(result, children)

    async def test_get_item_children_error_returns_empty(self):
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(404, {}))

        result = await api.get_item_children("u12345", "MISSING", "user")
        self.assertEqual(result, [])


class TestZoteroWebAPIGetAttachmentFile(unittest.IsolatedAsyncioTestCase):
    async def test_get_attachment_file_success(self):
        content = b"%PDF-1.4 fake pdf content"
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, content))

        result = await api.get_attachment_file("u12345", "ATTACH1", "user")
        self.assertEqual(result, content)

    async def test_get_attachment_file_not_found_returns_none(self):
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(404, b""))

        result = await api.get_attachment_file("u12345", "MISSING", "user")
        self.assertIsNone(result)

    async def test_get_attachment_file_server_error_returns_none(self):
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(500, b""))

        result = await api.get_attachment_file("u12345", "ERR", "user")
        self.assertIsNone(result)


class TestZoteroWebAPIBackoffHeader(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_header_causes_sleep(self):
        """When a Backoff header is present, asyncio.sleep is called."""
        resp = _make_response(200, [], headers={"Backoff": "2", "Last-Modified-Version": "1"})
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(resp)

        with patch("backend.zotero.web_api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await api.get_library_version("u12345", "user")
            mock_sleep.assert_called_once_with(2.0)


class TestZoteroWebAPIContextManager(unittest.IsolatedAsyncioTestCase):
    async def test_context_manager_opens_and_closes_session(self):
        api = ZoteroWebAPI(api_key="testkey")
        async with api:
            self.assertIsNotNone(api.session)
        # After exit, session is closed
        self.assertTrue(api.session is None or api.session.closed)


if __name__ == "__main__":
    unittest.main()
