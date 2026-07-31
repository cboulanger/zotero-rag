"""Unit tests for backend.services.review_queue_store."""

import tempfile
import unittest
from pathlib import Path

from backend.services.review_queue_store import (
    get_entry,
    list_pending,
    remove_entry,
    set_bucket,
    set_status,
    upsert_many,
)


class ReviewQueueStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()


class TestUpsertMany(ReviewQueueStoreTestCase):
    def test_new_entries_become_pending(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "ocr:ATT1", "type": "ocr", "bucket": "review", "payload": {"book_key": "BOOK1"}},
        ])
        entry = get_entry(self.path, "groups/1", "ocr:ATT1")
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["payload"], {"book_key": "BOOK1"})
        self.assertIn("created_at", entry)
        self.assertIn("updated_at", entry)

    def test_upsert_existing_pending_refreshes_payload(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "chapter:BOOK1:1-2", "type": "chapter", "bucket": "review", "payload": {"confidence": 0.5}},
        ])
        upsert_many(self.path, "groups/1", [
            {"queue_id": "chapter:BOOK1:1-2", "type": "chapter", "bucket": "review", "payload": {"confidence": 0.6}},
        ])
        entry = get_entry(self.path, "groups/1", "chapter:BOOK1:1-2")
        self.assertEqual(entry["payload"]["confidence"], 0.6)

    def test_upsert_existing_approved_left_untouched(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit", "payload": {"book_key": "OLD"}},
        ])
        set_status(self.path, "groups/1", "match:CHAP1", "approved")
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP1", "type": "match", "bucket": "commit", "payload": {"book_key": "NEW"}},
        ])
        entry = get_entry(self.path, "groups/1", "match:CHAP1")
        self.assertEqual(entry["status"], "approved")
        self.assertEqual(entry["payload"]["book_key"], "OLD")

    def test_upsert_existing_rejected_left_untouched(self):
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP2", "type": "match", "bucket": "review", "payload": {}},
        ])
        set_status(self.path, "groups/1", "match:CHAP2", "rejected")
        upsert_many(self.path, "groups/1", [
            {"queue_id": "match:CHAP2", "type": "match", "bucket": "review", "payload": {"changed": True}},
        ])
        entry = get_entry(self.path, "groups/1", "match:CHAP2")
        self.assertEqual(entry["status"], "rejected")
        self.assertNotIn("changed", entry["payload"])

    def test_empty_list_is_a_noop(self):
        upsert_many(self.path, "groups/1", [])
        self.assertFalse(self.path.exists())

    def test_per_library_isolation(self):
        upsert_many(self.path, "groups/1", [{"queue_id": "ocr:A", "type": "ocr", "bucket": "review", "payload": {}}])
        upsert_many(self.path, "groups/2", [{"queue_id": "ocr:A", "type": "ocr", "bucket": "review", "payload": {}}])
        set_status(self.path, "groups/1", "ocr:A", "approved")
        self.assertEqual(get_entry(self.path, "groups/1", "ocr:A")["status"], "approved")
        self.assertEqual(get_entry(self.path, "groups/2", "ocr:A")["status"], "pending")


class TestListPending(ReviewQueueStoreTestCase):
    def setUp(self):
        super().setUp()
        upsert_many(self.path, "groups/1", [
            {"queue_id": "ocr:A", "type": "ocr", "bucket": "review", "payload": {}},
            {"queue_id": "chapter:B:1-2", "type": "chapter", "bucket": "commit", "payload": {}},
            {"queue_id": "match:C", "type": "match", "bucket": "review", "payload": {}},
        ])
        set_status(self.path, "groups/1", "match:C", "approved")

    def test_excludes_non_pending(self):
        results = list_pending(self.path, "groups/1")
        self.assertEqual({e["queue_id"] for e in results}, {"ocr:A", "chapter:B:1-2"})

    def test_filters_by_bucket(self):
        results = list_pending(self.path, "groups/1", bucket="commit")
        self.assertEqual({e["queue_id"] for e in results}, {"chapter:B:1-2"})

    def test_filters_by_entry_type(self):
        results = list_pending(self.path, "groups/1", entry_type="ocr")
        self.assertEqual({e["queue_id"] for e in results}, {"ocr:A"})

    def test_unknown_library_returns_empty(self):
        self.assertEqual(list_pending(self.path, "groups/999"), [])


class TestSetStatusAndBucket(ReviewQueueStoreTestCase):
    def test_set_status_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            set_status(self.path, "groups/1", "does-not-exist", "approved")

    def test_set_bucket_flips_bucket_keeps_status(self):
        upsert_many(self.path, "groups/1", [{"queue_id": "match:D", "type": "match", "bucket": "commit", "payload": {}}])
        set_bucket(self.path, "groups/1", "match:D", "review")
        entry = get_entry(self.path, "groups/1", "match:D")
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["status"], "pending")


class TestGetEntryAndRemove(ReviewQueueStoreTestCase):
    def test_get_entry_returns_none_if_missing(self):
        self.assertIsNone(get_entry(self.path, "groups/1", "nope"))

    def test_remove_entry_deletes(self):
        upsert_many(self.path, "groups/1", [{"queue_id": "ocr:E", "type": "ocr", "bucket": "review", "payload": {}}])
        remove_entry(self.path, "groups/1", "ocr:E")
        self.assertIsNone(get_entry(self.path, "groups/1", "ocr:E"))

    def test_remove_entry_missing_is_a_noop(self):
        remove_entry(self.path, "groups/1", "does-not-exist")  # must not raise
