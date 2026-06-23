"""Unit tests for backend.services.cron_indexer.CronIndexer."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.cron_indexer import (
    AlreadyRunningError,
    CronIndexer,
    SlugInfo,
    is_process_alive,
)


def _make_indexer(
    slugs: list[str],
    tmp_dir: Path,
    mode: str = "auto",
    max_items: int | None = None,
) -> CronIndexer:
    """Construct a CronIndexer with mocked services and temp files."""
    import logging
    log = logging.getLogger("test_cron_indexer")
    return CronIndexer(
        slugs=slugs,
        api_key="test-api-key",
        vector_store=MagicMock(),
        embedding_service=MagicMock(),
        lock_file=tmp_dir / "cron.lock",
        status_file=tmp_dir / "cron_status.json",
        log=log,
        mode=mode,
        max_items=max_items,
    )


class TestParseSlug(unittest.TestCase):
    def test_parse_slug_user(self):
        indexer = _make_indexer([], Path(tempfile.mkdtemp()))
        info = indexer.parse_slug("users/12345")
        self.assertIsInstance(info, SlugInfo)
        self.assertEqual(info.slug, "users/12345")
        self.assertEqual(info.library_type, "user")
        self.assertEqual(info.library_id, "u12345")
        self.assertEqual(info.numeric_id, "12345")

    def test_parse_slug_group(self):
        indexer = _make_indexer([], Path(tempfile.mkdtemp()))
        info = indexer.parse_slug("groups/678")
        self.assertEqual(info.library_type, "group")
        self.assertEqual(info.library_id, "678")
        self.assertEqual(info.numeric_id, "678")

    def test_parse_slug_invalid(self):
        indexer = _make_indexer([], Path(tempfile.mkdtemp()))
        with self.assertRaises(ValueError):
            indexer.parse_slug("badslug")

    def test_parse_slug_invalid_type(self):
        indexer = _make_indexer([], Path(tempfile.mkdtemp()))
        with self.assertRaises(ValueError):
            indexer.parse_slug("libraries/999")


class TestLockFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_acquire_lock_creates_file(self):
        indexer = _make_indexer([], self.tmp)
        indexer._acquire_lock()
        self.assertTrue(indexer.lock_file.exists())
        pid_in_file = int(indexer.lock_file.read_text())
        self.assertEqual(pid_in_file, os.getpid())
        indexer._release_lock()

    def test_acquire_lock_fails_if_alive(self):
        indexer = _make_indexer([], self.tmp)
        # Write our own PID as a "running" process
        indexer.lock_file.write_text(str(os.getpid()))
        with self.assertRaises(AlreadyRunningError):
            indexer._acquire_lock()

    def test_acquire_lock_takes_over_dead_process(self):
        indexer = _make_indexer([], self.tmp)
        # Write a PID that definitely does not exist
        indexer.lock_file.write_text("99999999")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=False):
            indexer._acquire_lock()  # should not raise
        self.assertTrue(indexer.lock_file.exists())
        indexer._release_lock()

    def test_release_lock_deletes_file(self):
        indexer = _make_indexer([], self.tmp)
        indexer.lock_file.write_text(str(os.getpid()))
        indexer._release_lock()
        self.assertFalse(indexer.lock_file.exists())


class TestStatusFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_write_status_atomic(self):
        indexer = _make_indexer([], self.tmp)
        status = {"running": True, "pid": 1234, "slugs": {}}
        indexer._write_status(status)
        self.assertTrue(indexer.status_file.exists())
        loaded = json.loads(indexer.status_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded["pid"], 1234)

    def test_read_status_returns_empty_if_missing(self):
        indexer = _make_indexer([], self.tmp)
        result = indexer._read_status()
        self.assertEqual(result, {})

    def test_read_status_returns_dict(self):
        indexer = _make_indexer([], self.tmp)
        indexer.status_file.write_text(json.dumps({"running": False}), encoding="utf-8")
        result = indexer._read_status()
        self.assertEqual(result["running"], False)


class TestCronIndexerRun(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    async def test_run_success(self):
        """Happy-path: two slugs indexed successfully."""
        indexer = _make_indexer(["users/1", "groups/2"], self.tmp)

        fake_stats = {"items_processed": 10, "chunks_added": 50, "mode": "full"}

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor:

            # Set up the web API context manager mock
            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            # Set up DocumentProcessor.index_library
            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value=fake_stats)
            MockProcessor.return_value = mock_proc_instance

            result = await indexer.run()

        self.assertEqual(result["items_processed"], 20)
        self.assertEqual(result["chunks_added"], 100)
        self.assertEqual(len(result["libraries"]), 2)
        self.assertFalse(indexer.lock_file.exists())

        status = indexer._read_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["slugs"]["users/1"]["status"], "done")
        self.assertEqual(status["slugs"]["groups/2"]["status"], "done")

    async def test_run_already_running(self):
        """AlreadyRunningError propagates when lock is held by a live process."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.lock_file.write_text(str(os.getpid()))

        with self.assertRaises(AlreadyRunningError):
            await indexer.run()

    async def test_run_marks_error_on_exception(self):
        """When index_library raises, the slug is marked as error."""
        indexer = _make_indexer(["users/1"], self.tmp)

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor:

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(side_effect=RuntimeError("API down"))
            MockProcessor.return_value = mock_proc_instance

            # The error is caught per-slug, so run() completes without re-raising
            result = await indexer.run()

        status = indexer._read_status()
        self.assertFalse(status["running"])
        self.assertEqual(status["slugs"]["users/1"]["status"], "error")

    async def test_progress_callback_updates_status(self):
        """The progress callback writes items_processed into the status file."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.progress_update_interval = 1  # write on every item

        progress_calls: list[tuple] = []

        async def fake_index_library(**kwargs):
            cb = kwargs.get("progress_callback")
            if cb:
                cb(5, 20)   # simulate progress at item 5 of 20
            return {"items_processed": 20, "chunks_added": 100}

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor:

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(side_effect=fake_index_library)
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        # After run, the slug is done with final counts
        self.assertEqual(status["slugs"]["users/1"]["status"], "done")


class TestIsProcessAlive(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(is_process_alive(os.getpid()))

    def test_nonexistent_pid_is_not_alive(self):
        # PID 99999999 very unlikely to exist
        self.assertFalse(is_process_alive(99999999))


if __name__ == "__main__":
    unittest.main()
