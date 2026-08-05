"""Unit tests for scripts/evaluation_redaction/ -- the redaction pipeline
that turns real evaluation-book page text into a corpus safe to commit (see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md).
Pure logic, no PDFs/network/OCR involved, so these run in the default
suite."""

import unittest

from scripts.evaluation_redaction.region_classification import classify_regions

# Same shape as backend/tests/test_chapter_segmentation.py's
# TestAnalyzeAttachment._fake_book_pages() -- a proven-working minimal book
# fixture (TOC page + two located chapters). Kept as a separate copy since
# these two test files don't import from each other.
_FAKE_BOOK_PAGES = [
    "CONTENTS\n"
    "Introduction ..... 1\n"
    "Comparing Citation Styles ..... 3\n"
    "Appendix ..... 5\n",
    "Introduction\nJane Author\n\nThis book explores reference management.\n\n1",
    "...continued text follows here, with enough body content on this "
    "page that it clearly reads as a real continuation of the "
    "chapter rather than a blank divider page between sections.\n\n2",
    "Comparing Citation Styles\n\nJohn Smith\n\nThis chapter examines APA and MLA.\n\n3",
    "...continued chapter text, with enough body content on this "
    "final page that it clearly reads as a real continuation of "
    "the chapter rather than a blank divider page.\n\n4",
]


class TestClassifyRegionsFullPages(unittest.TestCase):
    def test_toc_page_is_a_full_preserved_page(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertEqual(regions.full_pages, frozenset({0}))

    def test_chapter_body_pages_are_not_full_preserved_pages(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertNotIn(1, regions.full_pages)
        self.assertNotIn(3, regions.full_pages)

    def test_header_lines_empty_when_book_has_no_running_header(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertEqual(regions.header_lines, frozenset())


if __name__ == "__main__":
    unittest.main()
