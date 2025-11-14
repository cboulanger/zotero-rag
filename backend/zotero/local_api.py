"""
Direct interface to Zotero local data API (localhost:23119).

This module provides direct HTTP access to Zotero's local server API,
which doesn't require API keys and provides access to all local libraries.
"""

import logging
from typing import Optional, Any, Tuple
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

    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize local API client.

        Args:
            base_url: Base URL for Zotero local API. If None, uses ZOTERO_API_URL from settings.
        """
        if base_url is None:
            from backend.config.settings import get_settings
            settings = get_settings()
            base_url = settings.zotero_api_url

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
            # Note: userID 0 refers to the current logged-in user's library
            libraries = []

            # Try to fetch items to verify library exists
            async with self.session.get(
                f"{self.base_url}/api/users/0/items",
                params={"limit": 1}
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    # Extract library info from first item if available
                    if data and len(data) > 0 and "library" in data[0]:
                        lib_info = data[0]["library"]
                        libraries.append({
                            "id": str(lib_info.get("id", "1")),
                            "name": lib_info.get("name", "My Library"),
                            "type": lib_info.get("type", "user")
                        })
                    else:
                        # Fallback if no items exist yet
                        libraries.append({
                            "id": "1",
                            "name": "My Library",
                            "type": "user"
                        })
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to fetch libraries: {response.status} - {error_text}")
                    raise ConnectionError(f"Zotero API returned {response.status}: {error_text}")

            # Fetch group libraries
            # Note: User ID 0 refers to current user's groups
            try:
                async with self.session.get(
                    f"{self.base_url}/api/users/0/groups"
                ) as response:
                    if response.status == 200:
                        groups_data = await response.json()
                        for group in groups_data:
                            libraries.append({
                                "id": str(group.get("data", {}).get("id", group.get("id"))),
                                "name": group.get("data", {}).get("name", "Unnamed Group"),
                                "type": "group"
                            })
                    else:
                        # Groups endpoint may not be available or no groups exist
                        logger.debug(f"Groups endpoint returned {response.status}")
            except Exception as e:
                # Non-fatal: just log and continue with user library only
                logger.debug(f"Could not fetch groups: {e}")

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

        DEPRECATED: Use get_library_items_since() for version-aware fetching.

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
        return await self.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None,
            limit=limit,
            start=start
        )

    async def get_library_items_since(
        self,
        library_id: str,
        library_type: str = "user",
        since_version: Optional[int] = None,
        limit: Optional[int] = None,
        start: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Get items from a library, optionally filtering by version.

        Args:
            library_id: Library ID
            library_type: "user" or "group"
            since_version: If provided, only return items modified since this version
            limit: Maximum number of items to return per request (pagination handled internally if None)
            start: Starting index for pagination

        Returns:
            List of item dictionaries with full metadata including 'version' field

        Raises:
            ConnectionError: If unable to connect to Zotero
        """
        try:
            await self._ensure_session()

            # Build URL based on library type
            # Note: For local API, userID 0 refers to the current user
            if library_type == "user":
                url = f"{self.base_url}/api/users/0/items"
            else:
                url = f"{self.base_url}/api/groups/{library_id}/items"

            # Build query parameters
            params = {"start": start}
            if limit:
                params["limit"] = limit
            else:
                params["limit"] = 100  # Default batch size for pagination

            if since_version is not None:
                params["since"] = since_version
                logger.info(f"Fetching items since version {since_version}")

            # Handle pagination if no limit specified
            all_items = []
            current_start = start

            while True:
                params["start"] = current_start

                async with self.session.get(url, params=params) as response:
                    if response.status == 200:
                        items = await response.json()
                        if not isinstance(items, list):
                            break

                        all_items.extend(items)

                        # If we got fewer items than the limit, we've reached the end
                        if len(items) < params["limit"]:
                            break

                        # If original limit was specified, don't paginate beyond it
                        if limit and len(all_items) >= limit:
                            all_items = all_items[:limit]
                            break

                        current_start += len(items)

                    else:
                        logger.error(f"Failed to get items: HTTP {response.status}")
                        if current_start == start:
                            # First request failed
                            return []
                        else:
                            # Subsequent request failed, return what we have
                            break

            logger.info(f"Retrieved {len(all_items)} items from library {library_id}")
            return all_items

        except Exception as e:
            logger.error(f"Failed to get library items: {e}")
            raise ConnectionError(f"Unable to get items from library {library_id}") from e

    async def get_library_version_range(
        self,
        library_id: str,
        library_type: str = "user",
    ) -> Tuple[int, int]:
        """
        Get the min and max version numbers in a library.

        Args:
            library_id: Library ID
            library_type: "user" or "group"

        Returns:
            (min_version, max_version) tuple

        Raises:
            ConnectionError: If unable to connect to Zotero
        """
        try:
            # Fetch items to get version range
            items = await self.get_library_items_since(
                library_id=library_id,
                library_type=library_type,
                since_version=None,
                limit=1000  # Should be enough to get accurate version range
            )

            if not items:
                return (0, 0)

            # Extract version numbers
            versions = []
            for item in items:
                version = item.get("version")
                if version is not None:
                    versions.append(version)

            if not versions:
                return (0, 0)

            return (min(versions), max(versions))

        except Exception as e:
            logger.error(f"Failed to get library version range: {e}")
            raise ConnectionError(f"Unable to get version range for library {library_id}") from e

    async def get_item_with_version(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user",
    ) -> Optional[dict[str, Any]]:
        """
        Get a single item with full version information.

        Args:
            library_id: Library ID
            item_key: Item key
            library_type: "user" or "group"

        Returns:
            Item dict with 'version', 'data', etc., or None if not found
        """
        try:
            await self._ensure_session()

            # Build URL based on library type
            if library_type == "user":
                url = f"{self.base_url}/api/users/0/items/{item_key}"
            else:
                url = f"{self.base_url}/api/groups/{library_id}/items/{item_key}"

            async with self.session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 404:
                    logger.warning(f"Item {item_key} not found")
                    return None
                else:
                    logger.error(f"Failed to get item {item_key}: HTTP {response.status}")
                    return None

        except Exception as e:
            logger.error(f"Failed to get item with version: {e}")
            return None

    async def get_attachment_with_version(
        self,
        library_id: str,
        attachment_key: str,
        library_type: str = "user",
    ) -> Optional[dict[str, Any]]:
        """
        Get attachment metadata including version.

        Note: Attachments are items too, so we use the same endpoint.

        Args:
            library_id: Library ID
            attachment_key: Attachment item key
            library_type: "user" or "group"

        Returns:
            Attachment item dict with 'version', 'data', etc., or None if not found
        """
        return await self.get_item_with_version(
            library_id=library_id,
            item_key=attachment_key,
            library_type=library_type
        )

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

            # Build URL with /api/ prefix for local API
            if library_type == "user":
                url = f"{self.base_url}/api/users/0/items/{item_key}/children"
            else:
                url = f"{self.base_url}/api/groups/{library_id}/items/{item_key}/children"

            async with self.session.get(url) as response:
                if response.status == 200:
                    children = await response.json()
                    return children if isinstance(children, list) else []
                else:
                    logger.debug(f"No children found for item {item_key}: HTTP {response.status}")
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

            # Build URL with /api/ prefix for local API
            if library_type == "user":
                url = f"{self.base_url}/api/users/0/items/{item_key}/file"
            else:
                url = f"{self.base_url}/api/groups/{library_id}/items/{item_key}/file"

            # Don't follow redirects - we'll handle file:// URLs ourselves
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status == 200:
                    return await response.read()
                elif response.status in (301, 302, 303, 307, 308):
                    # Local API redirects to file:// URL for local files
                    file_url = response.headers.get("Location")
                    if file_url and file_url.startswith("file://"):
                        # Extract file path from file:// URL
                        from urllib.parse import unquote, urlparse
                        from pathlib import Path

                        parsed = urlparse(file_url)
                        file_path = unquote(parsed.path)

                        # On Windows, remove leading slash from /C:/... paths
                        if file_path.startswith("/") and len(file_path) > 2 and file_path[2] == ":":
                            file_path = file_path[1:]

                        # Read file from filesystem
                        path = Path(file_path)
                        if path.exists():
                            return path.read_bytes()
                        else:
                            logger.warning(f"File not found on filesystem: {file_path}")
                            return None
                    else:
                        logger.warning(f"Unexpected redirect for {item_key}: {file_url}")
                        return None
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
