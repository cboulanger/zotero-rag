"""Unit tests for backend.services.chapter_evidence.types."""

import unittest

from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, _first_page_number


class TestFirstPageNumber(unittest.TestCase):
    def test_parses_range(self):
        self.assertEqual(_first_page_number("85-113"), 85)

    def test_parses_single_page(self):
        self.assertEqual(_first_page_number("45"), 45)

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(_first_page_number(""))

    def test_returns_none_for_unparseable_text(self):
        self.assertIsNone(_first_page_number("n/a"))


class TestChapterCandidateDefaults(unittest.TestCase):
    def test_defaults(self):
        c = ChapterCandidate(title="Introduction")
        self.assertEqual(c.authors, ())
        self.assertIsNone(c.printed_page_number)
        self.assertIsNone(c.pdf_page_index)
        self.assertIsNone(c.chapter_doi)
        self.assertEqual(c.source, "heuristic")
        self.assertEqual(c.metadata_confidence, 1.0)

    def test_is_frozen_and_hashable(self):
        c = ChapterCandidate(title="Introduction")
        self.assertIsInstance(hash(c), int)
        with self.assertRaises(Exception):
            c.title = "Something Else"


class TestBookContext(unittest.TestCase):
    def test_construction(self):
        ctx = BookContext(
            item_key="B1", isbn="9783031466373", title="Some Book",
            editors=("Jane Editor",), publisher="Acme Press", year=2020,
        )
        self.assertEqual(ctx.isbn, "9783031466373")
        self.assertEqual(ctx.editors, ("Jane Editor",))
