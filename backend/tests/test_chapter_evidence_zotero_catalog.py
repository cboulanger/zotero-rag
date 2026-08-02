"""Unit tests for backend.services.chapter_evidence.zotero_catalog_strategy."""

import unittest

from backend.services.chapter_evidence.types import BookContext
from backend.services.chapter_evidence.zotero_catalog_strategy import (
    ZoteroCatalogMetadataStrategy,
    find_zotero_catalog_candidates,
    score_zotero_catalog_candidate,
)


def _book_section(
    title, book_title, isbn="", date="", publisher="", pages="",
    authors=None, editors=None, doi=None,
):
    creators = [
        {"creatorType": "author", "firstName": f.split()[0], "lastName": f.split()[-1]}
        for f in (authors or [])
    ] + [
        {"creatorType": "editor", "firstName": f.split()[0], "lastName": f.split()[-1]}
        for f in (editors or [])
    ]
    data = {
        "itemType": "bookSection", "key": f"CH-{title[:5]}", "title": title,
        "bookTitle": book_title, "ISBN": isbn, "date": date, "publisher": publisher,
        "pages": pages, "creators": creators, "extra": "",
    }
    if doi is not None:
        data["DOI"] = doi
    return {"data": data}


class TestScoreZoteroCatalogCandidate(unittest.TestCase):
    def _context(self, **overrides):
        defaults = dict(
            item_key="B1", isbn="9783031466373", title="Some Book",
            editors=("Jane Editor",), publisher="Acme Press", year=2020,
        )
        defaults.update(overrides)
        return BookContext(**defaults)

    def test_exact_isbn_match_scores_one(self):
        context = self._context()
        candidate = _book_section("Ch1", "Some Book", isbn="9783031466373")["data"]
        self.assertEqual(score_zotero_catalog_candidate(candidate, context), 1.0)

    def test_title_only_match_scores_base(self):
        context = self._context(isbn=None, publisher=None, year=None, editors=())
        candidate = _book_section("Ch1", "Some Book")["data"]
        self.assertEqual(score_zotero_catalog_candidate(candidate, context), 0.6)

    def test_year_match_adds_bonus(self):
        context = self._context(isbn=None, publisher=None, editors=())
        candidate = _book_section("Ch1", "Some Book", date="2020-01")["data"]
        self.assertAlmostEqual(score_zotero_catalog_candidate(candidate, context), 0.75)

    def test_publisher_match_adds_bonus(self):
        context = self._context(isbn=None, year=None, editors=())
        candidate = _book_section("Ch1", "Some Book", publisher="Acme Press")["data"]
        self.assertAlmostEqual(score_zotero_catalog_candidate(candidate, context), 0.75)

    def test_editor_overlap_adds_bonus(self):
        context = self._context(isbn=None, publisher=None, year=None)
        candidate = _book_section("Ch1", "Some Book", editors=["Jane Editor"])["data"]
        self.assertAlmostEqual(score_zotero_catalog_candidate(candidate, context), 0.8)

    def test_score_is_capped_at_one(self):
        context = self._context(isbn=None)
        candidate = _book_section(
            "Ch1", "Some Book", date="2020-01", publisher="Acme Press", editors=["Jane Editor"],
        )["data"]
        self.assertEqual(score_zotero_catalog_candidate(candidate, context), 1.0)


class TestFindZoteroCatalogCandidates(unittest.TestCase):
    def _context(self, title="Some Book", **overrides):
        defaults = dict(item_key="B1", isbn=None, title=title, editors=(), publisher=None, year=None)
        defaults.update(overrides)
        return BookContext(**defaults)

    def test_returns_empty_when_no_title_match(self):
        result = find_zotero_catalog_candidates(self._context(), {})
        self.assertEqual(result, [])

    def test_returns_candidate_for_exact_title_match(self):
        item = _book_section(
            "Introduction", "Some Book", pages="1-20", authors=["Jane Author"], doi="10.1/ch1",
        )
        index = {"Some Book": [item]}
        result = find_zotero_catalog_candidates(self._context(), index)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "Introduction")
        self.assertEqual(result[0].authors, ("Jane Author",))
        self.assertEqual(result[0].printed_page_number, 1)
        self.assertEqual(result[0].chapter_doi, "10.1/ch1")
        self.assertEqual(result[0].source, "zotero_catalog")

    def test_does_not_fuzzy_match_similar_titles(self):
        item = _book_section("Introduction", "Some Book (2nd ed.)")
        index = {"Some Book (2nd ed.)": [item]}
        result = find_zotero_catalog_candidates(self._context(title="Some Book"), index)
        self.assertEqual(result, [])

    def test_deduplicates_by_title_keeping_higher_score(self):
        weak = _book_section("Introduction", "Some Book")
        strong = _book_section("Introduction", "Some Book", isbn="9783031466373")
        index = {"Some Book": [weak, strong]}
        result = find_zotero_catalog_candidates(self._context(isbn="9783031466373"), index)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata_confidence, 1.0)


class TestZoteroCatalogMetadataStrategy(unittest.IsolatedAsyncioTestCase):
    async def test_applicable_and_fetch(self):
        item = _book_section("Introduction", "Some Book")
        strategy = ZoteroCatalogMetadataStrategy({"Some Book": [item]})
        context = BookContext(item_key="B1", isbn=None, title="Some Book", editors=(), publisher=None, year=None)
        self.assertTrue(strategy.applicable(context))
        result = await strategy.fetch(context)
        self.assertEqual(len(result), 1)

    async def test_not_applicable_when_title_absent(self):
        strategy = ZoteroCatalogMetadataStrategy({})
        context = BookContext(item_key="B1", isbn=None, title="Some Book", editors=(), publisher=None, year=None)
        self.assertFalse(strategy.applicable(context))
