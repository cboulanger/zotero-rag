"""Unit tests for backend.services.cron_indexer.CronIndexer."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.cron_indexer import (
    AlreadyRunningError,
    CronIndexer,
    SlugInfo,
    SlugSkipRequested,
    abort_process,
    clear_control_state,
    is_process_alive,
    read_control_state,
    read_live_status,
    write_control_state,
)


def _make_target(zotero_key: str = "test-api-key", embedding_key: str = "test-emb-key",
                  embedding_key_name: str = "TEST_API_KEY", fingerprint: str = "fp-test") -> dict:
    return {
        "zotero_key": zotero_key,
        "embedding_key": embedding_key,
        "embedding_key_name": embedding_key_name,
        "fingerprint": fingerprint,
    }


def _make_indexer(
    slugs: list[str],
    tmp_dir: Path,
    mode: str = "auto",
    max_items: int | None = None,
    key_store=None,
) -> CronIndexer:
    """Construct a CronIndexer with mocked services and temp files."""
    import logging
    log = logging.getLogger("test_cron_indexer")
    vector_store = MagicMock()
    # Return None by default so _resolve_mode skips the completeness check cleanly.
    vector_store.get_library_metadata.return_value = None
    return CronIndexer(
        targets={s: _make_target(fingerprint=f"fp-{s}") for s in slugs},
        vector_store=vector_store,
        lock_file=tmp_dir / "cron.lock",
        status_file=tmp_dir / "cron_status.json",
        log=log,
        mode=mode,
        max_items=max_items,
        key_store=key_store,
    )


def _patch_embedding_service():
    """Patch create_embedding_service to return a mock with no rate-limit info."""
    mock_embedding = MagicMock()
    mock_embedding.get_rate_limit_info = AsyncMock(return_value=None)
    patcher = patch("backend.services.cron_indexer.create_embedding_service", return_value=mock_embedding)
    return patcher


def test_index_slug_uses_per_slug_key():
    import tempfile, logging
    from unittest.mock import MagicMock
    from pathlib import Path
    from backend.services.cron_indexer import CronIndexer
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        vs = MagicMock(); vs.get_library_metadata.return_value = None
        indexer = CronIndexer(
            targets={
                "users/12345": _make_target(zotero_key="KEY_A", fingerprint="fp-a"),
                "groups/678": _make_target(zotero_key="KEY_B", fingerprint="fp-b"),
            },
            vector_store=vs,
            lock_file=tmp / "l", status_file=tmp / "s.json",
            log=logging.getLogger("t"),
        )
        assert sorted(indexer.slugs) == ["groups/678", "users/12345"]
        assert indexer.targets["groups/678"]["zotero_key"] == "KEY_B"


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

    def test_acquire_lock_fails_while_another_holder_has_it(self):
        """Two independent lock acquisitions on the same path must not both succeed —
        this is what protects two near-simultaneous manual triggers from both indexing
        at once."""
        from filelock import FileLock
        indexer = _make_indexer([], self.tmp)
        other_holder = FileLock(str(indexer._flock_path))
        other_holder.acquire(timeout=0)
        try:
            with self.assertRaises(AlreadyRunningError):
                indexer._acquire_lock()
        finally:
            other_holder.release()

    def test_acquire_lock_takes_over_stale_file(self):
        """A lock file left behind by a crashed process (no live holder) is taken
        over and reported as stale, without needing any PID/liveness check —
        the OS-level lock itself is the authoritative signal that no one holds it."""
        indexer = _make_indexer([], self.tmp)
        indexer.lock_file.write_text("99999999")  # leftover content, no live flock
        stale = indexer._acquire_lock()
        self.assertTrue(stale)
        self.assertTrue(indexer.lock_file.exists())
        indexer._release_lock()

    def test_acquire_lock_fresh_returns_false(self):
        indexer = _make_indexer([], self.tmp)
        stale = indexer._acquire_lock()
        self.assertFalse(stale)
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
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

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
        """AlreadyRunningError propagates when lock is held by another process."""
        from filelock import FileLock
        indexer = _make_indexer(["users/1"], self.tmp)
        other_holder = FileLock(str(indexer._flock_path))
        other_holder.acquire(timeout=0)
        try:
            with self.assertRaises(AlreadyRunningError):
                await indexer.run()
        finally:
            other_holder.release()

    async def test_run_marks_error_on_exception(self):
        """When index_library raises, the slug is marked as error."""
        indexer = _make_indexer(["users/1"], self.tmp)

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

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

    async def test_stale_lock_forces_full_reindex_for_interrupted_slug(self):
        """When a stale lock is taken over, slugs that were 'indexing' get mode='full'."""
        indexer = _make_indexer(["users/1", "groups/2"], self.tmp)

        # Simulate a previous run's status: users/1 was mid-index, groups/2 was done
        indexer._write_status({
            "running": True,
            "pid": 99999999,
            "slugs": {
                "users/1":  {"status": "indexing"},
                "groups/2": {"status": "done"},
            },
        })
        indexer.lock_file.write_text("99999999")

        captured_modes: list[str] = []

        async def fake_index_library(**kwargs):
            captured_modes.append(kwargs.get("mode", ""))
            return {"items_processed": 5, "chunks_added": 10}

        with patch("backend.services.cron_indexer.is_process_alive", return_value=False), \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

            mock_api_instance = AsyncMock()
            mock_api_instance.get_library_item_count = AsyncMock(return_value=0)
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(side_effect=fake_index_library)
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        # users/1 was interrupted → must be "full"; groups/2 was not → stays "auto"
        self.assertEqual(captured_modes[0], "full",  "interrupted slug must use full mode")
        self.assertEqual(captured_modes[1], "auto",  "non-interrupted slug keeps auto mode")

    async def test_progress_callback_updates_status(self):
        """The progress callback writes items_processed into the status file."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.progress_update_interval = 1  # write on every item

        async def fake_index_library(**kwargs):
            cb = kwargs.get("progress_callback")
            if cb:
                cb(5, 20, 50)   # simulate progress at item 5 of 20, 50 chunks so far
            return {"items_processed": 20, "chunks_added": 100}

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

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


class TestReadLiveStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "system").mkdir()

    def _write_status(self, data: dict) -> None:
        (self.tmp / "system" / "cron_status.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_missing_file_returns_empty(self):
        self.assertEqual(read_live_status(self.tmp), {})

    def test_returns_status_verbatim_when_not_running(self):
        self._write_status({"running": False, "slugs": {"users/1": {"status": "done"}}})
        result = read_live_status(self.tmp)
        self.assertFalse(result["running"])
        self.assertIn("users/1", result["slugs"])
        self.assertNotIn("crashed", result)

    def test_running_with_dead_pid_marked_crashed(self):
        self._write_status({"running": True, "pid": 99999999})
        result = read_live_status(self.tmp)
        self.assertFalse(result["running"])
        self.assertTrue(result["crashed"])

    def test_running_with_live_pid_stays_running(self):
        self._write_status({"running": True, "pid": os.getpid()})
        result = read_live_status(self.tmp)
        self.assertTrue(result["running"])
        self.assertNotIn("crashed", result)


class TestPerSlugEmbeddingErrorIsolation(unittest.IsolatedAsyncioTestCase):
    """A single user's invalid/rate-limited embedding key must not affect other users."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    async def test_auth_error_isolated_to_one_slug(self):
        """An EmbeddingAuthenticationError for one slug only errors that slug."""
        from backend.services.embeddings import EmbeddingAuthenticationError
        key_store = MagicMock()
        indexer = _make_indexer(["users/1", "users/2"], self.tmp, key_store=key_store)

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()

            async def fake_index_library(**kwargs):
                if kwargs.get("library_name") == "users/1":
                    raise EmbeddingAuthenticationError("embedding API rejected credentials (HTTP 401)")
                return {"items_processed": 5, "chunks_added": 10}

            mock_proc_instance.index_library = AsyncMock(side_effect=fake_index_library)
            MockProcessor.return_value = mock_proc_instance

            result = await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "error")
        self.assertIn("authentication", status["slugs"]["users/1"]["error"].lower())
        # The other user's slug must still succeed, not be aborted.
        self.assertEqual(status["slugs"]["users/2"]["status"], "done")
        self.assertEqual(result["libraries"], ["users/1", "users/2"])
        key_store.set_embedding_key_status.assert_called_once_with("fp-users/1", "invalid")

    async def test_rate_limit_exhausted_isolated_to_one_slug(self):
        """An EmbeddingRateLimitExhaustedError for one slug only skips that slug."""
        from datetime import timedelta
        from backend.services.embeddings import EmbeddingRateLimitExhaustedError
        available_at = datetime.now(timezone.utc) + timedelta(hours=2)
        key_store = MagicMock()
        indexer = _make_indexer(["users/1", "users/2"], self.tmp, key_store=key_store)

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()

            async def fake_index_library(**kwargs):
                if kwargs.get("library_name") == "users/1":
                    raise EmbeddingRateLimitExhaustedError("quota exhausted", available_at=available_at)
                return {"items_processed": 5, "chunks_added": 10}

            mock_proc_instance.index_library = AsyncMock(side_effect=fake_index_library)
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "skipped")
        self.assertEqual(status["slugs"]["users/1"]["skip_reason"], "embedding_rate_limit")
        # The other user's slug must still succeed, not be skipped.
        self.assertEqual(status["slugs"]["users/2"]["status"], "done")
        key_store.set_embedding_key_status.assert_called_once_with(
            "fp-users/1", "rate_limited", rate_limit_until=available_at.isoformat()
        )

    async def test_rate_limit_headers_persisted_per_slug(self):
        """Rate-limit headers from a slug's own embedding service are saved to status."""
        headers = {"x-ratelimit-remaining-requests": "42"}
        indexer = _make_indexer(["users/1"], self.tmp)

        async def dummy_index(slug_info, status):
            return {"items_processed": 1, "chunks_added": 2, "rate_limit_headers": headers}

        indexer._index_slug = dummy_index
        await indexer.run()

        status = json.loads((self.tmp / "cron_status.json").read_text())
        self.assertEqual(status.get("last_rate_limit_headers"), headers)
        # The synthetic key must not leak into the per-slug status entry.
        self.assertNotIn("rate_limit_headers", status["slugs"]["users/1"])


class TestResolveMode(unittest.IsolatedAsyncioTestCase):
    """Unit tests for CronIndexer._resolve_mode()."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _make_meta(self, last_indexed_version=1, total_items_indexed=10, last_full_scan_indexable=0):
        from backend.models.library import LibraryIndexMetadata
        return LibraryIndexMetadata(
            library_id="u1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=last_indexed_version,
            total_items_indexed=total_items_indexed,
            last_full_scan_indexable=last_full_scan_indexable,
        )

    async def test_interrupted_slug_returns_full(self):
        """A slug in _interrupted_slugs always returns 'full', ignoring everything else."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer._interrupted_slugs.add("users/1")
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "full")
        mock_api.get_library_item_count.assert_not_called()

    async def test_explicit_full_mode_returned_directly(self):
        """When mode='full', _resolve_mode returns 'full' without touching the API."""
        indexer = _make_indexer(["users/1"], self.tmp, mode="full")
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "full")
        mock_api.get_library_item_count.assert_not_called()

    async def test_explicit_incremental_mode_returned_directly(self):
        """When mode='incremental', _resolve_mode returns it without touching the API."""
        indexer = _make_indexer(["users/1"], self.tmp, mode="incremental")
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "incremental")
        mock_api.get_library_item_count.assert_not_called()

    async def test_never_indexed_returns_auto(self):
        """A library with last_indexed_version=0 returns 'auto' (not yet indexed)."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.vector_store.get_library_metadata.return_value = self._make_meta(
            last_indexed_version=0
        )
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "auto")
        mock_api.get_library_item_count.assert_not_called()

    async def test_no_metadata_returns_auto(self):
        """A library with no stored metadata returns 'auto'."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.vector_store.get_library_metadata.return_value = None
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "auto")

    async def test_zotero_total_zero_returns_auto(self):
        """When Zotero API returns 0 total items, returns 'auto' (can't compute ratio)."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.vector_store.get_library_metadata.return_value = self._make_meta(
            last_indexed_version=5, total_items_indexed=10
        )
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        mock_api.get_library_item_count = AsyncMock(return_value=0)
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "auto")

    async def test_above_threshold_returns_auto(self):
        """Indexed ratio >= 25% → returns 'auto' (no forced full re-index)."""
        indexer = _make_indexer(["users/1"], self.tmp)
        # 300/1000 = 30% >= 25%
        indexer.vector_store.get_library_metadata.return_value = self._make_meta(
            last_indexed_version=5, total_items_indexed=300
        )
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        mock_api.get_library_item_count = AsyncMock(return_value=1000)
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "auto")

    async def test_below_threshold_no_floor_returns_full(self):
        """Indexed ratio < 25% with no scan floor → forces 'full' re-index."""
        indexer = _make_indexer(["users/1"], self.tmp)
        # 100/1000 = 10% < 25%, no scan floor
        indexer.vector_store.get_library_metadata.return_value = self._make_meta(
            last_indexed_version=5, total_items_indexed=100, last_full_scan_indexable=0
        )
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        mock_api.get_library_item_count = AsyncMock(return_value=1000)
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "full")

    async def test_below_threshold_but_floor_explains_count_returns_auto(self):
        """If count is explained by scan_floor (library has few indexable items), returns 'auto'."""
        indexer = _make_indexer(["users/1"], self.tmp)
        # 100/1000 = 10% < 25%, but scan_floor=105 → indexed (100) >= floor*0.9 (94.5)
        indexer.vector_store.get_library_metadata.return_value = self._make_meta(
            last_indexed_version=5, total_items_indexed=100, last_full_scan_indexable=105
        )
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        mock_api.get_library_item_count = AsyncMock(return_value=1000)
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "auto")

    async def test_below_threshold_floor_but_count_dropped_returns_full(self):
        """If count is well below scan_floor (items were deleted/lost), forces 'full'."""
        indexer = _make_indexer(["users/1"], self.tmp)
        # 50/1000 = 5% < 25%, scan_floor=105, but indexed (50) < floor*0.9 (94.5) → not explained
        indexer.vector_store.get_library_metadata.return_value = self._make_meta(
            last_indexed_version=5, total_items_indexed=50, last_full_scan_indexable=105
        )
        slug_info = indexer.parse_slug("users/1")
        mock_api = AsyncMock()
        mock_api.get_library_item_count = AsyncMock(return_value=1000)
        result = await indexer._resolve_mode(slug_info, mock_api)
        self.assertEqual(result, "full")


class TestAbortProcess(unittest.TestCase):
    def test_returns_false_when_process_already_gone(self):
        with patch("backend.services.cron_indexer.is_process_alive", return_value=False):
            self.assertFalse(abort_process(999999))

    def test_sends_sigterm_when_alive(self):
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True), \
             patch("backend.services.cron_indexer.os.kill") as mock_kill:
            result = abort_process(1234)
        self.assertTrue(result)
        import signal
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)

    def test_returns_false_when_process_exits_between_check_and_signal(self):
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True), \
             patch("backend.services.cron_indexer.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(abort_process(1234))


class TestControlState(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_file_reads_empty_dict(self):
        self.assertEqual(read_control_state(self.tmp), {})

    def test_round_trip(self):
        write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})
        self.assertEqual(read_control_state(self.tmp), {"skip_slug": "users/1", "requested_at": "now"})

    def test_clear_removes_matching_request(self):
        write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})
        clear_control_state(self.tmp, matched_slug="users/1")
        self.assertIsNone(read_control_state(self.tmp).get("skip_slug"))

    def test_clear_is_noop_when_slug_no_longer_matches(self):
        """A newer request for a different slug must not be clobbered by a
        stale clear for the slug that was skipped earlier."""
        write_control_state(self.tmp, {"skip_slug": "groups/2", "requested_at": "later"})
        clear_control_state(self.tmp, matched_slug="users/1")
        self.assertEqual(read_control_state(self.tmp).get("skip_slug"), "groups/2")


class TestSkipSlug(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    async def test_queued_slug_skipped_before_indexing_starts(self):
        """A skip request for a not-yet-started slug marks it skipped without
        ever calling index_library for it."""
        indexer = _make_indexer(["users/1", "groups/2"], self.tmp)
        write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value={"items_processed": 5, "chunks_added": 10})
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "skipped")
        self.assertEqual(status["slugs"]["users/1"]["skip_reason"], "Skipped by admin request")
        self.assertEqual(status["slugs"]["groups/2"]["status"], "done")
        mock_proc_instance.index_library.assert_awaited_once()  # only groups/2 was ever indexed

    async def test_in_progress_slug_skipped_via_progress_callback(self):
        """A skip request matching the currently-indexing slug raises
        SlugSkipRequested from inside progress_callback; _index_slug catches
        it and marks the slug skipped instead of propagating."""
        indexer = _make_indexer(["users/1"], self.tmp)
        indexer.progress_update_interval = 1  # check the control file on every callback

        async def fake_index_library(**kwargs):
            cb = kwargs.get("progress_callback")
            write_control_state(self.tmp, {"skip_slug": "users/1", "requested_at": "now"})
            cb(5, 20, 50)  # triggers the control-file check at this interval
            return {"items_processed": 20, "chunks_added": 100}  # unreachable if the skip raises correctly

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(side_effect=fake_index_library)
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "skipped")
        self.assertEqual(status["slugs"]["users/1"]["skip_reason"], "Skipped by admin request")
        self.assertIsNone(read_control_state(self.tmp).get("skip_slug"))  # cleared after being consumed

    async def test_skip_request_for_unrelated_slug_has_no_effect(self):
        """A skip request naming a slug that isn't in this run never matches
        either checkpoint."""
        indexer = _make_indexer(["users/1"], self.tmp)
        write_control_state(self.tmp, {"skip_slug": "groups/999", "requested_at": "now"})

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value={"items_processed": 5, "chunks_added": 10})
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["status"], "done")

    async def test_control_state_cleared_at_end_of_run_even_if_unconsumed(self):
        """A skip request naming a slug never touched by either checkpoint
        during this run (e.g. it targets a slug not in this run at all)
        must not leak into a future, unrelated run."""
        indexer = _make_indexer(["users/1"], self.tmp)
        write_control_state(self.tmp, {"skip_slug": "groups/999", "requested_at": "now"})  # not part of this run

        with patch("backend.services.cron_indexer.get_settings") as mock_get_settings, \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():
            mock_get_settings.return_value.data_path = self.tmp

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value={"items_processed": 5, "chunks_added": 10})
            MockProcessor.return_value = mock_proc_instance

            await indexer.run()

        self.assertIsNone(read_control_state(self.tmp).get("skip_slug"))


if __name__ == "__main__":
    unittest.main()
