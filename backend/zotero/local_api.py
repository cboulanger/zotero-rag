"""
Direct interface to Zotero local data API (localhost:23119).

This module provides direct HTTP access to Zotero's local server API,
which doesn't require API keys and provides access to all local libraries.
"""

import logging
from typing import Optional, Any
from pathlib import Path
import aiohttp
import asyncio

logger = logging.getLogger(__name__)


class ZoteroLocalAPI:
    """
    Client for Zotero's local data server API.

    The local API runs on localhost:23119 when Zotero is running and provides
    access to all libraries, items, and attachments without authentication.
    """

    def __init__(self, base_url: str = "http://localhost:23119"):
        """
        Initialize local API client.

        Args:
            base_url: Base URL for Zotero local API
        """
        self.base_url = base_url.rstrip("/")
        self.session: Optional[aiohttp.ClientSession] = None
        logger.info(f"Initialized ZoteroLocalAPI with base URL: {base_url}")

    async def _ensure_session(self):
        """Ensure aiohttp session exists."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self):
        """Close the HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def __aenter__(self):
        """Async context manager entry."""
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def check_connection(self) -> bool:
        """
        Check if Zotero local API is available.

        Returns:
            True if API is accessible, False otherwise
        """
        try:
            await self._ensure_session()
            async with self.session.get(f"{self.base_url}/connector/ping") as response:
                return response.status == 200
        except Exception as e:
            logger.debug(f"Connection check failed: {e}")
            return False

    async def list_libraries(self) -> list[dict[str, Any]]:
        """
        List all available libraries.

        Returns:
            List of library info dictionaries

        Raises:
            ConnectionError: If unable to connect to Zotero
        """
        try:
            await self._ensure_session()

            # Get user library info
            libraries = []

            # User library (ID 1 is typically the user library)
            async with self.session.get(
                f"{self.base_url}/users/1/items",
                params={"limit": 1}
            ) as response:
                if response.status == 200:
                    libraries.append({
                        "id": "1",
                        "name": "My Library",
                        "type": "user"
                    })

            # TODO: Add support for group libraries
            # This would require querying /groups endpoint

            return libraries

        except Exception as e:
            logger.error(f"Failed to list libraries: {e}")
            raise ConnectionError(f"Unable to connect to Zotero at {self.base_url}") from e

    async def get_library_items(
        self,
        library_id: str,
        library_type: str = "user",
        limit: Optional[int] = None,
        start: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Get items from a library.

        Args:
            library_id: Library ID
            library_type: "user" or "group"
            limit: Maximum number of items to return
            start: Starting index for pagination

        Returns:
            List of item dictionaries

        Raises:
            ConnectionError: If unable to connect to Zotero
        """
        try:
            await self._ensure_session()

            # Build URL based on library type
            if library_type == "user":
                url = f"{self.base_url}/users/{library_id}/items"
            else:
                url = f"{self.base_url}/groups/{library_id}/items"

            # Build query parameters
            params = {"start": start}
            if limit:
                params["limit"] = limit

            async with self.session.get(url, params=params) as response:
                if response.status == 200:
                    items = await response.json()
                    return items if isinstance(items, list) else []
                else:
                    logger.error(f"Failed to get items: HTTP {response.status}")
                    return []

        except Exception as e:
            logger.error(f"Failed to get library items: {e}")
            raise ConnectionError(f"Unable to get items from library {library_id}") from e

    async def get_item(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> Optional[dict[str, Any]]:
        """
        Get a specific item by key.

        Args:
            library_id: Library ID
            item_key: Item key
            library_type: "user" or "group"

        Returns:
            Item dictionary or None if not found
        """
        try:
            await self._ensure_session()

            if library_type == "user":
                url = f"{self.base_url}/users/{library_id}/items/{item_key}"
            else:
                url = f"{self.base_url}/groups/{library_id}/items/{item_key}"

            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.warning(f"Item {item_key} not found: HTTP {response.status}")
                    return None

        except Exception as e:
            logger.error(f"Failed to get item: {e}")
            return None

    async def get_item_children(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> list[dict[str, Any]]:
        """
        Get child items (attachments, notes) of an item.

        Args:
            library_id: Library ID
            item_key: Parent item key
            library_type: "user" or "group"

        Returns:
            List of child item dictionaries
        """
        try:
            await self._ensure_session()

            if library_type == "user":
                url = f"{self.base_url}/users/{library_id}/items/{item_key}/children"
            else:
                url = f"{self.base_url}/groups/{library_id}/items/{item_key}/children"

            async with self.session.get(url) as response:
                if response.status == 200:
                    children = await response.json()
                    return children if isinstance(children, list) else []
                else:
                    return []

        except Exception as e:
            logger.error(f"Failed to get item children: {e}")
            return []

    async def get_attachment_file(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> Optional[bytes]:
        """
        Get the binary content of an attachment.

        Args:
            library_id: Library ID
            item_key: Attachment item key
            library_type: "user" or "group"

        Returns:
            File content as bytes, or None if not available
        """
        try:
            await self._ensure_session()

            if library_type == "user":
                url = f"{self.base_url}/users/{library_id}/items/{item_key}/file"
            else:
                url = f"{self.base_url}/groups/{library_id}/items/{item_key}/file"

            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.warning(f"File for {item_key} not available: HTTP {response.status}")
                    return None

        except Exception as e:
            logger.error(f"Failed to get attachment file: {e}")
            return None

    async def get_fulltext(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> Optional[dict[str, Any]]:
        """
        Get full-text content for an item.

        Args:
            library_id: Library ID
            item_key: Item key
            library_type: "user" or "group"

        Returns:
            Dictionary with 'content' and 'indexedPages' fields, or None
        """
        try:
            await self._ensure_session()

            if library_type == "user":
                url = f"{self.base_url}/users/{library_id}/items/{item_key}/fulltext"
            else:
                url = f"{self.base_url}/groups/{library_id}/items/{item_key}/fulltext"

            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return None

        except Exception as e:
            logger.error(f"Failed to get fulltext: {e}")
            return None
