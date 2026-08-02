"""Unit tests for backend.services.chapter_segmentation.analyze_attachment_with_strategies
and build_book_context. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
sections 6 and 8.
"""

import io
import unittest

from pypdf import PdfWriter

from backend.services.chapter_evidence.types import BookContext, ChapterCandidate
from backend.services.chapter_segmentation import (
    analyze_attachment,
    analyze_attachment_with_strategies,
    build_book_context,
)


def _blank_pdf(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_with_outline(num_pages: int, entries: list[tuple[str, int]]) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in entries:
        writer.add_outline_item(title, page_number)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class _FakeMetadataStrategy:
    """Minimal stand-in for the MetadataStrategy protocol -- structurally
    identical to CrossrefMetadataStrategy/ZoteroCatalogMetadataStrategy's
    own applicable()/fetch() shape, used here to isolate orchestration
    tests from real HTTP/library-index construction."""

    def __init__(self, candidates: list[ChapterCandidate]):
        self._candidates = candidates

    def applicable(self, context: BookContext) -> bool:
        return bool(self._candidates)

    async def fetch(self, context: BookContext) -> list[ChapterCandidate]:
        return self._candidates


def _context(**overrides) -> BookContext:
    defaults = dict(item_key="B1", isbn=None, title="Some Book", editors=(), publisher=None, year=None)
    defaults.update(overrides)
    return BookContext(**defaults)


_FILLER = "Unrelated body filler text, nothing chapter-related in this passage at all."

# 20 pages, chapters starting at indices 5 and 12 -- deliberately well
# outside _toc_scan_indices(pages)'s front/back exclusion zone (indices
# {0,1,2,19} for a 20-page document), matching the same padding convention
# backend/tests/test_chapter_segmentation.py's own
# test_llm_toc_extraction_fires_when_heuristic_finds_nothing already uses
# and explains: a chapter starting inside that scan zone can never be
# content-search-located, since _locate_toc_entries excludes those pages
# from candidate consideration entirely.
_TWO_CHAPTER_PAGES = [
    _FILLER,  # 0
    _FILLER,  # 1
    _FILLER,  # 2
    _FILLER,  # 3
    _FILLER,  # 4
    "Introduction\nJane Author\n\nBody text opening the chapter.\n\n1",  # 5
    "...continued introduction text with real body content here.\n\n2",  # 6
    "...more continued introduction text with real body content.\n\n3",  # 7
    "...final continued introduction text with real body content.\n\n4",  # 8
    _FILLER,  # 9
    _FILLER,  # 10
    _FILLER,  # 11
    "Comparing Citation Styles\n\nJohn Smith\n\nBody text opening this chapter.\n\n5",  # 12
    "...continued citation styles text with real body content here.\n\n6",  # 13
    "...more continued citation styles text with real body content.\n\n7",  # 14
    "...final continued citation styles text with real body content.\n\n8",  # 15
    _FILLER,  # 16
    _FILLER,  # 17
    _FILLER,  # 18
    _FILLER,  # 19
]


class TestAnalyzeAttachmentWithStrategiesFallback(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_analyze_attachment_when_all_strategies_empty(self):
        pages = ["Just filler prose, nothing chapter-related here at all. " * 5] * 3
        pdf_bytes = _blank_pdf(3)
        result = await analyze_attachment_with_strategies(
            pages, pdf_bytes, _context(), _FakeMetadataStrategy([]), crossref_strategy=None,
        )
        expected = analyze_attachment(pages)
        self.assertEqual(result["chapters"], expected["chapters"])
        self.assertEqual(result["diagnostics"]["strategies_used"], [])
        self.assertEqual(result["diagnostics"]["outline_candidates_found"], 0)


class TestAnalyzeAttachmentWithStrategiesOutlineOnly(unittest.IsolatedAsyncioTestCase):
    async def test_outline_only_uses_direct_localization_and_fixed_confidence(self):
        pdf_bytes = _pdf_with_outline(20, [("Introduction", 5), ("Comparing Citation Styles", 12)])
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(), _FakeMetadataStrategy([]), crossref_strategy=None,
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["title"], "Introduction")
        self.assertEqual(chapters[0]["pdf_start_index"], 5)
        self.assertEqual(chapters[0]["source"], "outline")
        self.assertEqual(chapters[0]["confidence"], 0.98)
        self.assertEqual(chapters[1]["pdf_start_index"], 12)
        self.assertEqual(result["diagnostics"]["strategies_used"], ["outline"])


class TestAnalyzeAttachmentWithStrategiesOutlineBookTitleBookmark(unittest.IsolatedAsyncioTestCase):
    async def test_excludes_outline_entry_matching_the_books_own_title(self):
        # A half-title/title page bookmark commonly repeats the book's own
        # title verbatim -- it never appears in a printed table of
        # contents (so _is_back_matter/_is_production_bookmark can't
        # recognize it by generic vocabulary) but is never a real chapter
        # either. Reproduces a real false-positive found evaluating
        # against 9783907297339.pdf and 9783907297285.pdf, where the
        # book's own title as its first bookmark otherwise leaks straight
        # into the final chapter list.
        pdf_bytes = _pdf_with_outline(
            20, [("A Book About Citation Tools", 2), ("Introduction", 5), ("Comparing Citation Styles", 12)]
        )
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(title="A Book About Citation Tools"),
            _FakeMetadataStrategy([]), crossref_strategy=None,
        )
        titles = [c["title"] for c in result["chapters"]]
        self.assertNotIn("A Book About Citation Tools", titles)
        self.assertIn("Introduction", titles)
        self.assertIn("Comparing Citation Styles", titles)


