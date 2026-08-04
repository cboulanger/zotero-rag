"""Unit tests for backend/evaluation/harness.py -- the pages-loading logic
shared by the pytest accuracy harness and the evaluation scripts. The
extraction and cache primitives are patched; what's under test is the
routing: healthy pages pass through, OCR-shaped pages come from the eval
OCR cache, and a cache miss returns None (caller skips the book)."""

import unittest
from unittest.mock import patch

from backend.evaluation.harness import analysis_pages_for

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


if __name__ == "__main__":
    unittest.main()
