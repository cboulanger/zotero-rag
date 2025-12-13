"""
Abstract base class for remote storage backends.

Defines the interface that all storage backends (WebDAV, S3, etc.) must implement.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from datetime import datetime


class RemoteStorageBackend(ABC):
    """Abstract base class for remote storage backends."""

    @abstractmethod
    async def upload_file(
        self, local_path: Path, remote_path: str, metadata: Optional[dict] = None
    ) -> bool:
        """
        Upload file to remote storage.

        Args:
            local_path: Path to local file to upload
            remote_path: Remote path/key for the file
            metadata: Optional metadata to store with file

        Returns:
            True if upload successful

        Raises:
            ConnectionError: If connection to remote storage fails
            PermissionError: If authentication/authorization fails
            IOError: If file read/write fails
        """
        pass

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: Path) -> bool:
        """
        Download file from remote storage.

        Args:
            remote_path: Remote path/key of the file
            local_path: Local path where file should be saved

        Returns:
            True if download successful

        Raises:
            FileNotFoundError: If remote file doesn't exist
            ConnectionError: If connection to remote storage fails
            IOError: If file write fails
        """
        pass

    @abstractmethod
    async def exists(self, remote_path: str) -> bool:
        """
        Check if remote file exists.

        Args:
            remote_path: Remote path/key to check

        Returns:
            True if file exists

        Raises:
            ConnectionError: If connection to remote storage fails
        """
        pass

    @abstractmethod
    async def get_metadata(self, remote_path: str) -> Optional[dict]:
        """
        Get file metadata from remote storage.

        Args:
            remote_path: Remote path/key of the file

        Returns:
            Dictionary with metadata including:
            - size: File size in bytes
            - modified_at: Last modified timestamp (ISO 8601)
            - content_type: MIME type (if available)
            - custom: Any custom metadata stored with file

        Raises:
            FileNotFoundError: If remote file doesn't exist
            ConnectionError: If connection to remote storage fails
        """
        pass

    @abstractmethod
    async def delete_file(self, remote_path: str) -> bool:
        """
        Delete file from remote storage.

        Args:
            remote_path: Remote path/key of file to delete

        Returns:
            True if deletion successful

        Raises:
            FileNotFoundError: If remote file doesn't exist
            ConnectionError: If connection to remote storage fails
            PermissionError: If authorization fails
        """
        pass

    @abstractmethod
    async def list_files(self, remote_prefix: str) -> list[str]:
        """
        List files in remote directory/prefix.

        Args:
            remote_prefix: Remote directory path or prefix to list

        Returns:
            List of file paths/keys

        Raises:
            ConnectionError: If connection to remote storage fails
        """
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """
        Test connection to remote storage.

        Returns:
            True if connection successful

        Raises:
            ConnectionError: If connection fails
            PermissionError: If authentication fails
        """
        pass