class TestAnalyzeAttachmentWithStrategiesCrossrefOnly(unittest.IsolatedAsyncioTestCase):
    async def test_crossref_only_localizes_via_content_search(self):
        pdf_bytes = _blank_pdf(20)  # no outline
        crossref_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                chapter_doi="10.1/ch1", source="crossref", metadata_confidence=1.0,
            ),
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                chapter_doi="10.1/ch2", source="crossref", metadata_confidence=1.0,
            ),
        ]
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(isbn="9783031466373"),
            _FakeMetadataStrategy([]), crossref_strategy=_FakeMetadataStrategy(crossref_candidates),
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["title"], "Introduction")
        self.assertEqual(chapters[0]["pdf_start_index"], 5)
        self.assertEqual(chapters[0]["source"], "crossref")
        self.assertGreater(chapters[0]["confidence"], 0.0)
        self.assertEqual(result["diagnostics"]["strategies_used"], ["crossref"])


class TestAnalyzeAttachmentWithStrategiesZoteroCatalogOnly(unittest.IsolatedAsyncioTestCase):
    async def test_weak_catalog_confidence_pulls_final_confidence_down(self):
        pdf_bytes = _blank_pdf(8)  # no outline
        catalog_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
        ]
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(),
            _FakeMetadataStrategy(catalog_candidates), crossref_strategy=None,
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["source"], "zotero_catalog")
        # match_confidence's own ceiling is 1.0, so 0.6 * match_confidence <= 0.6
        self.assertLessEqual(chapters[0]["confidence"], 0.6)
        self.assertEqual(result["diagnostics"]["strategies_used"], ["zotero_catalog"])


