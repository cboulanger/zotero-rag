"""
Zotero Web API client for https://api.zotero.org.

Provides the same interface as ZoteroLocalAPI so DocumentProcessor can use
either backend transparently. Accepts backend-format library IDs (u12345 for
user libraries, 678 for group libraries) and converts them internally to the
numeric IDs required by the web API.
"""

import asyncio
import logging
from typing import Optional, Any

import aiohttp

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"
_PAGE_SIZE = 100


class ZoteroWebAPI:
    """
    Client for the Zotero web API (api.zotero.org).

    Accepts backend-format library IDs so DocumentProcessor can swap this in
    for ZoteroLocalAPI without changes: u12345 -> /users/12345, 678 -> /groups/678.
    """

    def __init__(self, api_key: str, base_url: str = ZOTERO_API_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None

    def _headers(self) -> dict[str, str]:
        return {
            "Zotero-API-Key": self.api_key,
            "Zotero-API-Version": "3",
        }

    def _backend_id_to_numeric(self, library_id: str, library_type: str) -> str:
        """Convert backend-format ID to numeric string for API URLs.

        u12345 -> "12345" (user), 678 -> "678" (group)
        """
        if library_type == "user":
            return library_id[1:] if library_id.startswith("u") else library_id
        return library_id

    def _base_url(self, library_id: str, library_type: str) -> str:
        numeric = self._backend_id_to_numeric(library_id, library_type)
        kind = "users" if library_type == "user" else "groups"
        return f"{self.base_url}/{kind}/{numeric}"

    async def _ensure_session(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self._headers())

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def __aenter__(self) -> "ZoteroWebAPI":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _handle_rate_limit(self, response: aiohttp.ClientResponse) -> None:
        """Sleep if the server requests backoff."""
        delay_str = response.headers.get("Backoff") or response.headers.get("Retry-After")
        if delay_str:
            try:
                delay = float(delay_str)
                logger.warning("Zotero rate limit: sleeping %.1fs", delay)
                await asyncio.sleep(delay)
            except ValueError:
                pass

    async def get_library_version(self, library_id: str, library_type: str = "user") -> int:
        """Return the current library version from Last-Modified-Version header."""
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/items"
        async with self.session.get(url, params={"limit": 0, "format": "json"}) as resp:
            await self._handle_rate_limit(resp)
            if resp.status != 200:
                logger.error("get_library_version failed: HTTP %s", resp.status)
                return 0
            return int(resp.headers.get("Last-Modified-Version", 0))

    async def get_library_item_count(self, library_id: str, library_type: str = "user") -> int:
        """Return the total item count for the library from Total-Results header (lightweight)."""
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/items"
        async with self.session.get(url, params={"limit": 0, "format": "json"}) as resp:
            await self._handle_rate_limit(resp)
            if resp.status != 200:
                logger.warning("get_library_item_count failed: HTTP %s", resp.status)
                return 0
            return int(resp.headers.get("Total-Results", 0))

    async def get_library_items_since(
        self,
        library_id: str,
        library_type: str = "user",
        since_version: Optional[int] = None,
        limit: Optional[int] = None,
        start: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch items with automatic pagination.

        If *limit* is given, at most that many items are returned (no pagination
        beyond the first page). If *limit* is None, all pages are fetched.
        """
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/items"
        page_size = min(limit, _PAGE_SIZE) if limit is not None else _PAGE_SIZE

        params: dict[str, Any] = {"format": "json", "limit": page_size, "start": start}
        if since_version is not None:
            params["since"] = since_version
            logger.info("Fetching items since version %s", since_version)

        all_items: list[dict] = []
        current_start = start

        while True:
            params["start"] = current_start
            async with self.session.get(url, params=params) as resp:
                await self._handle_rate_limit(resp)
                if resp.status != 200:
                    logger.error("get_library_items_since failed: HTTP %s", resp.status)
                    break
                items = await resp.json()
                if not isinstance(items, list):
                    break
                all_items.extend(items)

                if len(items) < page_size:
                    break  # last page
                if limit is not None and len(all_items) >= limit:
                    all_items = all_items[:limit]
                    break
                current_start += len(items)

        logger.info("Retrieved %d items from library %s", len(all_items), library_id)
        return all_items

    async def get_item_children(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> list[dict[str, Any]]:
        """Return child items (attachments, notes) for a parent item key."""
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/items/{item_key}/children"
        async with self.session.get(url, params={"format": "json"}) as resp:
            if resp.status != 200:
                logger.debug("get_item_children %s: HTTP %s", item_key, resp.status)
                return []
            children = await resp.json()
            return children if isinstance(children, list) else []

    async def get_deleted_item_keys(
        self,
        library_id: str,
        library_type: str = "user",
        since_version: int = 0,
    ) -> list[str]:
        """Return item keys deleted from Zotero since *since_version*.

        Calls GET /{kind}/{id}/deleted?since=version and returns the "items" list.
        Returns an empty list on error so callers can treat this as best-effort.
        """
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/deleted"
        async with self.session.get(url, params={"since": since_version}) as resp:
            await self._handle_rate_limit(resp)
            if resp.status == 200:
                data = await resp.json()
                return data.get("items", [])
            logger.warning("get_deleted_item_keys failed: HTTP %s", resp.status)
            return []

    async def get_attachment_file(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> Optional[bytes]:
        """Download attachment bytes, following HTTP redirects (e.g. S3 URLs)."""
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/items/{item_key}/file"
        # allow_redirects=True so aiohttp follows the S3 redirect automatically
        async with self.session.get(url, allow_redirects=True) as resp:
            if resp.status == 200:
                return await resp.read()
            if resp.status == 404:
                logger.debug("Attachment %s not found (404)", item_key)
                return None
            logger.warning("get_attachment_file %s: HTTP %s", item_key, resp.status)
            return None
