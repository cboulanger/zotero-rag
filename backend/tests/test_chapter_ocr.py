"""Unit tests for backend.services.chapter_ocr."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.services.chapter_ocr import (
    detect_language,
    load_cached_ocr,
    save_ocr_cache,
)


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


if __name__ == "__main__":
    unittest.main()
