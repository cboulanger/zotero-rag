"""
Factory for creating storage backend instances.

Provides a centralized way to create storage backends from settings.
"""

import logging
from typing import Literal, Optional

from backend.storage.base import RemoteStorageBackend
from backend.storage.webdav import WebDAVStorage
from backend.storage.s3 import S3Storage

logger = logging.getLogger(__name__)


def create_storage_backend(
    backend_type: Literal["webdav", "s3"],
    **kwargs
) -> RemoteStorageBackend:
    """
    Create a storage backend instance.

    Args:
        backend_type: Type of storage backend ("webdav" or "s3")
        **kwargs: Backend-specific configuration parameters

    Returns:
        Storage backend instance

    Raises:
        ValueError: If backend_type is invalid or required parameters are missing

    Examples:
        >>> # WebDAV backend
        >>> storage = create_storage_backend(
        ...     "webdav",
        ...     base_url="https://webdav.example.com",
        ...     username="user",
        ...     password="pass"
        ... )

        >>> # S3 backend
        >>> storage = create_storage_backend(
        ...     "s3",
        ...     bucket="my-bucket",
        ...     access_key="...",
        ...     secret_key="..."
        ... )
    """
    if backend_type == "webdav":
        return _create_webdav_backend(**kwargs)
    elif backend_type == "s3":
        return _create_s3_backend(**kwargs)
    else:
        raise ValueError(
            f"Invalid backend type: {backend_type}. "
            "Must be 'webdav' or 's3'"
        )


def _create_webdav_backend(**kwargs) -> WebDAVStorage:
    """Create WebDAV storage backend."""
    required = ["base_url", "username", "password"]
    missing = [k for k in required if k not in kwargs or not kwargs[k]]

    if missing:
        raise ValueError(
            f"Missing required WebDAV parameters: {', '.join(missing)}"
        )

    return WebDAVStorage(
        base_url=kwargs["base_url"],
        username=kwargs["username"],
        password=kwargs["password"],
        base_path=kwargs.get("base_path", "/zotero-rag/vectors/"),
        timeout=kwargs.get("timeout", 30.0),
    )


def _create_s3_backend(**kwargs) -> S3Storage:
    """Create S3 storage backend."""
    if "bucket" not in kwargs or not kwargs["bucket"]:
        raise ValueError("Missing required S3 parameter: bucket")

    return S3Storage(
        bucket=kwargs["bucket"],
        region=kwargs.get("region", "us-east-1"),
        prefix=kwargs.get("prefix", "zotero-rag/vectors/"),
        endpoint_url=kwargs.get("endpoint_url"),
        access_key=kwargs.get("access_key"),
        secret_key=kwargs.get("secret_key"),
    )


def create_storage_from_settings(settings) -> Optional[RemoteStorageBackend]:
    """
    Create storage backend from application settings.

    Args:
        settings: Application settings object with sync configuration

    Returns:
        Storage backend instance or None if sync is disabled

    Raises:
        ValueError: If configuration is invalid
    """
    if not settings.sync_enabled:
        logger.info("Sync is disabled, no storage backend created")
        return None

    backend_type = settings.sync_backend

    if backend_type == "webdav":
        return create_storage_backend(
            "webdav",
            base_url=settings.sync_webdav_url,
            username=settings.sync_webdav_username,
            password=settings.sync_webdav_password,
            base_path=settings.sync_webdav_base_path,
        )
    elif backend_type == "s3":
        return create_storage_backend(
            "s3",
            bucket=settings.sync_s3_bucket,
            region=settings.sync_s3_region,
            prefix=settings.sync_s3_prefix,
            endpoint_url=settings.sync_s3_endpoint_url,
            access_key=settings.sync_s3_access_key,
            secret_key=settings.sync_s3_secret_key,
        )
    else:
        raise ValueError(f"Unsupported sync backend: {backend_type}")
