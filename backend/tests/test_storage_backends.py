"""
Unit tests for remote storage backends.

Tests the abstract interface and concrete implementations (WebDAV, S3).
"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import tempfile
import json

from backend.storage.base import RemoteStorageBackend
from backend.storage.webdav import WebDAVStorage
from backend.storage.s3 import S3Storage, AIOBOTO3_AVAILABLE
from backend.storage.factory import create_storage_backend, create_storage_from_settings


class TestStorageBackendInterface:
    """Test that storage backends implement the required interface."""

    def test_webdav_implements_interface(self):
        """Test that WebDAVStorage implements RemoteStorageBackend."""
        assert issubclass(WebDAVStorage, RemoteStorageBackend)

    def test_s3_implements_interface(self):
        """Test that S3Storage implements RemoteStorageBackend (if available)."""
        if AIOBOTO3_AVAILABLE:
            assert issubclass(S3Storage, RemoteStorageBackend)


class TestStorageFactory:
    """Test storage backend factory."""

    def test_create_webdav_backend(self):
        """Test creating WebDAV backend from factory."""
        storage = create_storage_backend(
            "webdav",
            base_url="https://webdav.example.com",
            username="user",
            password="pass",
        )
        assert isinstance(storage, WebDAVStorage)
        assert storage.base_url == "https://webdav.example.com"

    @pytest.mark.skipif(not AIOBOTO3_AVAILABLE, reason="aioboto3 not installed")
    def test_create_s3_backend(self):
        """Test creating S3 backend from factory."""
        storage = create_storage_backend(
            "s3",
            bucket="test-bucket",
            access_key="test-key",
            secret_key="test-secret",
        )
        assert isinstance(storage, S3Storage)
        assert storage.bucket == "test-bucket"

    def test_create_invalid_backend_type(self):
        """Test that invalid backend type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid backend type"):
            create_storage_backend("invalid")

    def test_create_webdav_missing_required_params(self):
        """Test that missing required WebDAV params raises ValueError."""
        with pytest.raises(ValueError, match="Missing required WebDAV parameters"):
            create_storage_backend("webdav", base_url="https://example.com")

    @pytest.mark.skipif(not AIOBOTO3_AVAILABLE, reason="aioboto3 not installed")
    def test_create_s3_missing_required_params(self):
        """Test that missing required S3 params raises ValueError."""
        with pytest.raises(ValueError, match="Missing required S3 parameter"):
            create_storage_backend("s3")


class TestWebDAVStorage:
    """Test WebDAV storage implementation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.storage = WebDAVStorage(
            base_url="https://webdav.example.com",
            username="testuser",
            password="testpass",
            base_path="/test/",
        )

    async def test_get_full_url(self):
        """Test URL construction."""
        url = self.storage._get_full_url("snapshot.tar.gz")
        assert url == "https://webdav.example.com/test/snapshot.tar.gz"

    async def test_get_metadata_path(self):
        """Test metadata path construction."""
        meta_path = self.storage._get_metadata_path("snapshot.tar.gz")
        assert meta_path == "snapshot.tar.gz.meta.json"

    @pytest.mark.asyncio
    async def test_upload_file_success(self):
        """Test successful file upload."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = Path(tmp.name)

        try:
            # Mock HTTP client
            with patch.object(self.storage.client, "put") as mock_put:
                mock_response = Mock()
                mock_response.raise_for_status = Mock()
                mock_put.return_value = mock_response

                result = await self.storage.upload_file(
                    tmp_path, "test.tar.gz", metadata={"test": "data"}
                )

                assert result is True
                assert mock_put.call_count == 2  # File + metadata

        finally:
            tmp_path.unlink()
            await self.storage.close()

    @pytest.mark.asyncio
    async def test_exists_true(self):
        """Test exists() when file exists."""
        with patch.object(self.storage.client, "head") as mock_head:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_head.return_value = mock_response

            result = await self.storage.exists("test.tar.gz")
            assert result is True

        await self.storage.close()

    @pytest.mark.asyncio
    async def test_exists_false(self):
        """Test exists() when file doesn't exist."""
        with patch.object(self.storage.client, "head") as mock_head:
            mock_response = Mock()
            mock_response.status_code = 404
            mock_head.return_value = mock_response

            result = await self.storage.exists("test.tar.gz")
            assert result is False

        await self.storage.close()


@pytest.mark.skipif(not AIOBOTO3_AVAILABLE, reason="aioboto3 not installed")
class TestS3Storage:
    """Test S3 storage implementation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.storage = S3Storage(
            bucket="test-bucket",
            region="us-east-1",
            prefix="test/",
            access_key="test-key",
            secret_key="test-secret",
        )

    def test_get_full_key(self):
        """Test S3 key construction."""
        key = self.storage._get_full_key("snapshot.tar.gz")
        assert key == "test/snapshot.tar.gz"

    def test_get_metadata_key(self):
        """Test metadata key construction."""
        meta_key = self.storage._get_metadata_key("snapshot.tar.gz")
        assert meta_key == "test/snapshot.tar.gz.meta.json"

    @pytest.mark.asyncio
    async def test_exists_returns_true_when_object_exists(self):
        """Test exists() when S3 object exists."""
        with patch.object(self.storage.session, "client") as mock_client:
            mock_s3 = AsyncMock()
            mock_s3.head_object = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_s3

            result = await self.storage.exists("test.tar.gz")
            assert result is True

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        async with self.storage as storage:
            assert storage == self.storage


class TestStorageFromSettings:
    """Test creating storage from settings."""

    def test_sync_disabled_returns_none(self):
        """Test that disabled sync returns None."""
        mock_settings = Mock()
        mock_settings.sync_enabled = False

        result = create_storage_from_settings(mock_settings)
        assert result is None

    def test_webdav_backend_from_settings(self):
        """Test creating WebDAV backend from settings."""
        mock_settings = Mock()
        mock_settings.sync_enabled = True
        mock_settings.sync_backend = "webdav"
        mock_settings.sync_webdav_url = "https://webdav.example.com"
        mock_settings.sync_webdav_username = "user"
        mock_settings.sync_webdav_password = "pass"
        mock_settings.sync_webdav_base_path = "/test/"

        storage = create_storage_from_settings(mock_settings)
        assert isinstance(storage, WebDAVStorage)

    @pytest.mark.skipif(not AIOBOTO3_AVAILABLE, reason="aioboto3 not installed")
    def test_s3_backend_from_settings(self):
        """Test creating S3 backend from settings."""
        mock_settings = Mock()
        mock_settings.sync_enabled = True
        mock_settings.sync_backend = "s3"
        mock_settings.sync_s3_bucket = "test-bucket"
        mock_settings.sync_s3_region = "us-east-1"
        mock_settings.sync_s3_prefix = "test/"
        mock_settings.sync_s3_endpoint_url = None
        mock_settings.sync_s3_access_key = "test-key"
        mock_settings.sync_s3_secret_key = "test-secret"

        storage = create_storage_from_settings(mock_settings)
        assert isinstance(storage, S3Storage)
