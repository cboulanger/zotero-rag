import json
import tempfile
import unittest
from pathlib import Path

from backend.services import pending_upload_cache as cache


class TestPendingUploadCacheStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trips_bytes_and_metadata(self):
        cache.write_entry(
            self.data_path, "u123", "ATT1", b"hello world",
            {"item_key": "ITEM1", "mime_type": "application/pdf", "item_version": 5,
             "attachment_version": 2, "title": "A Title", "authors": ["Doe"],
             "year": 2020, "item_type": "journalArticle", "library_type": "user",
             "library_name": "My Library"},
        )
        result = cache.read_entry(self.data_path, "u123", "ATT1")
        self.assertIsNotNone(result)
        file_bytes, meta = result
        self.assertEqual(file_bytes, b"hello world")
        self.assertEqual(meta["item_key"], "ITEM1")
        self.assertEqual(meta["title"], "A Title")
        self.assertEqual(meta["attempts"], 0)
        self.assertIsNone(meta["last_error"])
        self.assertIn("enqueued_at", meta)

    def test_read_missing_entry_returns_none(self):
        self.assertIsNone(cache.read_entry(self.data_path, "u123", "NOPE"))

    def test_has_entry(self):
        self.assertFalse(cache.has_entry(self.data_path, "u123", "ATT1"))
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        self.assertTrue(cache.has_entry(self.data_path, "u123", "ATT1"))

    def test_rewrite_overwrites_in_place_and_resets_attempts(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"v1", {"item_key": "I1"})
        cache.record_failure(self.data_path, "u123", "ATT1", "boom")
        cache.write_entry(self.data_path, "u123", "ATT1", b"v2", {"item_key": "I1"})
        file_bytes, meta = cache.read_entry(self.data_path, "u123", "ATT1")
        self.assertEqual(file_bytes, b"v2")
        self.assertEqual(meta["attempts"], 0)
        self.assertIsNone(meta["last_error"])

    def test_delete_entry_removes_both_files(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        cache.delete_entry(self.data_path, "u123", "ATT1")
        self.assertIsNone(cache.read_entry(self.data_path, "u123", "ATT1"))

    def test_delete_entry_is_safe_when_nothing_exists(self):
        cache.delete_entry(self.data_path, "u123", "NEVER_WROTE")  # must not raise

    def test_list_entries_returns_metadata_sorted_by_enqueued_at(self):
        cache.write_entry(self.data_path, "u123", "ATT_B", b"x", {"item_key": "I_B"})
        cache.write_entry(self.data_path, "u123", "ATT_A", b"x", {"item_key": "I_A"})
        entries = cache.list_entries(self.data_path, "u123")
        self.assertEqual([e["attachment_key"] for e in entries], ["ATT_B", "ATT_A"])
        self.assertEqual(entries[0]["item_key"], "I_B")

    def test_list_entries_empty_for_unknown_library(self):
        self.assertEqual(cache.list_entries(self.data_path, "u_nope"), [])

    def test_list_entries_scoped_to_one_library(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        cache.write_entry(self.data_path, "u999", "ATT2", b"x", {"item_key": "I2"})
        self.assertEqual(len(cache.list_entries(self.data_path, "u123")), 1)
        self.assertEqual(len(cache.list_entries(self.data_path, "u999")), 1)

    def test_record_failure_increments_attempts_and_sets_last_error(self):
        cache.write_entry(self.data_path, "u123", "ATT1", b"x", {"item_key": "I1"})
        cache.record_failure(self.data_path, "u123", "ATT1", "first error")
        cache.record_failure(self.data_path, "u123", "ATT1", "second error")
        _, meta = cache.read_entry(self.data_path, "u123", "ATT1")
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["last_error"], "second error")

    def test_record_failure_on_missing_entry_is_a_no_op(self):
        cache.record_failure(self.data_path, "u123", "NOPE", "boom")  # must not raise


from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from backend.config.settings import Settings


class TestQueueStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        (self.data_path / "system").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, **overrides):
        return Settings(data_path=self.data_path, autoindex_secret="x" * 32, **overrides)

    def test_eta_from_interval_scheduler_with_no_prior_run(self):
        settings = self._settings(autoindex_interval_minutes=30)
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(reason)
        eta_dt = datetime.fromisoformat(eta)
        delta = eta_dt - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=25) < delta < timedelta(minutes=35))

    def test_eta_from_interval_scheduler_after_a_prior_run(self):
        settings = self._settings(autoindex_interval_minutes=60)
        last_run = datetime.now(timezone.utc) - timedelta(minutes=10)
        (self.data_path / "system" / "cron_status.json").write_text(
            json.dumps({"finished_at": last_run.isoformat()}), encoding="utf-8"
        )
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(reason)
        eta_dt = datetime.fromisoformat(eta)
        delta = eta_dt - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=45) < delta < timedelta(minutes=55))

    def test_eta_falls_back_to_top_of_next_hour_without_an_interval(self):
        settings = self._settings(autoindex_interval_minutes=None)
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(reason)
        eta_dt = datetime.fromisoformat(eta)
        self.assertEqual(eta_dt.minute, 0)
        self.assertEqual(eta_dt.second, 0)
        now = datetime.now(timezone.utc)
        self.assertTrue(timedelta(0) < (eta_dt - now) <= timedelta(hours=1))

    def test_paused_scheduler_reports_paused_reason_and_no_eta(self):
        from backend.services.autoindex_scheduler import write_scheduler_state
        write_scheduler_state(self.data_path, {"paused": True})
        settings = self._settings(autoindex_interval_minutes=30)
        eta, reason = cache.compute_queue_eta(settings)
        self.assertIsNone(eta)
        self.assertEqual(reason, "paused")

    def test_library_has_valid_autoindex_target_true_when_slug_listed(self):
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = [{"targets": ["users/123", "groups/456"]}]
        self.assertTrue(cache.library_has_valid_autoindex_target("u123", key_store))
        self.assertTrue(cache.library_has_valid_autoindex_target("456", key_store))

    def test_library_has_valid_autoindex_target_false_when_slug_absent(self):
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = [{"targets": ["groups/456"]}]
        self.assertFalse(cache.library_has_valid_autoindex_target("u999", key_store))

    def test_library_has_valid_autoindex_target_false_when_store_disabled(self):
        key_store = MagicMock()
        key_store.enabled = False
        self.assertFalse(cache.library_has_valid_autoindex_target("u123", key_store))

    def test_get_queue_status_prioritizes_paused_over_key_invalid(self):
        from backend.services.autoindex_scheduler import write_scheduler_state
        write_scheduler_state(self.data_path, {"paused": True})
        settings = self._settings(autoindex_interval_minutes=30)
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = []
        status = cache.get_queue_status(settings, "u123", key_store)
        self.assertEqual(status, {"eta": None, "reason": "paused"})

    def test_get_queue_status_reports_key_invalid_when_not_paused(self):
        settings = self._settings(autoindex_interval_minutes=30)
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = []
        status = cache.get_queue_status(settings, "u123", key_store)
        self.assertEqual(status, {"eta": None, "reason": "key_invalid"})

    def test_get_queue_status_reports_eta_when_valid_and_not_paused(self):
        settings = self._settings(autoindex_interval_minutes=30)
        key_store = MagicMock()
        key_store.enabled = True
        key_store.list_metadata.return_value = [{"targets": ["users/123"]}]
        status = cache.get_queue_status(settings, "u123", key_store)
        self.assertIsNone(status["reason"])
        self.assertIsNotNone(status["eta"])


if __name__ == "__main__":
    unittest.main()
