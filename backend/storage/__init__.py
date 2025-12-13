"""
Remote storage backends for vector database synchronization.

This module provides abstract interfaces and concrete implementations
for syncing vector databases to remote storage (WebDAV, S3).
"""

from backend.storage.base import RemoteStorageBackend
from backend.storage.factory import create_storage_backend

__all__ = ["RemoteStorageBackend", "create_storage_backend"]
