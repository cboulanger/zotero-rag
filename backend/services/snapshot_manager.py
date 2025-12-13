"""
Qdrant snapshot management for vector database synchronization.

Handles creation and restoration of Qdrant collection snapshots with
compression, checksums, and metadata.
"""

import hashlib
import json
import logging
import tarfile
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime

from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata

logger = logging.getLogger(__name__)


class SnapshotManager:
    """Manages Qdrant collection snapshots for sync."""

    # Qdrant collection names
    CHUNKS_COLLECTION = "document_chunks"
    DEDUP_COLLECTION = "deduplication"
    METADATA_COLLECTION = "library_metadata"

    def __init__(
        self,
        vector_store: VectorStore,
        temp_dir: Optional[Path] = None,
    ):
        """
        Initialize snapshot manager.

        Args:
            vector_store: VectorStore instance
            temp_dir: Temporary directory for snapshots (default: /tmp/zotero-rag-snapshots)
        """
        self.vector_store = vector_store
        self.temp_dir = temp_dir or Path("/tmp/zotero-rag-snapshots")
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Initialized SnapshotManager with temp dir: {self.temp_dir}")

    async def create_snapshot(
        self, library_id: str, compression: str = "gz"
    ) -> Path:
        """
        Create snapshot for a specific library's collections.

        Process:
        1. Get library metadata to determine version
        2. Create Qdrant snapshots for all 3 collections
        3. Package into tar.gz with metadata.json
        4. Compute SHA256 checksum
        5. Return path to snapshot file

        Args:
            library_id: Library ID to snapshot
            compression: Compression type (gz, bz2, xz)

        Returns:
            Path to snapshot file (e.g., library_6297749_v12345.tar.gz)

        Raises:
            ValueError: If library not indexed
            RuntimeError: If snapshot creation fails
        """
        try:
            # Get library metadata
            lib_metadata = self.vector_store.get_library_metadata(library_id)
            if not lib_metadata:
                raise ValueError(f"Library {library_id} not indexed")

            version = lib_metadata.last_indexed_version
            logger.info(f"Creating snapshot for library {library_id} v{version}")

            # Create working directory
            work_dir = self.temp_dir / f"snapshot_{library_id}_{version}_work"
            work_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Create snapshots for all collections
                snapshot_files = await self._create_collection_snapshots(
                    library_id, work_dir
                )

                # Create metadata file
                metadata = self._create_snapshot_metadata(
                    lib_metadata, compression, snapshot_files
                )
                metadata_path = work_dir / "metadata.json"
                with open(metadata_path, "w") as f:
                    json.dump(metadata, f, indent=2)

                # Create checksums file
                checksums_path = work_dir / "checksums.txt"
                await self._create_checksums_file(snapshot_files, checksums_path)

                # Create tar archive
                tar_name = f"library_{library_id}_v{version}.tar.{compression}"
                tar_path = self.temp_dir / tar_name

                await self._create_tar_archive(
                    work_dir, tar_path, compression, snapshot_files
                )

                # Compute final checksum
                final_checksum = self._compute_file_checksum(tar_path)
                logger.info(
                    f"Created snapshot {tar_path.name} "
                    f"(checksum: {final_checksum[:16]}...)"
                )

                return tar_path

            finally:
                # Cleanup working directory
                if work_dir.exists():
                    shutil.rmtree(work_dir)
                    logger.debug(f"Cleaned up working directory: {work_dir}")

        except Exception as e:
            logger.error(f"Error creating snapshot: {e}")
            raise RuntimeError(f"Snapshot creation failed: {e}")

    async def _create_collection_snapshots(
        self, library_id: str, work_dir: Path
    ) -> dict[str, Path]:
        """
        Create Qdrant snapshots for all library collections.

        Returns dict mapping collection name to snapshot path.
        """
        snapshot_files = {}

        # Note: Qdrant's snapshot feature creates full collection snapshots
        # For library-specific snapshots, we would need to:
        # 1. Create full collection snapshot
        # 2. Filter data by library_id
        # 3. Create new collection from filtered data
        #
        # For MVP, we'll document this limitation and implement later

        # For now, create placeholder files that will be replaced with
        # actual snapshot implementation
        logger.warning(
            "Full Qdrant snapshot implementation pending - "
            "creating library data export instead"
        )

        # Export library chunks as JSON (temporary solution)
        chunks_data = await self._export_library_chunks(library_id)
        chunks_file = work_dir / "document_chunks.snapshot"
        with open(chunks_file, "w") as f:
            json.dump(chunks_data, f)
        snapshot_files[self.CHUNKS_COLLECTION] = chunks_file

        # Export deduplication records
        dedup_data = await self._export_library_dedup(library_id)
        dedup_file = work_dir / "deduplication.snapshot"
        with open(dedup_file, "w") as f:
            json.dump(dedup_data, f)
        snapshot_files[self.DEDUP_COLLECTION] = dedup_file

        # Export library metadata
        metadata_data = await self._export_library_metadata(library_id)
        metadata_file = work_dir / "library_metadata.snapshot"
        with open(metadata_file, "w") as f:
            json.dump(metadata_data, f)
        snapshot_files[self.METADATA_COLLECTION] = metadata_file

        logger.debug(f"Created {len(snapshot_files)} collection snapshots")
        return snapshot_files

    async def _export_library_chunks(self, library_id: str) -> list[dict]:
        """Export all chunks for a library as JSON."""
        # Scroll through all chunks for this library
        chunks_data = []

        results = self.vector_store.client.scroll(
            collection_name=self.CHUNKS_COLLECTION,
            scroll_filter={
                "must": [
                    {"key": "library_id", "match": {"value": library_id}}
                ]
            },
            limit=1000,  # Process in batches
            with_payload=True,
            with_vectors=True,
        )

        points, next_offset = results

        for point in points:
            chunks_data.append({
                "id": str(point.id),
                "vector": point.vector,
                "payload": point.payload,
            })

        # Continue scrolling if there are more results
        while next_offset:
            results = self.vector_store.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter={
                    "must": [
                        {"key": "library_id", "match": {"value": library_id}}
                    ]
                },
                limit=1000,
                offset=next_offset,
                with_payload=True,
                with_vectors=True,
            )
            points, next_offset = results

            for point in points:
                chunks_data.append({
                    "id": str(point.id),
                    "vector": point.vector,
                    "payload": point.payload,
                })

        logger.debug(f"Exported {len(chunks_data)} chunks for library {library_id}")
        return chunks_data

    async def _export_library_dedup(self, library_id: str) -> list[dict]:
        """Export all deduplication records for a library."""
        dedup_data = []

        results = self.vector_store.client.scroll(
            collection_name=self.DEDUP_COLLECTION,
            scroll_filter={
                "must": [
                    {"key": "library_id", "match": {"value": library_id}}
                ]
            },
            limit=1000,
            with_payload=True,
            with_vectors=False,  # Dedup collection doesn't need vectors
        )

        points, _ = results

        for point in points:
            dedup_data.append({
                "id": str(point.id),
                "payload": point.payload,
            })

        logger.debug(f"Exported {len(dedup_data)} dedup records for library {library_id}")
        return dedup_data

    async def _export_library_metadata(self, library_id: str) -> dict:
        """Export library metadata."""
        lib_metadata = self.vector_store.get_library_metadata(library_id)
        if lib_metadata:
            return lib_metadata.model_dump()
        return {}

    def _create_snapshot_metadata(
        self,
        lib_metadata: LibraryIndexMetadata,
        compression: str,
        snapshot_files: dict[str, Path],
    ) -> dict:
        """Create metadata for snapshot archive."""
        return {
            "library_id": lib_metadata.library_id,
            "library_version": lib_metadata.last_indexed_version,
            "snapshot_version": "v1",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "total_chunks": lib_metadata.total_chunks,
            "total_items": lib_metadata.total_items_indexed,
            "qdrant_version": "1.15.1",  # TODO: Get from Qdrant client
            "schema_version": lib_metadata.schema_version,
            "compression": compression,
            "collections": list(snapshot_files.keys()),
        }

    async def _create_checksums_file(
        self, snapshot_files: dict[str, Path], checksums_path: Path
    ) -> None:
        """Create checksums file with SHA256 hashes."""
        with open(checksums_path, "w") as f:
            for collection, file_path in snapshot_files.items():
                checksum = self._compute_file_checksum(file_path)
                f.write(f"{checksum}  {file_path.name}\n")

    def _compute_file_checksum(self, file_path: Path) -> str:
        """Compute SHA256 checksum of file."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    async def _create_tar_archive(
        self,
        work_dir: Path,
        tar_path: Path,
        compression: str,
        snapshot_files: dict[str, Path],
    ) -> None:
        """Create compressed tar archive of snapshot files."""
        mode_map = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz", "": "w"}
        mode = mode_map.get(compression, "w:gz")

        with tarfile.open(tar_path, mode) as tar:
            # Add snapshot files
            for collection, file_path in snapshot_files.items():
                tar.add(file_path, arcname=file_path.name)

            # Add metadata and checksums
            tar.add(work_dir / "metadata.json", arcname="metadata.json")
            tar.add(work_dir / "checksums.txt", arcname="checksums.txt")

        logger.debug(f"Created tar archive: {tar_path}")

    async def restore_snapshot(
        self, snapshot_path: Path, library_id: str, verify_checksum: bool = True
    ) -> bool:
        """
        Restore snapshot for a library.

        Process:
        1. Verify checksum (if enabled)
        2. Extract tar.gz
        3. Validate metadata matches library_id
        4. Delete existing library collections (if any)
        5. Restore Qdrant snapshots
        6. Update library metadata

        Args:
            snapshot_path: Path to snapshot archive
            library_id: Expected library ID (validation)
            verify_checksum: Whether to verify checksums

        Returns:
            True if successful

        Raises:
            ValueError: If validation fails
            RuntimeError: If restoration fails
        """
        try:
            logger.info(f"Restoring snapshot from {snapshot_path.name}")

            # Create working directory
            work_dir = self.temp_dir / f"restore_{library_id}_work"
            work_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Extract archive
                with tarfile.open(snapshot_path, "r:*") as tar:
                    tar.extractall(work_dir)

                # Load and validate metadata
                metadata_path = work_dir / "metadata.json"
                with open(metadata_path) as f:
                    metadata = json.load(f)

                if metadata["library_id"] != library_id:
                    raise ValueError(
                        f"Library ID mismatch: expected {library_id}, "
                        f"got {metadata['library_id']}"
                    )

                # Verify checksums if enabled
                if verify_checksum:
                    await self._verify_checksums(work_dir)

                # Delete existing library data
                logger.info(f"Deleting existing data for library {library_id}")
                self.vector_store.delete_library_chunks(library_id)
                self.vector_store.delete_library_deduplication_records(library_id)

                # Restore collections
                await self._restore_collection_snapshots(library_id, work_dir)

                # Restore library metadata
                await self._restore_library_metadata(library_id, metadata)

                logger.info(
                    f"Successfully restored snapshot for library {library_id} "
                    f"(version {metadata['library_version']})"
                )
                return True

            finally:
                # Cleanup working directory
                if work_dir.exists():
                    shutil.rmtree(work_dir)

        except Exception as e:
            logger.error(f"Error restoring snapshot: {e}")
            raise RuntimeError(f"Snapshot restoration failed: {e}")

    async def _verify_checksums(self, work_dir: Path) -> None:
        """Verify file checksums match checksums.txt."""
        checksums_path = work_dir / "checksums.txt"
        with open(checksums_path) as f:
            for line in f:
                expected_checksum, filename = line.strip().split("  ")
                file_path = work_dir / filename
                actual_checksum = self._compute_file_checksum(file_path)

                if expected_checksum != actual_checksum:
                    raise ValueError(
                        f"Checksum mismatch for {filename}: "
                        f"expected {expected_checksum}, got {actual_checksum}"
                    )

        logger.debug("All checksums verified successfully")

    async def _restore_collection_snapshots(
        self, library_id: str, work_dir: Path
    ) -> None:
        """Restore Qdrant collections from snapshot files."""
        # Restore chunks
        chunks_file = work_dir / "document_chunks.snapshot"
        if chunks_file.exists():
            with open(chunks_file) as f:
                chunks_data = json.load(f)

            # Import chunks back into Qdrant
            from qdrant_client.models import PointStruct

            points = [
                PointStruct(
                    id=chunk["id"],
                    vector=chunk["vector"],
                    payload=chunk["payload"],
                )
                for chunk in chunks_data
            ]

            # Upsert in batches
            batch_size = 100
            for i in range(0, len(points), batch_size):
                batch = points[i : i + batch_size]
                self.vector_store.client.upsert(
                    collection_name=self.CHUNKS_COLLECTION,
                    points=batch,
                )

            logger.info(f"Restored {len(chunks_data)} chunks")

        # Restore deduplication records
        dedup_file = work_dir / "deduplication.snapshot"
        if dedup_file.exists():
            with open(dedup_file) as f:
                dedup_data = json.load(f)

            from qdrant_client.models import PointStruct

            points = [
                PointStruct(
                    id=record["id"],
                    vector=[0.0],  # Dummy vector
                    payload=record["payload"],
                )
                for record in dedup_data
            ]

            if points:
                self.vector_store.client.upsert(
                    collection_name=self.DEDUP_COLLECTION,
                    points=points,
                )

            logger.info(f"Restored {len(dedup_data)} dedup records")

    async def _restore_library_metadata(
        self, library_id: str, snapshot_metadata: dict
    ) -> None:
        """Restore library metadata from snapshot."""
        metadata_file = Path("/tmp") / "library_metadata.snapshot"
        if metadata_file.exists():
            with open(metadata_file) as f:
                lib_data = json.load(f)
                lib_metadata = LibraryIndexMetadata(**lib_data)
                self.vector_store.update_library_metadata(lib_metadata)
                logger.debug(f"Restored library metadata for {library_id}")

    async def get_snapshot_info(self, snapshot_path: Path) -> dict:
        """
        Extract metadata from snapshot without full restore.

        Args:
            snapshot_path: Path to snapshot archive

        Returns:
            Snapshot metadata dictionary

        Raises:
            ValueError: If snapshot is invalid
        """
        try:
            with tarfile.open(snapshot_path, "r:*") as tar:
                # Extract only metadata.json
                metadata_member = tar.getmember("metadata.json")
                metadata_file = tar.extractfile(metadata_member)

                if metadata_file:
                    metadata = json.load(metadata_file)
                    return metadata
                else:
                    raise ValueError("metadata.json not found in snapshot")

        except Exception as e:
            logger.error(f"Error reading snapshot info: {e}")
            raise ValueError(f"Invalid snapshot: {e}")

    async def cleanup_temp_dir(self) -> None:
        """Clean up temporary snapshot directory."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("Cleaned up temporary snapshot directory")
