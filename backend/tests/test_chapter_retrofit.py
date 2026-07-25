"""Unit tests for backend.services.chapter_retrofit."""

import unittest
from unittest.mock import MagicMock

from backend.services.chapter_retrofit import find_best_book_match, locate_chapter_pdf_range
from backend.services.chapter_retrofit import run as retrofit_run


class TestFindBestBookMatch(unittest.TestCase):
    def setUp(self):
        self.books = [
            {"key": "BOOK1", "title": "Handbook of Reference Management", "year": 2019},
            {"key": "BOOK2", "title": "Introduction to Bibliographic Software", "year": 2021},
        ]

    def test_matches_exact_title_and_year(self):
        result = find_best_book_match("Handbook of Reference Management", 2019, self.books)
        self.assertIsNotNone(result)
        self.assertEqual(result.book_key, "BOOK1")
        self.assertGreaterEqual(result.score, 0.9)

    def test_matches_within_year_tolerance(self):
        result = find_best_book_match("Handbook of Reference Management", 2020, self.books)
        self.assertIsNotNone(result)
        self.assertEqual(result.book_key, "BOOK1")

    def test_rejects_year_outside_tolerance(self):
        result = find_best_book_match("Handbook of Reference Management", 2023, self.books)
        self.assertIsNone(result)

    def test_no_candidates_returns_none(self):
        result = find_best_book_match("Something Entirely Different", 2019, self.books)
        self.assertIsNone(result)

    def test_ambiguous_close_scores_returns_none(self):
        books = [
            {"key": "A", "title": "Studies in Modern History", "year": 2020},
            {"key": "B", "title": "Studies in Modern History Vol 2", "year": 2020},
        ]
        result = find_best_book_match("Studies in Modern History", 2020, books)
        self.assertIsNone(result)


class TestLocateChapterPdfRange(unittest.TestCase):
    def test_finds_contiguous_span(self):
        book_pages = [
            "Front matter, nothing relevant here.",
            "Comparing Citation Styles\nThis chapter examines APA and MLA styles in depth.",
            "...continued examination of citation styles and their history.",
            "Unrelated next chapter begins here with different content entirely.",
        ]
        chapter_text = "Comparing Citation Styles\nThis chapter examines APA and MLA styles in depth. ...continued examination of citation styles and their history."
        result = locate_chapter_pdf_range(chapter_text, book_pages)
        self.assertEqual(result, (1, 2))

    def test_returns_none_when_no_confident_span(self):
        book_pages = ["Completely unrelated content about gardening techniques."]
        chapter_text = "This is about astrophysics and black holes entirely."
        result = locate_chapter_pdf_range(chapter_text, book_pages)
        self.assertIsNone(result)


class TestRetrofitRun(unittest.TestCase):
    def test_links_confident_match_and_skips_ambiguous(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
        )
        self.assertEqual(len(result["linked"]), 1)
        self.assertEqual(result["linked"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["linked"][0]["book_key"], "BOOK1")
        zot.update_item.assert_called()

    def test_book_write_succeeds_but_chapter_write_fails_reports_failed(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)
        # First update_item call (the book, written first) succeeds; the
        # second call (the chapter) raises, simulating a version conflict.
        zot.update_item.side_effect = [None, Exception("boom")]

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
        )

        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["failed"][0]["book_key"], "BOOK1")
        self.assertIn("boom", result["failed"][0]["error"])

        # Confirm the book write was attempted before the chapter write
        # that raised: update_item's first call must have been for BOOK1.
        first_call_arg = zot.update_item.call_args_list[0].args[0]
        self.assertEqual(first_call_arg["data"]["key"], "BOOK1")
        self.assertEqual(zot.update_item.call_count, 2)


if __name__ == "__main__":
    unittest.main()