class TestAnalyzeAttachmentWithStrategiesMultiStrategyFusion(unittest.IsolatedAsyncioTestCase):
    async def test_crossref_and_zotero_catalog_fuse_with_corroboration(self):
        """Crossref finds only "Introduction" (DOI-backed, confidence 1.0);
        the Zotero-catalog strategy independently finds BOTH "Introduction"
        (weaker, confidence 0.6) and "Comparing Citation Styles" (catalog-
        only). This exercises both strategies contributing simultaneously --
        never tested anywhere in the plan's test suite despite the design
        spec's testing section requiring it -- and covers two distinct
        fusion behaviors in one scenario:

        - "Introduction" is corroborated by both sources; _merge_two_metadata_lists
          is winner-take-all on metadata_confidence, so the stronger
          Crossref candidate's `source` ("crossref") must win over the
          weaker catalog one, not be silently overwritten by whichever
          strategy happened to run/insert last.
        - "Comparing Citation Styles" is catalog-only and must still survive
          fusion untouched (source "zotero_catalog"), proving corroboration
          on one chapter doesn't cost coverage of another.
        """
        pdf_bytes = _blank_pdf(20)  # no outline
        crossref_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                chapter_doi="10.1/ch1", source="crossref", metadata_confidence=1.0,
            ),
        ]
        catalog_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
        ]
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(isbn="9783031466373"),
            _FakeMetadataStrategy(catalog_candidates), crossref_strategy=_FakeMetadataStrategy(crossref_candidates),
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        titles = [c["title"] for c in chapters]
        self.assertEqual(titles, ["Introduction", "Comparing Citation Styles"])
        self.assertEqual(result["diagnostics"]["strategies_used"], ["crossref", "zotero_catalog"])
        # Corroboration on "Introduction": the stronger, DOI-backed Crossref
        # candidate's data must win, not the weaker catalog one.
        self.assertEqual(chapters[0]["source"], "crossref")
        # "Comparing Citation Styles" is catalog-only -- must still survive.
        self.assertEqual(chapters[1]["source"], "zotero_catalog")

    async def test_dedup_prevents_duplicate_rows_when_ordering_defeats_alignment(self):
        """Reproduces the original bug scenario: Crossref and the
        Zotero-catalog strategy both find the same two real chapters, but
        return them in a DIFFERENT relative order -- _align's greedy,
        monotonic-order matching pairs "Introduction" across the two lists
        but then cannot pair "Comparing Citation Styles" (it would need to
        match backwards), so merge_metadata_sources leaves it as two
        separate ChapterCandidates, both missing pdf_page_index, both
        projecting to an identical TocEntry. Without the Part-1 dedup fix,
        this produces two overlapping rows for the same chapter and lets
        the weaker (last-inserted) candidate silently win the
        entry_to_candidate lookup; with the fix, exactly one row survives
        and it carries the stronger (DOI-backed, higher-confidence)
        candidate's data.
        """
        pdf_bytes = _blank_pdf(20)  # no outline
        # Crossref returns "Comparing Citation Styles" BEFORE "Introduction"
        # -- the reverse of catalog order below -- which is what defeats
        # _align's monotonic greedy matching for the second title.
        crossref_candidates = [
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                chapter_doi="10.1/ch2", source="crossref", metadata_confidence=1.0,
            ),
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                chapter_doi="10.1/ch1", source="crossref", metadata_confidence=1.0,
            ),
        ]
        catalog_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
        ]
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(isbn="9783031466373"),
            _FakeMetadataStrategy(catalog_candidates), crossref_strategy=_FakeMetadataStrategy(crossref_candidates),
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        # The bug produced two rows (one per source) for whichever title
        # _align failed to pair. The fix collapses them back to one.
        self.assertEqual(len(chapters), 2)
        titles = [c["title"] for c in chapters]
        self.assertEqual(titles, ["Introduction", "Comparing Citation Styles"])
        self.assertEqual(len(titles), len(set(titles)))
        # Each surviving row must carry the STRONGER (Crossref, DOI-backed)
        # candidate's source -- confirming the dedup kept the winner, not
        # just an arbitrary last-inserted candidate.
        for chapter in chapters:
            self.assertEqual(chapter["source"], "crossref")


class TestBuildBookContext(unittest.TestCase):
    def test_builds_context_from_book_data(self):
        book_data = {
            "key": "BOOK1", "ISBN": "978-3-031-46637-3", "title": "Some Book",
            "publisher": "Acme Press", "date": "2020-01",
            "creators": [
                {"creatorType": "editor", "firstName": "Jane", "lastName": "Editor"},
                {"creatorType": "author", "firstName": "Not", "lastName": "AnEditor"},
            ],
        }
        context = build_book_context(book_data)
        self.assertEqual(context.item_key, "BOOK1")
        self.assertEqual(context.isbn, "9783031466373")
        self.assertEqual(context.title, "Some Book")
        self.assertEqual(context.publisher, "Acme Press")
        self.assertEqual(context.year, 2020)
        self.assertEqual(context.editors, ("Jane Editor",))

    def test_handles_missing_fields(self):
        context = build_book_context({"key": "BOOK2", "title": "Untitled"})
        self.assertIsNone(context.isbn)
        self.assertIsNone(context.publisher)
        self.assertIsNone(context.year)
        self.assertEqual(context.editors, ())
