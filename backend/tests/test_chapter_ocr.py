"""Unit tests for backend.services.chapter_ocr."""

import hashlib
import json
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from backend.services.chapter_ocr import (
    detect_language,
    load_cached_ocr,
    save_ocr_cache,
)
from backend.services.chapter_ocr import run as ocr_run


class TestDetectLanguage(unittest.TestCase):
    def test_uses_item_language_field_if_set(self):
        self.assertEqual(detect_language(item_language="de", title="Some Title"), "deu")

    def test_detects_from_title_when_no_item_language(self):
        result = detect_language(item_language=None, title="Einführung in die Zitierweise")
        self.assertEqual(result, "deu")

    def test_falls_back_to_combined_default_when_undetectable(self):
        result = detect_language(item_language=None, title="")
        self.assertEqual(result, "eng+deu+fra+spa")


class TestOcrCache(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_ocr_cache(cache_dir, "abc123", detected_language="deu", pages=["page one", "page two"])
            result = load_cached_ocr(cache_dir, "abc123")
            self.assertEqual(result["detected_language"], "deu")
            self.assertEqual(result["pages"], ["page one", "page two"])

    def test_returns_none_when_not_cached(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(load_cached_ocr(Path(tmp), "does-not-exist"))


class TestOcrRun(unittest.TestCase):
    def test_ocrs_each_attachment_and_caches_result(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 one page fake"
        zotero_client.get_item.return_value = {"data": {"title": "Einführung in die Zitierweise", "language": ""}}

        extractor = AsyncMock()
        extractor.extract_and_chunk.return_value = [MagicMock(text="OCR'd page text")]

        with TemporaryDirectory() as tmp, unittest.mock.patch(
            "backend.services.chapter_ocr.PdfReader"
        ) as mock_reader_cls, unittest.mock.patch(
            "backend.services.chapter_ocr.slice_single_page_pdf", return_value=b"single page bytes"
        ):
            mock_reader_cls.return_value.pages = [MagicMock()]  # one page
            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                extractor=extractor,
                library_id="1",
                library_type="group",
                attachment_specs=[{"item_key": "BOOK1", "attachment_key": "ATT1"}],
                max_items=None,
                cache_dir=Path(tmp),
                progress_callback=lambda p, m: None,
            ))
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["ocr_succeeded"])
        self.assertEqual(result["results"][0]["detected_language"], "deu")
        # Regression guard for the Task-9-style bug: get_attachment_file must be
        # called with the attachment's OWN key (ATT1), not the book item's key
        # (BOOK1) — /items/{key}/file requires the attachment's key.
        zotero_client.get_attachment_file.assert_called_once_with("1", "ATT1", library_type="group")


if __name__ == "__main__":
    unittest.main()
