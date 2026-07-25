"""Unit tests for backend.services.chapter_retrofit."""

import unittest

from backend.services.chapter_retrofit import find_best_book_match


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


if __name__ == "__main__":
    unittest.main()
