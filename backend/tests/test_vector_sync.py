"""
Tests for vector synchronization service.
"""

import unittest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from pathlib import Path
import tempfile
import shutil

from backend.services.vector_sync import VectorSyncService, SyncStatus
from backend.models.library import LibraryIndexMetadata


class TestVectorSyncService(unittest.IsolatedAsyncioTestCase):
    """Test VectorSyncService class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock dependencies
        self.vector_store = Mock()
        self.snapshot_manager = Mock()
        self.snapshot_manager.temp_dir = Path(tempfile.mkdtemp())
        self.storage_backend = AsyncMock()
        self.zotero_client = Mock()

        # Create service
        self.service = VectorSyncService(
            vector_store=self.vector_store,
            snapshot_manager=self.snapshot_manager,
            storage_backend=self.storage_backend,
            zotero_client=self.zotero_client,
        )

    def tearDown(self):
        """Clean up test fixtures."""
        if self.snapshot_manager.temp_dir.exists():
            shutil.rmtree(self.snapshot_manager.temp_dir)

    def test_get_remote_snapshot_path(self):
        """Test remote snapshot path generation."""
        path = self.service._get_remote_snapshot_path("123", 456)
        self.assertEqual(path, "library_123_v456.tar.gz")

    def test_parse_snapshot_filename_valid(self):
        """Test parsing valid snapshot filename."""
        result = self.service._parse_snapshot_filename("library_123_v456.tar.gz")
        self.assertEqual(result, ("123", 456))

        result = self.service._parse_snapshot_filename("library_999_v12345.tar.bz2")
        self.assertEqual(result, ("999", 12345))

    def test_parse_snapshot_filename_invalid(self):
        """Test parsing invalid snapshot filename."""
        self.assertIsNone(self.service._parse_snapshot_filename("invalid.tar.gz"))
        self.assertIsNone(self.service._parse_snapshot_filename("library_abc_v123.tar.gz"))
        self.assertIsNone(self.service._parse_snapshot_filename("snapshot.tar.gz"))

    def test_compare_versions_same(self):
        """Test version comparison when versions are the same."""
        local = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        remote = {"library_version": 100}

        status = self.service._compare_versions(local, remote)
        self.assertEqual(status, SyncStatus.SAME)

    def test_compare_versions_local_newer(self):
        """Test version comparison when local is newer."""
        local = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test",
            last_indexed_version=200,
        )
        remote = {"library_version": 100}

        status = self.service._compare_versions(local, remote)
        self.assertEqual(status, SyncStatus.LOCAL_NEWER)

    def test_compare_versions_remote_newer(self):
        """Test version comparison when remote is newer."""
        local = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        remote = {"library_version": 200}

        status = self.service._compare_versions(local, remote)
        self.assertEqual(status, SyncStatus.REMOTE_NEWER)

    def test_compare_versions_no_local(self):
        """Test version comparison when no local copy."""
        remote = {"library_version": 100}

        status = self.service._compare_versions(None, remote)
        self.assertEqual(status, SyncStatus.NO_LOCAL)

    def test_compare_versions_no_remote(self):
        """Test version comparison when no remote copy."""
        local = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )

        status = self.service._compare_versions(local, None)
        self.assertEqual(status, SyncStatus.NO_REMOTE)

    def test_compare_versions_neither(self):
        """Test version comparison when neither exists."""
        status = self.service._compare_versions(None, None)
        self.assertEqual(status, SyncStatus.NO_REMOTE)

    async def test_get_latest_remote_snapshot_found(self):
        """Test finding latest remote snapshot."""
        # Mock storage to return multiple snapshots
        self.storage_backend.list_files.return_value = [
            "library_123_v100.tar.gz",
            "library_123_v200.tar.gz",
            "library_456_v150.tar.gz",
        ]

        self.storage_backend.get_metadata.return_value = {
            "library_version": 200,
            "total_chunks": 1000,
        }

        result = await self.service._get_latest_remote_snapshot("123")

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "library_123_v200.tar.gz")
        self.assertEqual(result[1]["library_version"], 200)

    async def test_get_latest_remote_snapshot_not_found(self):
        """Test when no remote snapshot found."""
        self.storage_backend.list_files.return_value = [
            "library_456_v100.tar.gz",
        ]

        result = await self.service._get_latest_remote_snapshot("123")
        self.assertIsNone(result)

    async def test_should_pull_no_local(self):
        """Test should_pull when no local copy exists."""
        self.vector_store.get_library_metadata.return_value = None
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        should_pull, reason = await self.service.should_pull("123")

        self.assertTrue(should_pull)
        self.assertIn("No local copy", reason)

    async def test_should_pull_no_remote(self):
        """Test should_pull when no remote copy exists."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = []

        should_pull, reason = await self.service.should_pull("123")

        self.assertFalse(should_pull)
        self.assertIn("No remote copy", reason)

    async def test_should_pull_remote_newer(self):
        """Test should_pull when remote is newer."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v200.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 200}

        should_pull, reason = await self.service.should_pull("123")

        self.assertTrue(should_pull)
        self.assertIn("200 > local version 100", reason)

    async def test_should_pull_local_newer(self):
        """Test should_pull when local is newer."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=200,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        should_pull, reason = await self.service.should_pull("123")

        self.assertFalse(should_pull)
        self.assertIn("newer", reason)

    async def test_should_pull_same_version(self):
        """Test should_pull when versions are identical."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        should_pull, reason = await self.service.should_pull("123")

        self.assertFalse(should_pull)
        self.assertIn("identical", reason)

    async def test_pull_library_success(self):
        """Test successful pull operation."""
        # Setup mocks
        local_after_restore = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
            total_chunks=500,
        )

        # Mock get_library_metadata to return local metadata after restore
        self.vector_store.get_library_metadata.return_value = local_after_restore

        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        # Mock download
        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v100.tar.gz"
        snapshot_path.write_text("fake snapshot data")
        self.storage_backend.download_file.return_value = True

        # Mock snapshot info
        self.snapshot_manager.get_snapshot_info = AsyncMock(
            return_value={"library_id": "123", "library_version": 100}
        )

        # Mock restore
        self.snapshot_manager.restore_snapshot = AsyncMock(return_value=True)

        result = await self.service.pull_library("123", force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["chunks_restored"], 500)
        self.assertEqual(result["library_version"], 100)
        self.assertGreater(result["downloaded_bytes"], 0)

    async def test_pull_library_not_needed(self):
        """Test pull when not needed."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        result = await self.service.pull_library("123", force=False)

        self.assertFalse(result["success"])
        self.assertIn("not needed", result["message"])
        self.assertEqual(result["chunks_restored"], 0)

    async def test_pull_library_no_remote(self):
        """Test pull when no remote snapshot exists."""
        self.vector_store.get_library_metadata.return_value = None
        self.storage_backend.list_files.return_value = []

        result = await self.service.pull_library("123", force=True)

        self.assertFalse(result["success"])
        self.assertIn("No remote snapshot", result["message"])

    async def test_push_library_success(self):
        """Test successful push operation."""
        # Setup local metadata
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=200,
            total_chunks=1000,
            total_items_indexed=50,
        )
        self.vector_store.get_library_metadata.return_value = local

        # Mock no remote or older remote
        self.storage_backend.list_files.return_value = []

        # Mock snapshot creation
        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v200.tar.gz"
        snapshot_path.write_text("fake snapshot data")
        self.snapshot_manager.create_snapshot = AsyncMock(return_value=snapshot_path)

        # Mock upload
        self.storage_backend.upload_file.return_value = True
        self.storage_backend.exists.return_value = True

        result = await self.service.push_library("123")

        self.assertTrue(result["success"])
        self.assertEqual(result["chunks_pushed"], 1000)
        self.assertEqual(result["library_version"], 200)
        self.assertGreater(result["uploaded_bytes"], 0)

    async def test_push_library_not_indexed(self):
        """Test push when library not indexed locally."""
        self.vector_store.get_library_metadata.return_value = None

        result = await self.service.push_library("123")

        self.assertFalse(result["success"])
        self.assertIn("not indexed", result["message"])

    async def test_push_library_conflict(self):
        """Test push when remote is newer (conflict)."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local

        # Mock newer remote
        self.storage_backend.list_files.return_value = ["library_123_v200.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 200}

        result = await self.service.push_library("123", force=False)

        self.assertFalse(result["success"])
        self.assertIn("Remote version is newer", result["message"])

    async def test_push_library_force_override(self):
        """Test push with force overriding conflict."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
            total_chunks=500,
        )
        self.vector_store.get_library_metadata.return_value = local

        # Mock newer remote
        self.storage_backend.list_files.return_value = ["library_123_v200.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 200}

        # Mock snapshot and upload
        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v100.tar.gz"
        snapshot_path.write_text("fake data")
        self.snapshot_manager.create_snapshot = AsyncMock(return_value=snapshot_path)
        self.storage_backend.upload_file.return_value = True
        self.storage_backend.exists.return_value = True

        result = await self.service.push_library("123", force=True)

        self.assertTrue(result["success"])

    async def test_sync_library_pull_mode(self):
        """Test sync with explicit pull direction."""
        # Setup for pull
        self.vector_store.get_library_metadata.side_effect = [
            None,
            LibraryIndexMetadata(
                library_id="123",
                library_type="user",
                library_name="Test",
                last_indexed_version=100,
                total_chunks=500,
            ),
        ]
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v100.tar.gz"
        snapshot_path.write_text("data")
        self.storage_backend.download_file.return_value = True
        self.snapshot_manager.get_snapshot_info = AsyncMock(
            return_value={"library_id": "123"}
        )
        self.snapshot_manager.restore_snapshot = AsyncMock(return_value=True)

        result = await self.service.sync_library("123", direction="pull")

        self.assertTrue(result["success"])
        self.assertEqual(result["chunks_restored"], 500)

    async def test_sync_library_push_mode(self):
        """Test sync with explicit push direction."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=200,
            total_chunks=1000,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = []

        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v200.tar.gz"
        snapshot_path.write_text("data")
        self.snapshot_manager.create_snapshot = AsyncMock(return_value=snapshot_path)
        self.storage_backend.upload_file.return_value = True
        self.storage_backend.exists.return_value = True

        result = await self.service.sync_library("123", direction="push")

        self.assertTrue(result["success"])
        self.assertEqual(result["chunks_pushed"], 1000)

    async def test_sync_library_auto_no_local(self):
        """Test auto sync when no local copy."""
        self.vector_store.get_library_metadata.side_effect = [
            None,  # For auto check
            None,  # For pull check
            LibraryIndexMetadata(
                library_id="123",
                library_type="user",
                library_name="Test",
                last_indexed_version=100,
                total_chunks=500,
            ),  # After restore
        ]
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v100.tar.gz"
        snapshot_path.write_text("data")
        self.storage_backend.download_file.return_value = True
        self.snapshot_manager.get_snapshot_info = AsyncMock(
            return_value={"library_id": "123"}
        )
        self.snapshot_manager.restore_snapshot = AsyncMock(return_value=True)

        result = await self.service.sync_library("123", direction="auto")

        self.assertTrue(result["success"])

    async def test_sync_library_auto_no_remote(self):
        """Test auto sync when no remote copy."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=200,
            total_chunks=1000,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = []

        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v200.tar.gz"
        snapshot_path.write_text("data")
        self.snapshot_manager.create_snapshot = AsyncMock(return_value=snapshot_path)
        self.storage_backend.upload_file.return_value = True
        self.storage_backend.exists.return_value = True

        result = await self.service.sync_library("123", direction="auto")

        self.assertTrue(result["success"])

    async def test_sync_library_auto_same_version(self):
        """Test auto sync when versions are identical."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        result = await self.service.sync_library("123", direction="auto")

        self.assertTrue(result["success"])
        self.assertIn("identical", result["message"])
        self.assertEqual(result["operation"], "none")

    async def test_sync_library_auto_local_newer(self):
        """Test auto sync when local is newer (should push)."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=200,
            total_chunks=1000,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v200.tar.gz"
        snapshot_path.write_text("data")
        self.snapshot_manager.create_snapshot = AsyncMock(return_value=snapshot_path)
        self.storage_backend.upload_file.return_value = True
        self.storage_backend.exists.return_value = True

        result = await self.service.sync_library("123", direction="auto")

        self.assertTrue(result["success"])
        self.assertEqual(result["chunks_pushed"], 1000)

    async def test_sync_library_auto_remote_newer(self):
        """Test auto sync when remote is newer (should pull)."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
            total_chunks=500,
        )
        self.vector_store.get_library_metadata.side_effect = [
            local,  # For auto check
            local,  # For pull check
            LibraryIndexMetadata(
                library_id="123",
                library_type="user",
                library_name="Test",
                last_indexed_version=200,
                total_chunks=1000,
            ),  # After restore
        ]
        self.storage_backend.list_files.return_value = ["library_123_v200.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 200}

        snapshot_path = self.snapshot_manager.temp_dir / "library_123_v200.tar.gz"
        snapshot_path.write_text("data")
        self.storage_backend.download_file.return_value = True
        self.snapshot_manager.get_snapshot_info = AsyncMock(
            return_value={"library_id": "123"}
        )
        self.snapshot_manager.restore_snapshot = AsyncMock(return_value=True)

        result = await self.service.sync_library("123", direction="auto")

        self.assertTrue(result["success"])
        self.assertEqual(result["chunks_restored"], 1000)

    async def test_sync_library_invalid_direction(self):
        """Test sync with invalid direction."""
        result = await self.service.sync_library("123", direction="invalid")

        self.assertFalse(result["success"])
        self.assertIn("Invalid direction", result["message"])

    async def test_list_remote_libraries(self):
        """Test listing remote libraries."""
        self.storage_backend.list_files.return_value = [
            "library_123_v100.tar.gz",
            "library_123_v200.tar.gz",  # Latest for 123
            "library_456_v150.tar.gz",
            "other_file.txt",  # Should be ignored
        ]

        # Mock metadata for latest versions
        async def get_metadata(path):
            if path == "library_123_v200.tar.gz":
                return {
                    "library_version": 200,
                    "uploaded_at": "2025-01-01T00:00:00Z",
                    "total_chunks": 1000,
                    "total_items": 50,
                }
            elif path == "library_456_v150.tar.gz":
                return {
                    "library_version": 150,
                    "uploaded_at": "2025-01-02T00:00:00Z",
                    "total_chunks": 500,
                    "total_items": 25,
                }
            return None

        self.storage_backend.get_metadata.side_effect = get_metadata

        result = await self.service.list_remote_libraries()

        self.assertEqual(len(result), 2)

        # Find library 123
        lib_123 = next(lib for lib in result if lib["library_id"] == "123")
        self.assertEqual(lib_123["library_version"], 200)
        self.assertEqual(lib_123["snapshot_file"], "library_123_v200.tar.gz")
        self.assertEqual(lib_123["total_chunks"], 1000)

        # Find library 456
        lib_456 = next(lib for lib in result if lib["library_id"] == "456")
        self.assertEqual(lib_456["library_version"], 150)
        self.assertEqual(lib_456["total_chunks"], 500)

    async def test_list_remote_libraries_empty(self):
        """Test listing when no remote libraries exist."""
        self.storage_backend.list_files.return_value = []

        result = await self.service.list_remote_libraries()

        self.assertEqual(len(result), 0)

    async def test_get_sync_status_both_exist(self):
        """Test getting sync status when both local and remote exist."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
            total_chunks=500,
            last_indexed_at="2025-01-01T00:00:00Z",
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = ["library_123_v200.tar.gz"]
        self.storage_backend.get_metadata.return_value = {
            "library_version": 200,
            "total_chunks": 1000,
            "uploaded_at": "2025-01-02T00:00:00Z",
        }

        status = await self.service.get_sync_status("123")

        self.assertTrue(status["local_exists"])
        self.assertTrue(status["remote_exists"])
        self.assertEqual(status["local_version"], 100)
        self.assertEqual(status["remote_version"], 200)
        self.assertEqual(status["sync_status"], SyncStatus.REMOTE_NEWER)
        self.assertEqual(status["local_chunks"], 500)
        self.assertEqual(status["remote_chunks"], 1000)

    async def test_get_sync_status_only_local(self):
        """Test getting sync status when only local exists."""
        local = LibraryIndexMetadata(
            library_id="123",
            library_type="user",
            library_name="Test",
            last_indexed_version=100,
        )
        self.vector_store.get_library_metadata.return_value = local
        self.storage_backend.list_files.return_value = []

        status = await self.service.get_sync_status("123")

        self.assertTrue(status["local_exists"])
        self.assertFalse(status["remote_exists"])
        self.assertEqual(status["sync_status"], SyncStatus.NO_REMOTE)

    async def test_get_sync_status_only_remote(self):
        """Test getting sync status when only remote exists."""
        self.vector_store.get_library_metadata.return_value = None
        self.storage_backend.list_files.return_value = ["library_123_v100.tar.gz"]
        self.storage_backend.get_metadata.return_value = {"library_version": 100}

        status = await self.service.get_sync_status("123")

        self.assertFalse(status["local_exists"])
        self.assertTrue(status["remote_exists"])
        self.assertEqual(status["sync_status"], SyncStatus.NO_LOCAL)

    async def test_get_sync_status_neither_exists(self):
        """Test getting sync status when neither exists."""
        self.vector_store.get_library_metadata.return_value = None
        self.storage_backend.list_files.return_value = []

        status = await self.service.get_sync_status("123")

        self.assertFalse(status["local_exists"])
        self.assertFalse(status["remote_exists"])
        self.assertEqual(status["sync_status"], SyncStatus.NO_REMOTE)


if __name__ == "__main__":
    unittest.main()
