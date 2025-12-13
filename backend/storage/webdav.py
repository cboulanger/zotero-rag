"""
WebDAV remote storage implementation.

Provides WebDAV-based storage backend for vector database synchronization.
Compatible with Nextcloud, ownCloud, and other WebDAV servers.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

import httpx

from backend.storage.base import RemoteStorageBackend

logger = logging.getLogger(__name__)


class WebDAVStorage(RemoteStorageBackend):
    """WebDAV remote storage implementation."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        base_path: str = "/zotero-rag/vectors/",
        timeout: float = 30.0,
    ):
        """
        Initialize WebDAV storage.

        Args:
            base_url: WebDAV server URL (e.g., https://webdav.example.com)
            username: WebDAV username
            password: WebDAV password
            base_path: Base path for vector storage
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.base_path = base_path.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout

        # Create async HTTP client with authentication
        self.client = httpx.AsyncClient(
            auth=(username, password),
            timeout=timeout,
            follow_redirects=True,
        )

        logger.info(f"Initialized WebDAV storage at {self.base_url}{self.base_path}")

    def _get_full_url(self, remote_path: str) -> str:
        """Construct full WebDAV URL from remote path."""
        # Remove leading slash if present
        remote_path = remote_path.lstrip("/")
        return f"{self.base_url}{self.base_path}{remote_path}"

    def _get_metadata_path(self, remote_path: str) -> str:
        """Get path for metadata sidecar file."""
        return f"{remote_path}.meta.json"

    async def upload_file(
        self, local_path: Path, remote_path: str, metadata: Optional[dict] = None
    ) -> bool:
        """
        Upload file to WebDAV storage with optional metadata.

        Metadata is stored as a sidecar .meta.json file.
        """
        try:
            # Ensure parent directories exist
            await self._ensure_directory(Path(remote_path).parent.as_posix())

            # Upload main file
            url = self._get_full_url(remote_path)
            with open(local_path, "rb") as f:
                response = await self.client.put(url, content=f.read())
                response.raise_for_status()

            logger.info(f"Uploaded {local_path} to {remote_path}")

            # Upload metadata sidecar if provided
            if metadata:
                metadata_path = self._get_metadata_path(remote_path)
                metadata_url = self._get_full_url(metadata_path)
                metadata_json = json.dumps(metadata, indent=2)
                response = await self.client.put(
                    metadata_url, content=metadata_json.encode("utf-8")
                )
                response.raise_for_status()
                logger.debug(f"Uploaded metadata for {remote_path}")

            return True

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise PermissionError(f"Authentication failed: {e}")
            elif e.response.status_code == 403:
                raise PermissionError(f"Authorization failed: {e}")
            else:
                raise IOError(f"Upload failed: {e}")
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error during upload: {e}")
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            raise

    async def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Download file from WebDAV storage."""
        try:
            url = self._get_full_url(remote_path)
            response = await self.client.get(url)
            response.raise_for_status()

            # Ensure local directory exists
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Write file
            with open(local_path, "wb") as f:
                f.write(response.content)

            logger.info(f"Downloaded {remote_path} to {local_path}")
            return True

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            elif e.response.status_code == 401:
                raise PermissionError(f"Authentication failed: {e}")
            else:
                raise IOError(f"Download failed: {e}")
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error during download: {e}")
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            raise

    async def exists(self, remote_path: str) -> bool:
        """Check if remote file exists using HEAD request."""
        try:
            url = self._get_full_url(remote_path)
            response = await self.client.head(url)
            return response.status_code == 200
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error checking file existence: {e}")
        except Exception:
            return False

    async def get_metadata(self, remote_path: str) -> Optional[dict]:
        """
        Get file metadata from WebDAV.

        Returns metadata from sidecar file plus file properties from WebDAV.
        """
        try:
            # Get file properties using PROPFIND
            url = self._get_full_url(remote_path)
            headers = {"Depth": "0"}
            response = await self.client.request("PROPFIND", url, headers=headers)
            response.raise_for_status()

            # Parse basic metadata from WebDAV response
            # Note: Full XML parsing would be more robust, but this is simpler
            metadata = {
                "size": int(response.headers.get("Content-Length", 0)),
                "modified_at": response.headers.get("Last-Modified", ""),
                "content_type": response.headers.get("Content-Type", ""),
            }

            # Try to get custom metadata from sidecar file
            try:
                metadata_path = self._get_metadata_path(remote_path)
                metadata_url = self._get_full_url(metadata_path)
                meta_response = await self.client.get(metadata_url)
                if meta_response.status_code == 200:
                    custom = json.loads(meta_response.content)
                    metadata["custom"] = custom
            except Exception as e:
                logger.debug(f"No custom metadata found for {remote_path}: {e}")

            return metadata

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            raise IOError(f"Failed to get metadata: {e}")
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error getting metadata: {e}")
        except Exception as e:
            logger.error(f"Error getting metadata: {e}")
            raise

    async def delete_file(self, remote_path: str) -> bool:
        """Delete file from WebDAV storage."""
        try:
            # Delete main file
            url = self._get_full_url(remote_path)
            response = await self.client.delete(url)
            response.raise_for_status()

            logger.info(f"Deleted {remote_path}")

            # Try to delete metadata sidecar (ignore errors)
            try:
                metadata_path = self._get_metadata_path(remote_path)
                metadata_url = self._get_full_url(metadata_path)
                await self.client.delete(metadata_url)
            except Exception:
                pass

            return True

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            elif e.response.status_code in (401, 403):
                raise PermissionError(f"Authorization failed: {e}")
            raise IOError(f"Delete failed: {e}")
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error during delete: {e}")
        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            raise

    async def list_files(self, remote_prefix: str) -> list[str]:
        """
        List files in remote directory using PROPFIND.

        Returns list of file paths relative to base_path.
        """
        try:
            url = self._get_full_url(remote_prefix)
            headers = {"Depth": "1"}  # List immediate children
            response = await self.client.request("PROPFIND", url, headers=headers)
            response.raise_for_status()

            # Parse WebDAV XML response
            # This is a simplified implementation - full XML parsing would be better
            files = []
            content = response.text

            # Extract hrefs from response (very basic parsing)
            import re

            href_pattern = r'<d:href>([^<]+)</d:href>'
            hrefs = re.findall(href_pattern, content)

            for href in hrefs:
                # Skip the directory itself
                if href.endswith("/"):
                    continue
                # Skip metadata sidecar files
                if href.endswith(".meta.json"):
                    continue

                # Extract path relative to base_path
                if self.base_path in href:
                    relative_path = href.split(self.base_path, 1)[1]
                    files.append(relative_path)

            logger.debug(f"Listed {len(files)} files in {remote_prefix}")
            return files

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []  # Directory doesn't exist
            raise ConnectionError(f"Failed to list files: {e}")
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error listing files: {e}")
        except Exception as e:
            logger.error(f"Error listing files: {e}")
            raise

    async def test_connection(self) -> bool:
        """Test connection to WebDAV server."""
        try:
            # Try to list root directory
            url = self._get_full_url("")
            headers = {"Depth": "0"}
            response = await self.client.request("PROPFIND", url, headers=headers)
            response.raise_for_status()
            logger.info("WebDAV connection test successful")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise PermissionError(f"Authentication failed: {e}")
            raise ConnectionError(f"Connection test failed: {e}")
        except httpx.RequestError as e:
            raise ConnectionError(f"Connection error: {e}")

    async def _ensure_directory(self, remote_path: str) -> bool:
        """
        Ensure remote directory exists, creating if necessary.

        WebDAV requires MKCOL method to create collections (directories).
        """
        if not remote_path or remote_path == ".":
            return True

        try:
            # Check if exists
            url = self._get_full_url(remote_path)
            response = await self.client.head(url)
            if response.status_code == 200:
                return True

            # Create parent first (recursive)
            parent = str(Path(remote_path).parent)
            if parent and parent != ".":
                await self._ensure_directory(parent)

            # Create this directory
            response = await self.client.request("MKCOL", url)
            if response.status_code in (201, 405):  # 405 = already exists
                logger.debug(f"Created directory {remote_path}")
                return True
            response.raise_for_status()
            return True

        except Exception as e:
            logger.warning(f"Error ensuring directory {remote_path}: {e}")
            # Don't fail - directory might exist or be created later
            return False

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
        logger.debug("Closed WebDAV client")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
        return False
