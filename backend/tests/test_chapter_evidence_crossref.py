"""Unit tests for backend.services.chapter_evidence.crossref_strategy."""

import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.services.chapter_evidence.crossref_strategy import (
    CrossrefMetadataStrategy,
    fetch_crossref_chapters,
    normalize_isbn,
)
from backend.services.chapter_evidence.types import BookContext


class TestNormalizeIsbn(unittest.TestCase):
    def test_extracts_isbn13(self):
        self.assertEqual(normalize_isbn("978-3-031-46637-3"), "9783031466373")

    def test_falls_back_to_isbn10(self):
        self.assertEqual(normalize_isbn("0-306-40615-2"), "0306406152")

    def test_normalizes_lowercase_isbn10_check_digit(self):
        self.assertEqual(normalize_isbn("030640615x"), "030640615X")

    def test_picks_isbn13_over_isbn10_when_both_present(self):
        self.assertEqual(normalize_isbn("0306406152; 978-3-031-46637-3"), "9783031466373")

    def test_returns_none_for_empty(self):
        self.assertIsNone(normalize_isbn(""))

    def test_returns_none_for_unparseable(self):
        self.assertIsNone(normalize_isbn("not an isbn"))


def _crossref_response(items: list[dict]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"message": {"items": items}}
    response.raise_for_status = MagicMock()
    return response


class TestFetchCrossrefChapters(unittest.IsolatedAsyncioTestCase):
    async def test_parses_book_chapter_records_only(self):
        items = [
            {"type": "book", "title": ["The Whole Book"], "DOI": "10.1/book"},
            {
                "type": "book-chapter", "title": ["Introduction"],
                "author": [{"given": "Jane", "family": "Author"}],
                "page": "1-20", "DOI": "10.1/ch1",
            },
        ]
        http_client = AsyncMock()
        http_client.get = AsyncMock(return_value=_crossref_response(items))
        result = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "Introduction")
        self.assertEqual(result[0].authors, ("Jane Author",))
        self.assertEqual(result[0].printed_page_number, 1)
        self.assertEqual(result[0].chapter_doi, "10.1/ch1")
        self.assertEqual(result[0].source, "crossref")
        self.assertEqual(result[0].metadata_confidence, 1.0)

    async def test_appends_subtitle_to_the_truncated_crossref_title(self):
        # Crossref splits a chapter's real (often colon/period-separated)
        # printed heading into separate title/subtitle fields -- using only
        # title[0] silently truncates it (e.g. "Re:Law." instead of the
        # real "Re:Law. Recht überdenken und neu gestalten"). A short,
        # truncated title is a much weaker/more ambiguous content-search
        # target: it can fuzzy-match a brief in-body mention of the same
        # short phrase in an unrelated, nearby chapter almost as well as the
        # real chapter-opening page, and unlike a long title, the
        # transitive clustering gap can then bridge that spurious mention
        # into the real page as if they were one continuous chapter run.
        # Reproduces a real mislocation found evaluating against
        # 9783847432364.pdf -- see
        # docs/superpowers/plans/2026-08-01-chapter-segmentation-strategy-pipeline.md.
        items = [
            {
                "type": "book-chapter", "title": ["Re:Law."], "subtitle": ["Recht überdenken und neu gestalten"],
                "author": [], "page": "205-228", "DOI": "10.1/ch1",
            },
        ]
        http_client = AsyncMock()
        http_client.get = AsyncMock(return_value=_crossref_response(items))
        result = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(result[0].title, "Re:Law. Recht überdenken und neu gestalten")

    async def test_no_subtitle_leaves_title_unchanged(self):
        items = [
            {
                "type": "book-chapter", "title": ["Trans Soldat*innen und ein neues Gemeinsames"],
                "author": [], "page": "73-92", "DOI": "10.1/ch1",
            },
        ]
        http_client = AsyncMock()
        http_client.get = AsyncMock(return_value=_crossref_response(items))
        result = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(result[0].title, "Trans Soldat*innen und ein neues Gemeinsames")

    async def test_returns_empty_list_for_unregistered_isbn(self):
        http_client = AsyncMock()
        http_client.get = AsyncMock(return_value=_crossref_response([]))
        result = await fetch_crossref_chapters("0000000000000", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(result, [])

    async def test_network_exception_is_swallowed(self):
        import httpx
        http_client = AsyncMock()
        http_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        result = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(result, [])

    async def test_cache_hit_short_circuits_without_http_call(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            http_client = AsyncMock()
            http_client.get = AsyncMock(return_value=_crossref_response([
                {
                    "type": "book-chapter", "title": ["Introduction"],
                    "author": [], "page": "1-20", "DOI": "10.1/ch1",
                },
            ]))
            first = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=cache_dir, contact_email=None)
            self.assertEqual(len(first), 1)
            http_client.get.assert_called_once()

            second = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=cache_dir, contact_email=None)
            self.assertEqual(len(second), 1)
            http_client.get.assert_called_once()  # still once -- cache hit, no new call

    async def test_corrupted_cache_file_is_treated_as_miss(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cache_path = cache_dir / "9783031466373.json"
            cache_path.write_text("not valid json {{{", encoding="utf-8")

            http_client = AsyncMock()
            http_client.get = AsyncMock(return_value=_crossref_response([
                {
                    "type": "book-chapter", "title": ["Introduction"],
                    "author": [{"given": "Jane", "family": "Author"}],
                    "page": "1-20", "DOI": "10.1/ch1",
                },
            ]))

            result = await fetch_crossref_chapters(
                "9783031466373", http_client, cache_dir=cache_dir, contact_email=None
            )

            http_client.get.assert_called_once()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].title, "Introduction")

    async def test_empty_result_is_still_cached(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            http_client = AsyncMock()
            http_client.get = AsyncMock(return_value=_crossref_response([]))
            await fetch_crossref_chapters("0000000000000", http_client, cache_dir=cache_dir, contact_email=None)
            http_client.get.assert_called_once()
            await fetch_crossref_chapters("0000000000000", http_client, cache_dir=cache_dir, contact_email=None)
            http_client.get.assert_called_once()  # still once


class TestCrossrefMetadataStrategy(unittest.IsolatedAsyncioTestCase):
    def _context(self, isbn):
        return BookContext(item_key="B1", isbn=isbn, title="Some Book", editors=(), publisher=None, year=None)

    async def test_applicable_false_without_isbn(self):
        strategy = CrossrefMetadataStrategy(AsyncMock(), cache_dir=None, contact_email=None)
        self.assertFalse(strategy.applicable(self._context(None)))

    async def test_applicable_true_with_isbn(self):
        strategy = CrossrefMetadataStrategy(AsyncMock(), cache_dir=None, contact_email=None)
        self.assertTrue(strategy.applicable(self._context("9783031466373")))

    async def test_fetch_returns_empty_without_isbn(self):
        strategy = CrossrefMetadataStrategy(AsyncMock(), cache_dir=None, contact_email=None)
        result = await strategy.fetch(self._context(None))
        self.assertEqual(result, [])
