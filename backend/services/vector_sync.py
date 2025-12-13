"""
Vector database synchronization service.

Orchestrates version checking, pull/push operations, and conflict resolution
for syncing vector databases with remote storage backends.
"""

import logging
import re
from pathlib import Path
from typing import Literal, Optional
from datetime import datetime

from backend.db.vector_store import VectorStore
from backend.services.snapshot_manager import SnapshotManager
from backend.storage.base import RemoteStorageBackend
from backend.models.library import LibraryIndexMetadata
from backend.zotero.local_api import ZoteroLocalAPI

logger = logging.getLogger(__name__)


class SyncStatus:
    """Synchronization status constants."""

    LOCAL_NEWER = "local_newer"
    REMOTE_NEWER = "remote_newer"
    SAME = "same"
    DIVERGED = "diverged"
    NO_LOCAL = "no_local"
    NO_REMOTE = "no_remote"


class VectorSyncService:
    """Orchestrates vector database synchronization."""

    def __init__(
        self,
        vector_store: VectorStore,
        snapshot_manager: SnapshotManager,
        storage_backend: RemoteStorageBackend,
        zotero_client: Optional[ZoteroLocalAPI] = None,
    ):
        """
        Initialize sync service.

        Args:
            vector_store: VectorStore instance
            snapshot_manager: SnapshotManager instance
            storage_backend: Remote storage backend
            zotero_client: Optional Zotero local API client
        """
        self.vector_store = vector_store
        self.snapshot_manager = snapshot_manager
        self.storage = storage_backend
        self.zotero_client = zotero_client

        logger.info("Initialized VectorSyncService")

    def _get_remote_snapshot_path(self, library_id: str, version: int) -> str:
        """
        Get remote storage path for a library snapshot.

        Args:
            library_id: Library ID
            version: Library version

        Returns:
            Remote path (e.g., "library_6297749_v12345.tar.gz")
        """
        return f"library_{library_id}_v{version}.tar.gz"

    def _parse_snapshot_filename(self, filename: str) -> Optional[tuple[str, int]]:
        """
        Parse library ID and version from snapshot filename.

        Args:
            filename: Snapshot filename

        Returns:
            Tuple of (library_id, version) or None if invalid format
        """
        match = re.match(r"library_(\d+)_v(\d+)\.tar\.(gz|bz2|xz)", filename)
        if match:
            return match.group(1), int(match.group(2))
        return None

    async def _get_latest_remote_snapshot(
        self, library_id: str
    ) -> Optional[tuple[str, dict]]:
        """
        Find the latest remote snapshot for a library.

        Args:
            library_id: Library ID

        Returns:
            Tuple of (remote_path, metadata) or None if not found
        """
        try:
            # List all files in remote storage
            files = await self.storage.list_files("")

            # Filter for this library's snapshots
            library_snapshots = []
            for file in files:
                parsed = self._parse_snapshot_filename(file)
                if parsed and parsed[0] == library_id:
                    library_snapshots.append((file, parsed[1]))  # (path, version)

            if not library_snapshots:
                return None

            # Get the highest version
            latest_file, latest_version = max(library_snapshots, key=lambda x: x[1])

            # Get metadata
            metadata = await self.storage.get_metadata(latest_file)
            if not metadata:
                logger.warning(f"No metadata found for {latest_file}")
                return None

            return latest_file, metadata

        except Exception as e:
            logger.error(f"Error finding remote snapshot for library {library_id}: {e}")
            return None

    def _compare_versions(
        self,
        local_meta: Optional[LibraryIndexMetadata],
        remote_meta: Optional[dict],
    ) -> str:
        """
        Compare local and remote library versions.

        Args:
            local_meta: Local library metadata
            remote_meta: Remote snapshot metadata

        Returns:
            Status constant (LOCAL_NEWER, REMOTE_NEWER, SAME, NO_LOCAL, NO_REMOTE)
        """
        if not local_meta and not remote_meta:
            return SyncStatus.NO_REMOTE

        if not local_meta:
            return SyncStatus.NO_LOCAL

        if not remote_meta:
            return SyncStatus.NO_REMOTE

        local_version = local_meta.last_indexed_version
        remote_version = remote_meta.get("library_version", 0)

        if local_version == remote_version:
            return SyncStatus.SAME

        if local_version > remote_version:
            return SyncStatus.LOCAL_NEWER

        if local_version < remote_version:
            return SyncStatus.REMOTE_NEWER

        # Should not reach here with monotonic Zotero versions
        return SyncStatus.DIVERGED

    async def should_pull(self, library_id: str) -> tuple[bool, str]:
        """
        Determine if library should be pulled from remote.

        Decision logic:
        1. No local copy exists -> PULL
        2. Remote doesn't exist -> NO_PULL
        3. Remote version > local version -> PULL
        4. Local version >= remote version -> NO_PULL

        Args:
            library_id: Library ID

        Returns:
            Tuple of (should_pull, reason)
        """
        try:
            # Get local metadata
            local_meta = self.vector_store.get_library_metadata(library_id)

            # Find latest remote snapshot
            remote_result = await self._get_latest_remote_snapshot(library_id)

            # Compare versions
            remote_meta = remote_result[1] if remote_result else None
            status = self._compare_versions(local_meta, remote_meta)

            if status == SyncStatus.NO_LOCAL:
                return True, "No local copy exists, pull required"

            if status == SyncStatus.NO_REMOTE:
                return False, "No remote copy exists"

            if status == SyncStatus.REMOTE_NEWER:
                local_v = local_meta.last_indexed_version if local_meta else 0
                remote_v = remote_meta.get("library_version", 0) if remote_meta else 0
                return True, f"Remote version {remote_v} > local version {local_v}"

            if status == SyncStatus.SAME:
                return False, "Versions are identical"

            if status == SyncStatus.LOCAL_NEWER:
                return False, "Local version is newer"

            return False, f"Unknown status: {status}"

        except Exception as e:
            logger.error(f"Error checking pull status for library {library_id}: {e}")
            return False, f"Error: {str(e)}"

    async def pull_library(
        self, library_id: str, force: bool = False
    ) -> dict[str, any]:
        """
        Pull library vectors from remote storage.

        Process:
        1. Check if pull is needed (unless force=True)
        2. Download snapshot from remote storage
        3. Validate snapshot metadata
        4. Restore snapshot to local Qdrant
        5. Update local library metadata
        6. Cleanup temporary files

        Args:
            library_id: Library ID
            force: Force pull even if not needed

        Returns:
            Statistics dict with keys:
            - success: bool
            - downloaded_bytes: int
            - restore_time: float (seconds)
            - chunks_restored: int
            - library_version: int
            - message: str

        Raises:
            ValueError: If pull cannot be performed
            RuntimeError: If pull operation fails
        """
        start_time = datetime.now()
        logger.info(f"Starting pull for library {library_id} (force={force})")

        try:
            # Check if pull is needed
            if not force:
                should_pull, reason = await self.should_pull(library_id)
                if not should_pull:
                    return {
                        "success": False,
                        "message": f"Pull not needed: {reason}",
                        "downloaded_bytes": 0,
                        "restore_time": 0,
                        "chunks_restored": 0,
                        "library_version": 0,
                    }

            # Find latest remote snapshot
            remote_result = await self._get_latest_remote_snapshot(library_id)
            if not remote_result:
                raise ValueError(f"No remote snapshot found for library {library_id}")

            remote_path, remote_meta = remote_result

            # Download snapshot
            logger.info(f"Downloading snapshot: {remote_path}")
            temp_dir = self.snapshot_manager.temp_dir
            local_snapshot = temp_dir / Path(remote_path).name

            download_success = await self.storage.download_file(
                remote_path, local_snapshot
            )
            if not download_success:
                raise RuntimeError(f"Failed to download snapshot from {remote_path}")

            downloaded_bytes = local_snapshot.stat().st_size
            logger.info(f"Downloaded {downloaded_bytes} bytes")

            # Validate metadata
            snapshot_info = await self.snapshot_manager.get_snapshot_info(
                local_snapshot
            )
            if snapshot_info["library_id"] != library_id:
                raise ValueError(
                    f"Snapshot library ID mismatch: expected {library_id}, "
                    f"got {snapshot_info['library_id']}"
                )

            # Restore snapshot
            logger.info("Restoring snapshot to local vector store")
            restore_success = await self.snapshot_manager.restore_snapshot(
                local_snapshot, library_id
            )
            if not restore_success:
                raise RuntimeError("Failed to restore snapshot")

            # Get restored library metadata
            lib_meta = self.vector_store.get_library_metadata(library_id)
            chunks_restored = lib_meta.total_chunks if lib_meta else 0
            library_version = lib_meta.last_indexed_version if lib_meta else 0

            # Cleanup
            if local_snapshot.exists():
                local_snapshot.unlink()

            elapsed = (datetime.now() - start_time).total_seconds()

            logger.info(
                f"Pull completed: {chunks_restored} chunks, "
                f"version {library_version}, {elapsed:.2f}s"
            )

            return {
                "success": True,
                "message": f"Successfully pulled library {library_id}",
                "downloaded_bytes": downloaded_bytes,
                "restore_time": elapsed,
                "chunks_restored": chunks_restored,
                "library_version": library_version,
            }

        except Exception as e:
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.error(f"Pull failed for library {library_id}: {e}")
            return {
                "success": False,
                "message": f"Pull failed: {str(e)}",
                "downloaded_bytes": 0,
                "restore_time": elapsed,
                "chunks_restored": 0,
                "library_version": 0,
            }

    async def push_library(
        self, library_id: str, force: bool = False
    ) -> dict[str, any]:
        """
        Push library vectors to remote storage.

        Process:
        1. Get local library metadata
        2. Check if push needed (unless force=True)
        3. Create snapshot
        4. Upload to remote storage with metadata
        5. Verify upload
        6. Cleanup temporary files

        Args:
            library_id: Library ID
            force: Force push even if remote is newer

        Returns:
            Statistics dict with keys:
            - success: bool
            - uploaded_bytes: int
            - snapshot_time: float (seconds)
            - chunks_pushed: int
            - library_version: int
            - message: str

        Raises:
            ValueError: If push cannot be performed
            RuntimeError: If push operation fails
        """
        start_time = datetime.now()
        logger.info(f"Starting push for library {library_id} (force={force})")

        try:
            # Get local metadata
            local_meta = self.vector_store.get_library_metadata(library_id)
            if not local_meta:
                raise ValueError(f"Library {library_id} not indexed locally")

            # Check for conflicts unless force=True
            if not force:
                remote_result = await self._get_latest_remote_snapshot(library_id)
                if remote_result:
                    remote_meta = remote_result[1]
                    status = self._compare_versions(local_meta, remote_meta)

                    if status == SyncStatus.REMOTE_NEWER:
                        raise ValueError(
                            f"Remote version is newer than local. "
                            f"Pull first or use force=True to override."
                        )

            # Create snapshot
            logger.info(f"Creating snapshot for library {library_id}")
            snapshot_path = await self.snapshot_manager.create_snapshot(library_id)
            snapshot_bytes = snapshot_path.stat().st_size
            logger.info(f"Created snapshot: {snapshot_bytes} bytes")

            # Upload to remote storage
            remote_path = self._get_remote_snapshot_path(
                library_id, local_meta.last_indexed_version
            )
            logger.info(f"Uploading snapshot to {remote_path}")

            # Prepare metadata for upload
            upload_metadata = {
                "library_id": library_id,
                "library_version": local_meta.last_indexed_version,
                "uploaded_at": datetime.now().isoformat(),
                "total_chunks": local_meta.total_chunks,
                "total_items": local_meta.total_items_indexed,
            }

            upload_success = await self.storage.upload_file(
                snapshot_path, remote_path, metadata=upload_metadata
            )
            if not upload_success:
                raise RuntimeError(f"Failed to upload snapshot to {remote_path}")

            # Verify upload
            exists = await self.storage.exists(remote_path)
            if not exists:
                raise RuntimeError(
                    f"Upload verification failed: {remote_path} not found"
                )

            # Cleanup
            if snapshot_path.exists():
                snapshot_path.unlink()

            elapsed = (datetime.now() - start_time).total_seconds()

            logger.info(
                f"Push completed: {local_meta.total_chunks} chunks, "
                f"version {local_meta.last_indexed_version}, {elapsed:.2f}s"
            )

            return {
                "success": True,
                "message": f"Successfully pushed library {library_id}",
                "uploaded_bytes": snapshot_bytes,
                "snapshot_time": elapsed,
                "chunks_pushed": local_meta.total_chunks,
                "library_version": local_meta.last_indexed_version,
            }

        except Exception as e:
            elapsed = (datetime.now() - start_time).total_seconds()
            logger.error(f"Push failed for library {library_id}: {e}")
            return {
                "success": False,
                "message": f"Push failed: {str(e)}",
                "uploaded_bytes": 0,
                "snapshot_time": elapsed,
                "chunks_pushed": 0,
                "library_version": 0,
            }

    async def sync_library(
        self,
        library_id: str,
        direction: Literal["auto", "pull", "push"] = "auto",
    ) -> dict[str, any]:
        """
        Bidirectional sync with conflict detection.

        Auto mode logic:
        - Local newer: push
        - Remote newer: pull
        - Same version: no-op
        - Conflict (diverged): error, require manual resolution

        Args:
            library_id: Library ID
            direction: Sync direction (auto, pull, push)

        Returns:
            Statistics dict from pull or push operation

        Raises:
            ValueError: If conflict detected or invalid direction
        """
        logger.info(f"Starting sync for library {library_id}, direction={direction}")

        try:
            if direction == "pull":
                return await self.pull_library(library_id)

            if direction == "push":
                return await self.push_library(library_id)

            if direction == "auto":
                # Get local and remote metadata
                local_meta = self.vector_store.get_library_metadata(library_id)
                remote_result = await self._get_latest_remote_snapshot(library_id)
                remote_meta = remote_result[1] if remote_result else None

                # Compare versions
                status = self._compare_versions(local_meta, remote_meta)

                if status == SyncStatus.NO_LOCAL:
                    logger.info("No local copy, pulling from remote")
                    return await self.pull_library(library_id)

                if status == SyncStatus.NO_REMOTE:
                    logger.info("No remote copy, pushing to remote")
                    return await self.push_library(library_id)

                if status == SyncStatus.SAME:
                    return {
                        "success": True,
                        "message": "Versions are identical, no sync needed",
                        "operation": "none",
                    }

                if status == SyncStatus.LOCAL_NEWER:
                    logger.info("Local is newer, pushing to remote")
                    return await self.push_library(library_id)

                if status == SyncStatus.REMOTE_NEWER:
                    logger.info("Remote is newer, pulling from remote")
                    return await self.pull_library(library_id)

                if status == SyncStatus.DIVERGED:
                    raise ValueError(
                        "Libraries have diverged. Manual conflict resolution required. "
                        "Use direction='pull' or 'push' with force=True."
                    )

            raise ValueError(f"Invalid direction: {direction}")

        except Exception as e:
            logger.error(f"Sync failed for library {library_id}: {e}")
            return {
                "success": False,
                "message": f"Sync failed: {str(e)}",
            }

    async def list_remote_libraries(self) -> list[dict]:
        """
        List available libraries in remote storage.

        Returns:
            List of dicts with keys:
            - library_id: str
            - library_version: int
            - snapshot_file: str
            - uploaded_at: str
            - total_chunks: int
            - total_items: int
        """
        try:
            # List all files
            files = await self.storage.list_files("")

            # Parse snapshot files
            libraries = {}
            for file in files:
                parsed = self._parse_snapshot_filename(file)
                if not parsed:
                    continue

                library_id, version = parsed

                # Keep only the latest version per library
                if library_id not in libraries or version > libraries[library_id][1]:
                    libraries[library_id] = (file, version)

            # Get metadata for each library
            result = []
            for library_id, (snapshot_file, version) in libraries.items():
                metadata = await self.storage.get_metadata(snapshot_file)
                if not metadata:
                    logger.warning(f"No metadata for {snapshot_file}")
                    continue

                result.append(
                    {
                        "library_id": library_id,
                        "library_version": version,
                        "snapshot_file": snapshot_file,
                        "uploaded_at": metadata.get("uploaded_at", "unknown"),
                        "total_chunks": metadata.get("total_chunks", 0),
                        "total_items": metadata.get("total_items", 0),
                    }
                )

            logger.info(f"Found {len(result)} remote libraries")
            return result

        except Exception as e:
            logger.error(f"Error listing remote libraries: {e}")
            return []

    async def get_sync_status(self, library_id: str) -> dict[str, any]:
        """
        Get sync status for a library.

        Args:
            library_id: Library ID

        Returns:
            Status dict with keys:
            - local_exists: bool
            - remote_exists: bool
            - local_version: int
            - remote_version: int
            - sync_status: str (from SyncStatus constants)
            - local_chunks: int
            - remote_chunks: int
            - local_last_indexed: str (ISO timestamp)
            - remote_uploaded_at: str (ISO timestamp)
        """
        try:
            # Get local metadata
            local_meta = self.vector_store.get_library_metadata(library_id)

            # Get remote metadata
            remote_result = await self._get_latest_remote_snapshot(library_id)
            remote_meta = remote_result[1] if remote_result else None

            # Compare versions
            status = self._compare_versions(local_meta, remote_meta)

            return {
                "local_exists": local_meta is not None,
                "remote_exists": remote_meta is not None,
                "local_version": (
                    local_meta.last_indexed_version if local_meta else 0
                ),
                "remote_version": (
                    remote_meta.get("library_version", 0) if remote_meta else 0
                ),
                "sync_status": status,
                "local_chunks": local_meta.total_chunks if local_meta else 0,
                "remote_chunks": (
                    remote_meta.get("total_chunks", 0) if remote_meta else 0
                ),
                "local_last_indexed": (
                    local_meta.last_indexed_at if local_meta else ""
                ),
                "remote_uploaded_at": (
                    remote_meta.get("uploaded_at", "") if remote_meta else ""
                ),
            }

        except Exception as e:
            logger.error(f"Error getting sync status for library {library_id}: {e}")
            return {
                "local_exists": False,
                "remote_exists": False,
                "local_version": 0,
                "remote_version": 0,
                "sync_status": "error",
                "local_chunks": 0,
                "remote_chunks": 0,
                "local_last_indexed": "",
                "remote_uploaded_at": "",
                "error": str(e),
            }
