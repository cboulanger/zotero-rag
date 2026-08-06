"""Unit tests for backend.services.chapter_ocr (the Zotero-specific batch
OCR orchestrator -- the OCR engine itself now lives in the standalone
chapter_segmentation package; its own tests live there)."""

import asyncio
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from chapter_segmentation.ocr import save_ocr_cache

from backend.services.chapter_ocr import run as ocr_run


class TestOcrRun(unittest.TestCase):
    def test_ocrs_each_attachment_and_caches_result(self):
        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 one page fake"
        zotero_client.get_item.return_value = {"data": {"title": "Einführung in die Zitierweise", "language": ""}}

        ocr_backend = AsyncMock()
        ocr_backend.ocr_pdf_pages.return_value = ["OCR'd page text"]

        with TemporaryDirectory() as tmp:
            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                ocr_backend=ocr_backend,
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
        # Regression guard: get_attachment_file must be called with the
        # attachment's OWN key (ATT1), not the book item's key (BOOK1).
        zotero_client.get_attachment_file.assert_called_once_with("1", "ATT1", library_type="group")
        ocr_backend.ocr_pdf_pages.assert_awaited_once_with(b"%PDF-1.4 one page fake", language="deu")

    def test_cache_hit_short_circuits_before_fetching_item(self):
        fixture_bytes = b"%PDF-1.4 one page fake"
        content_hash = hashlib.sha256(fixture_bytes).hexdigest()

        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.return_value = fixture_bytes

        ocr_backend = AsyncMock()

        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_ocr_cache(cache_dir, content_hash, detected_language="fra", pages=["cached page one"])

            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                ocr_backend=ocr_backend,
                library_id="1",
                library_type="group",
                attachment_specs=[{"item_key": "BOOK1", "attachment_key": "ATT1"}],
                max_items=None,
                cache_dir=cache_dir,
                progress_callback=lambda p, m: None,
            ))

        self.assertEqual(len(result["results"]), 1)
        entry = result["results"][0]
        self.assertTrue(entry["ocr_succeeded"])
        self.assertEqual(entry["detected_language"], "fra")
        self.assertEqual(entry["char_count"], len("cached page one"))
        # Cache hit must short-circuit before ever fetching the item or OCR-ing.
        zotero_client.get_item.assert_not_called()
        ocr_backend.ocr_pdf_pages.assert_not_called()

    def test_one_attachment_failure_does_not_abort_the_batch(self):
        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.side_effect = [
            b"%PDF-1.4 fake book one",
            b"%PDF-1.4 fake book two",
        ]
        zotero_client.get_item.return_value = {"data": {"title": "", "language": "en"}}

        ocr_backend = AsyncMock()
        ocr_backend.ocr_pdf_pages.side_effect = [
            RuntimeError("Kreuzberg sidecar timeout"),
            ["OCR'd page text"],
        ]

        with TemporaryDirectory() as tmp:
            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                ocr_backend=ocr_backend,
                library_id="1",
                library_type="group",
                attachment_specs=[
                    {"item_key": "BOOK1", "attachment_key": "ATT1"},
                    {"item_key": "BOOK2", "attachment_key": "ATT2"},
                ],
                max_items=None,
                cache_dir=Path(tmp),
                progress_callback=lambda p, m: None,
            ))

        self.assertEqual(len(result["results"]), 2)
        first, second = result["results"]
        self.assertFalse(first["ocr_succeeded"])
        self.assertIn("error", first)
        self.assertTrue(second["ocr_succeeded"])


if __name__ == "__main__":
    unittest.main()
