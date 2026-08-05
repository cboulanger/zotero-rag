"""Unit tests for backend/evaluation/harness.py -- the pages-loading logic
shared by the pytest accuracy harness and the evaluation scripts. The
extraction and cache primitives are patched; what's under test is the
routing: healthy pages pass through, OCR-shaped pages come from the eval
OCR cache, and a cache miss returns None (caller skips the book)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.evaluation.harness import analysis_pages_for, available_public_books, public_pages_for

_HEALTHY_PAGES = ["Zeile\n" * 200] * 40
_OCR_PAGES = ["ocr text\n" * 100] * 40


class TestAnalysisPagesFor(unittest.TestCase):
    def test_returns_extracted_pages_when_text_layer_is_usable(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=(_HEALTHY_PAGES, False)), \
             patch("backend.evaluation.harness.load_cached_ocr") as mock_cache:
            pages = analysis_pages_for(b"%PDF-fake")
        self.assertEqual(pages, _HEALTHY_PAGES)
        mock_cache.assert_not_called()

    def test_returns_cached_ocr_pages_for_ocr_shaped_input(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=([""] * 300, False)), \
             patch("backend.evaluation.harness.load_cached_ocr", return_value={"detected_language": "deu", "pages": _OCR_PAGES}):
            pages = analysis_pages_for(b"%PDF-fake")
        self.assertEqual(pages, _OCR_PAGES)

    def test_returns_none_on_ocr_cache_miss(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=([""] * 300, False)), \
             patch("backend.evaluation.harness.load_cached_ocr", return_value=None):
            self.assertIsNone(analysis_pages_for(b"%PDF-fake"))

    def test_returns_none_when_cached_ocr_pages_are_still_degenerate(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=([""] * 300, False)), \
             patch("backend.evaluation.harness.load_cached_ocr", return_value={"detected_language": "deu", "pages": [""] * 300}):
            self.assertIsNone(analysis_pages_for(b"%PDF-fake"))


class TestAvailablePublicBooks(unittest.TestCase):
    def test_yields_books_with_a_cache_entry_and_expected_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = Path(tmp)
            public_cache_dir = eval_dir / "public-cache"
            public_cache_dir.mkdir()
            (eval_dir / "9999999.expected.json").write_text("{}", encoding="utf-8")
            (public_cache_dir / "9999999.pages.json").write_text(
                json.dumps({"pages": ["a"]}), encoding="utf-8",
            )
            book = {"filename": "9999999.pdf", "title": "Test Book"}
            with patch("backend.evaluation.harness.EVAL_DIR", eval_dir), \
                 patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir), \
                 patch("backend.evaluation.harness.load_manifest_books", return_value=[book]):
                results = available_public_books()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], "9999999")

    def test_skips_books_with_no_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = Path(tmp)
            public_cache_dir = eval_dir / "public-cache"
            public_cache_dir.mkdir()
            (eval_dir / "9999999.expected.json").write_text("{}", encoding="utf-8")
            book = {"filename": "9999999.pdf", "title": "Test Book"}
            with patch("backend.evaluation.harness.EVAL_DIR", eval_dir), \
                 patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir), \
                 patch("backend.evaluation.harness.load_manifest_books", return_value=[book]):
                results = available_public_books()
            self.assertEqual(results, [])


class TestPublicPagesFor(unittest.TestCase):
    def test_returns_pages_for_existing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            public_cache_dir = Path(tmp) / "public-cache"
            public_cache_dir.mkdir()
            (public_cache_dir / "9999999.pages.json").write_text(
                json.dumps({"pages": ["redacted page text"]}), encoding="utf-8",
            )
            with patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir):
                pages = public_pages_for("9999999")
            self.assertEqual(pages, ["redacted page text"])

    def test_returns_none_for_missing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            public_cache_dir = Path(tmp) / "public-cache"
            public_cache_dir.mkdir()
            with patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir):
                self.assertIsNone(public_pages_for("9999999"))


if __name__ == "__main__":
    unittest.main()
