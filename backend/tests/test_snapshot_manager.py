"""
Unit tests for snapshot manager.

Tests snapshot creation, restoration, and metadata handling.
"""

import pytest
import json
import tarfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import tempfile
import shutil

from backend.services.snapshot_manager import SnapshotManager
from backend.models.library import LibraryIndexMetadata


class TestSnapshotManager:
    """Test snapshot manager functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create mock vector store
        self.mock_vector_store = Mock()
        self.mock_vector_store.CHUNKS_COLLECTION = "document_chunks"
        self.mock_vector_store.DEDUP_COLLECTION = "deduplication"
        self.mock_vector_store.METADATA_COLLECTION = "library_metadata"

        # Create temporary directory for tests
        self.temp_dir = Path(tempfile.mkdtemp())

        # Create snapshot manager
        self.manager = SnapshotManager(
            vector_store=self.mock_vector_store,
            temp_dir=self.temp_dir,
        )

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    @pytest.mark.asyncio
    async def test_create_snapshot_metadata(self):
        """Test snapshot metadata creation."""
        lib_metadata = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100,
            total_chunks=1000,
            total_items_indexed=50,
            schema_version=2,
        )

        snapshot_files = {
            "document_chunks": Path("test.snapshot"),
            "deduplication": Path("test2.snapshot"),
        }

        metadata = self.manager._create_snapshot_metadata(
            lib_metadata, "gz", snapshot_files
        )

        assert metadata["library_id"] == "123"
        assert metadata["library_version"] == 100
        assert metadata["total_chunks"] == 1000
        assert metadata["total_items"] == 50
        assert metadata["compression"] == "gz"
        assert metadata["schema_version"] == 2
        assert "created_at" in metadata
        assert "collections" in metadata

    @pytest.mark.asyncio
    async def test_compute_file_checksum(self):
        """Test file checksum computation."""
        # Create test file
        test_file = self.temp_dir / "test.txt"
        test_content = b"test content for checksum"
        with open(test_file, "wb") as f:
            f.write(test_content)

        # Compute checksum
        checksum = self.manager._compute_file_checksum(test_file)

        # Verify it's a valid SHA256 hex string
        assert len(checksum) == 64
        assert all(c in "0123456789abcdef" for c in checksum)

        # Verify same content produces same checksum
        checksum2 = self.manager._compute_file_checksum(test_file)
        assert checksum == checksum2

    @pytest.mark.asyncio
    async def test_create_tar_archive(self):
        """Test tar archive creation."""
        # Create test files
        work_dir = self.temp_dir / "work"
        work_dir.mkdir()

        snapshot_files = {}
        for i, name in enumerate(["file1.snapshot", "file2.snapshot"]):
            file_path = work_dir / name
            with open(file_path, "w") as f:
                f.write(f"content {i}")
            snapshot_files[f"collection_{i}"] = file_path

        # Create metadata and checksums
        metadata_path = work_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump({"test": "data"}, f)

        checksums_path = work_dir / "checksums.txt"
        with open(checksums_path, "w") as f:
            f.write("abc123  file1.snapshot\n")

        # Create tar archive
        tar_path = self.temp_dir / "test.tar.gz"
        await self.manager._create_tar_archive(
            work_dir, tar_path, "gz", snapshot_files
        )

        # Verify archive exists and contains expected files
        assert tar_path.exists()
        with tarfile.open(tar_path, "r:gz") as tar:
            members = tar.getnames()
            assert "metadata.json" in members
            assert "checksums.txt" in members
            assert "file1.snapshot" in members
            assert "file2.snapshot" in members

    @pytest.mark.asyncio
    async def test_get_snapshot_info(self):
        """Test extracting snapshot metadata without full restore."""
        # Create test snapshot
        work_dir = self.temp_dir / "work"
        work_dir.mkdir()

        metadata = {
            "library_id": "123",
            "library_version": 100,
            "created_at": "2025-12-11T12:00:00Z",
        }

        metadata_path = work_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)

        # Create minimal tar archive
        tar_path = self.temp_dir / "test.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(metadata_path, arcname="metadata.json")

        # Get snapshot info
        info = await self.manager.get_snapshot_info(tar_path)

        assert info["library_id"] == "123"
        assert info["library_version"] == 100
        assert info["created_at"] == "2025-12-11T12:00:00Z"

    @pytest.mark.asyncio
    async def test_get_snapshot_info_invalid_archive(self):
        """Test get_snapshot_info with invalid archive."""
        invalid_file = self.temp_dir / "invalid.tar.gz"
        with open(invalid_file, "w") as f:
            f.write("not a tar file")

        with pytest.raises(ValueError, match="Invalid snapshot"):
            await self.manager.get_snapshot_info(invalid_file)

    @pytest.mark.asyncio
    async def test_export_library_chunks(self):
        """Test exporting library chunks."""
        # Mock scroll results
        mock_points = [
            Mock(id="1", vector=[0.1, 0.2], payload={"library_id": "123", "text": "test1"}),
            Mock(id="2", vector=[0.3, 0.4], payload={"library_id": "123", "text": "test2"}),
        ]

        self.mock_vector_store.client.scroll.return_value = (mock_points, None)

        # Export chunks
        chunks_data = await self.manager._export_library_chunks("123")

        assert len(chunks_data) == 2
        assert chunks_data[0]["id"] == "1"
        assert chunks_data[0]["vector"] == [0.1, 0.2]
        assert chunks_data[0]["payload"]["text"] == "test1"

    @pytest.mark.asyncio
    async def test_create_snapshot_no_library(self):
        """Test create_snapshot fails when library not indexed."""
        self.mock_vector_store.get_library_metadata.return_value = None

        with pytest.raises(RuntimeError, match="Library .* not indexed"):
            await self.manager.create_snapshot("999")

    @pytest.mark.asyncio
    async def test_cleanup_temp_dir(self):
        """Test cleanup of temporary directory."""
        # Create some test files
        test_file = self.temp_dir / "test.txt"
        with open(test_file, "w") as f:
            f.write("test")

        assert test_file.exists()

        # Cleanup
        await self.manager.cleanup_temp_dir()

        # Verify directory is empty but still exists
        assert self.temp_dir.exists()
        assert not test_file.exists()


class TestSnapshotRestoration:
    """Test snapshot restoration functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_vector_store = Mock()
        self.temp_dir = Path(tempfile.mkdtemp())
        self.manager = SnapshotManager(
            vector_store=self.mock_vector_store,
            temp_dir=self.temp_dir,
        )

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def _create_test_snapshot(self, library_id: str, version: int) -> Path:
        """Helper to create a test snapshot archive."""
        work_dir = self.temp_dir / "create_work"
        work_dir.mkdir()

        # Create metadata
        metadata = {
            "library_id": library_id,
            "library_version": version,
            "created_at": "2025-12-11T12:00:00Z",
            "total_chunks": 10,
            "total_items": 5,
        }
        with open(work_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        # Create mock snapshot files
        chunks_data = [{"id": "1", "vector": [0.1], "payload": {"test": "data"}}]
        with open(work_dir / "document_chunks.snapshot", "w") as f:
            json.dump(chunks_data, f)

        with open(work_dir / "deduplication.snapshot", "w") as f:
            json.dump([], f)

        # Create checksums
        with open(work_dir / "checksums.txt", "w") as f:
            f.write("abc123  document_chunks.snapshot\n")
            f.write("def456  deduplication.snapshot\n")

        # Create tar archive
        tar_path = self.temp_dir / f"library_{library_id}_v{version}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for file in work_dir.iterdir():
                tar.add(file, arcname=file.name)

        shutil.rmtree(work_dir)
        return tar_path

    @pytest.mark.asyncio
    async def test_restore_snapshot_library_id_mismatch(self):
        """Test restore fails with library ID mismatch."""
        snapshot_path = self._create_test_snapshot("123", 100)

        self.mock_vector_store.delete_library_chunks.return_value = 0
        self.mock_vector_store.delete_library_deduplication_records.return_value = 0

        # Try to restore with different library_id
        with pytest.raises(RuntimeError, match="Library ID mismatch"):
            await self.manager.restore_snapshot(snapshot_path, "456")

    @pytest.mark.asyncio
    async def test_restore_snapshot_success(self):
        """Test successful snapshot restoration."""
        snapshot_path = self._create_test_snapshot("123", 100)

        self.mock_vector_store.delete_library_chunks.return_value = 0
        self.mock_vector_store.delete_library_deduplication_records.return_value = 0
        self.mock_vector_store.client.upsert = Mock()
        self.mock_vector_store.update_library_metadata = Mock()

        # Restore snapshot (skip checksum verification for simplicity)
        result = await self.manager.restore_snapshot(
            snapshot_path, "123", verify_checksum=False
        )

        assert result is True
        self.mock_vector_store.delete_library_chunks.assert_called_once_with("123")
        self.mock_vector_store.delete_library_deduplication_records.assert_called_once()
