"""Tests for the rag-failed machinery: page-aware PDF inspection/splitting,
FailedAttachmentStore, and deferred-upload quarantine."""

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pikepdf
from pikepdf import Name

from backend.services import pending_upload_cache as cache
from backend.services.failed_attachments import FailedAttachmentStore, REASON_TOO_COSTLY
from backend.services.index_event_log import IndexEventLog
from backend.services.indexed_tag_sync import plan_tag_ops
from backend.utils.pdf_splitter import inspect_pdf, split_pdf_bytes


def _pdf(pages: int, text: bool) -> bytes:
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    for _ in range(pages):
        page = pdf.add_blank_page(page_size=(200, 200))
        if text:
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
            page.Contents = pdf.make_stream(b"BT /F1 12 Tf 10 100 Td (hello) Tj ET")
        else:
            page.Contents = pdf.make_stream(b"q 200 0 0 200 0 0 cm /Im0 Do Q")
    buf = BytesIO()
    pdf.save(buf)
    return buf.getvalue()


class TestPdfProfile(unittest.TestCase):
    def test_detects_text_layer(self):
        profile = inspect_pdf(_pdf(3, text=True))
        self.assertEqual((profile.page_count, profile.has_text_layer), (3, True))

    def test_image_only_scan_has_no_text_layer(self):
        profile = inspect_pdf(_pdf(60, text=False))
        self.assertEqual((profile.page_count, profile.has_text_layer), (60, False))

    def test_unparseable_raises_value_error(self):
        with self.assertRaises(ValueError):
            inspect_pdf(b"not a pdf")

    def test_max_pages_per_part_caps_parts_even_when_bytes_allow_more(self):
        parts = split_pdf_bytes(_pdf(120, text=False), 30 * 1024 ** 2, max_pages_per_part=50)
        self.assertEqual([offset for _, offset in parts], [0, 50, 100])


class TestFailedAttachmentStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.log = IndexEventLog(root / "events.jsonl")
        self.store = FailedAttachmentStore(root / "failed.json", self.log)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mark_clear_round_trip_emits_events(self):
        self.store.mark_failed("u1", "ATT", "ITEM", REASON_TOO_COSTLY, "too big")
        self.store.mark_failed("u1", "ATT", "ITEM", REASON_TOO_COSTLY, "too big")  # idempotent
        self.assertTrue(self.store.is_failed("u1", "ATT"))
        self.assertEqual(self.store.failed_keys("u1", ["ATT", "OTHER"]), {"ATT"})
        self.assertIsNotNone(self.store.clear("u1", "ATT"))
        self.assertIsNone(self.store.clear("u1", "ATT"))
        self.assertFalse(self.store.is_failed("u1", "ATT"))
        types = [e["type"] for e in self.log.read_since(0)["events"]]
        self.assertEqual(types, ["failed", "unfailed"])

    def test_failed_ops_planned_with_kind(self):
        att = [{"data": {"key": "A", "tags": []}}, {"data": {"key": "B", "tags": [{"tag": "T"}]}}]
        ops = plan_tag_ops(att, {"A"}, "T", kind="failed")
        self.assertEqual([(o["op"], o["attachment_key"], o["kind"]) for o in ops], [("add", "A", "failed"), ("remove", "B", "failed")])


class TestQuarantine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = SimpleNamespace(data_path=self.root, pending_upload_max_attempts=2)
        self.settings.failed_attachments_path = self.root / "system" / "failed_attachments.json"
        self.settings.index_events_path = self.root / "system" / "events.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_failing_entry_sorts_behind_untried_ones(self):
        cache.write_entry(self.root, "u1", "OLD", b"x", {"item_key": "I1"})
        cache.write_entry(self.root, "u1", "NEW", b"x", {"item_key": "I2"})
        cache.record_failure(self.root, "u1", "OLD", "boom")
        self.assertEqual([e["attachment_key"] for e in cache.list_entries(self.root, "u1")], ["NEW", "OLD"])

    def test_quarantine_after_max_attempts_and_release(self):
        from unittest.mock import patch

        cache.write_entry(self.root, "u1", "ATT", b"x", {"item_key": "I1"})
        with patch("backend.config.settings.get_settings", return_value=self.settings):
            self.assertFalse(cache.note_failure(self.settings, "u1", "ATT", "I1", "e1"))
            self.assertTrue(cache.note_failure(self.settings, "u1", "ATT", "I1", "e2"))
            from backend.services.failed_attachments import get_failed_store

            self.assertTrue(get_failed_store().is_failed("u1", "ATT"))
        self.assertEqual(cache.list_entries(self.root, "u1"), [])
        self.assertEqual(len(cache.list_entries(self.root, "u1", include_quarantined=True)), 1)
        cache.release_entry(self.root, "u1", "ATT")
        entries = cache.list_entries(self.root, "u1")
        self.assertEqual((len(entries), entries[0]["attempts"]), (1, 0))

    def test_systemic_failure_does_not_count(self):
        cache.write_entry(self.root, "u1", "ATT", b"x", {"item_key": "I1"})
        for _ in range(5):
            self.assertFalse(cache.note_failure(self.settings, "u1", "ATT", "I1", "quota", count_attempt=False))


if __name__ == "__main__":
    unittest.main()
