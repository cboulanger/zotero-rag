"""
S3 remote storage implementation.

Provides S3-based storage backend for vector database synchronization.
Compatible with AWS S3, MinIO, DigitalOcean Spaces, and other S3-compatible services.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

try:
    import aioboto3
    from botocore.exceptions import ClientError, NoCredentialsError
    AIOBOTO3_AVAILABLE = True
except ImportError:
    AIOBOTO3_AVAILABLE = False

from backend.storage.base import RemoteStorageBackend

logger = logging.getLogger(__name__)


class S3Storage(RemoteStorageBackend):
    """Amazon S3 / S3-compatible storage implementation."""

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        prefix: str = "zotero-rag/vectors/",
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        """
        Initialize S3 storage.

        Args:
            bucket: S3 bucket name
            region: AWS region
            prefix: Key prefix for vector storage
            endpoint_url: Custom endpoint for S3-compatible services (MinIO, etc.)
            access_key: AWS access key (if not using IAM)
            secret_key: AWS secret key
        """
        if not AIOBOTO3_AVAILABLE:
            raise ImportError(
                "aioboto3 is required for S3 storage. "
                "Install with: uv pip install aioboto3"
            )

        self.bucket = bucket
        self.region = region
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key

        # Create aioboto3 session
        self.session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

        logger.info(
            f"Initialized S3 storage at s3://{bucket}/{prefix} "
            f"(endpoint: {endpoint_url or 'AWS'})"
        )

    def _get_full_key(self, remote_path: str) -> str:
        """Construct full S3 key from remote path."""
        # Remove leading slash if present
        remote_path = remote_path.lstrip("/")
        return f"{self.prefix}{remote_path}"

    def _get_metadata_key(self, remote_path: str) -> str:
        """Get S3 key for metadata sidecar file."""
        return f"{self._get_full_key(remote_path)}.meta.json"

    async def upload_file(
        self, local_path: Path, remote_path: str, metadata: Optional[dict] = None
    ) -> bool:
        """
        Upload file to S3 storage with optional metadata.

        Uses multipart upload for large files. Metadata stored in S3 object metadata
        and as a sidecar .meta.json file for redundancy.
        """
        try:
            key = self._get_full_key(remote_path)

            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                # Prepare S3 object metadata
                extra_args = {}
                if metadata:
                    # Store metadata in S3 object metadata (limited to 2KB)
                    # Store only essential fields to stay under limit
                    s3_metadata = {
                        "library-id": str(metadata.get("library_id", "")),
                        "library-version": str(metadata.get("library_version", "")),
                        "created-at": str(metadata.get("created_at", "")),
                    }
                    extra_args["Metadata"] = s3_metadata

                # Upload main file
                await s3_client.upload_file(
                    str(local_path), self.bucket, key, ExtraArgs=extra_args or None
                )

                logger.info(f"Uploaded {local_path} to s3://{self.bucket}/{key}")

                # Upload full metadata as sidecar file
                if metadata:
                    metadata_key = self._get_metadata_key(remote_path)
                    metadata_json = json.dumps(metadata, indent=2)
                    await s3_client.put_object(
                        Bucket=self.bucket,
                        Key=metadata_key,
                        Body=metadata_json.encode("utf-8"),
                        ContentType="application/json",
                    )
                    logger.debug(f"Uploaded metadata for {remote_path}")

            return True

        except NoCredentialsError as e:
            raise PermissionError(f"AWS credentials not found: {e}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "403":
                raise PermissionError(f"Access denied: {e}")
            elif error_code == "404":
                raise FileNotFoundError(f"Bucket not found: {self.bucket}")
            else:
                raise IOError(f"S3 upload failed: {e}")
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            raise

    async def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Download file from S3 storage."""
        try:
            key = self._get_full_key(remote_path)

            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                # Ensure local directory exists
                local_path.parent.mkdir(parents=True, exist_ok=True)

                # Download file
                await s3_client.download_file(self.bucket, key, str(local_path))

            logger.info(f"Downloaded s3://{self.bucket}/{key} to {local_path}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            elif error_code == "403":
                raise PermissionError(f"Access denied: {e}")
            else:
                raise IOError(f"S3 download failed: {e}")
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            raise

    async def exists(self, remote_path: str) -> bool:
        """Check if remote file exists in S3."""
        try:
            key = self._get_full_key(remote_path)

            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                await s3_client.head_object(Bucket=self.bucket, Key=key)
                return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                return False
            raise ConnectionError(f"Error checking file existence: {e}")
        except Exception:
            return False

    async def get_metadata(self, remote_path: str) -> Optional[dict]:
        """
        Get file metadata from S3.

        Returns metadata from both S3 object metadata and sidecar file.
        """
        try:
            key = self._get_full_key(remote_path)

            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                # Get S3 object metadata
                response = await s3_client.head_object(Bucket=self.bucket, Key=key)

                metadata = {
                    "size": response.get("ContentLength", 0),
                    "modified_at": response.get("LastModified", "").isoformat()
                    if response.get("LastModified")
                    else "",
                    "content_type": response.get("ContentType", ""),
                    "etag": response.get("ETag", "").strip('"'),
                }

                # Try to get custom metadata from sidecar file
                try:
                    metadata_key = self._get_metadata_key(remote_path)
                    meta_response = await s3_client.get_object(
                        Bucket=self.bucket, Key=metadata_key
                    )
                    async with meta_response["Body"] as stream:
                        metadata_content = await stream.read()
                        custom = json.loads(metadata_content.decode("utf-8"))
                        metadata["custom"] = custom
                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    if error_code != "NoSuchKey":
                        logger.debug(f"No custom metadata found for {remote_path}")

            return metadata

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "404" or error_code == "NoSuchKey":
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            raise IOError(f"Failed to get metadata: {e}")
        except Exception as e:
            logger.error(f"Error getting metadata: {e}")
            raise

    async def delete_file(self, remote_path: str) -> bool:
        """Delete file from S3 storage."""
        try:
            key = self._get_full_key(remote_path)

            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                # Delete main file
                await s3_client.delete_object(Bucket=self.bucket, Key=key)

                logger.info(f"Deleted s3://{self.bucket}/{key}")

                # Try to delete metadata sidecar (ignore errors)
                try:
                    metadata_key = self._get_metadata_key(remote_path)
                    await s3_client.delete_object(Bucket=self.bucket, Key=metadata_key)
                except Exception:
                    pass

            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchKey":
                raise FileNotFoundError(f"Remote file not found: {remote_path}")
            elif error_code == "403":
                raise PermissionError(f"Access denied: {e}")
            raise IOError(f"Delete failed: {e}")
        except Exception as e:
            logger.error(f"Error deleting file: {e}")
            raise

    async def list_files(self, remote_prefix: str) -> list[str]:
        """
        List files in S3 prefix.

        Returns list of file keys relative to storage prefix.
        """
        try:
            prefix = self._get_full_key(remote_prefix)

            files = []
            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                # List objects with pagination
                paginator = s3_client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(
                    Bucket=self.bucket, Prefix=prefix
                ):
                    if "Contents" in page:
                        for obj in page["Contents"]:
                            key = obj["Key"]
                            # Skip metadata sidecar files
                            if key.endswith(".meta.json"):
                                continue
                            # Return path relative to storage prefix
                            if key.startswith(self.prefix):
                                relative_path = key[len(self.prefix) :]
                                files.append(relative_path)

            logger.debug(f"Listed {len(files)} files in {remote_prefix}")
            return files

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "NoSuchBucket":
                raise ConnectionError(f"Bucket not found: {self.bucket}")
            raise ConnectionError(f"Failed to list files: {e}")
        except Exception as e:
            logger.error(f"Error listing files: {e}")
            raise

    async def test_connection(self) -> bool:
        """Test connection to S3."""
        try:
            async with self.session.client(
                "s3", endpoint_url=self.endpoint_url
            ) as s3_client:
                # Try to head the bucket
                await s3_client.head_bucket(Bucket=self.bucket)
                logger.info("S3 connection test successful")
                return True

        except NoCredentialsError as e:
            raise PermissionError(f"AWS credentials not found: {e}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "403":
                raise PermissionError(f"Access denied to bucket: {self.bucket}")
            elif error_code == "404":
                raise ConnectionError(f"Bucket not found: {self.bucket}")
            raise ConnectionError(f"Connection test failed: {e}")
        except Exception as e:
            raise ConnectionError(f"Connection error: {e}")

    async def close(self):
        """Close S3 client (cleanup method for consistency)."""
        # aioboto3 sessions are closed automatically
        logger.debug("S3 storage cleanup complete")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
        return False
