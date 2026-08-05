"""Unit tests for scripts/evaluation_redaction/ -- the redaction pipeline
that turns real evaluation-book page text into a corpus safe to commit (see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md).
Pure logic, no PDFs/network/OCR involved, so these run in the default
suite."""

import unittest

from scripts.evaluation_redaction.region_classification import classify_regions, RegionMap
from scripts.evaluation_redaction.redact import build_preserve_mask, redact_page
from scripts.evaluation_redaction.wordlists import build_word_pool, locale_for_detected_language, pick_word

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


class TestClassifyRegionsHeadingWindows(unittest.TestCase):
    def test_chapter_start_pages_get_a_heading_window(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertIn(1, regions.heading_windows)
        self.assertIn(3, regions.heading_windows)

    def test_heading_window_covers_the_chapter_title(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        start, end = regions.heading_windows[1]
        self.assertIn("Introduction", _FAKE_BOOK_PAGES[1][start:end])

    def test_continuation_pages_get_no_heading_window(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertNotIn(2, regions.heading_windows)
        self.assertNotIn(4, regions.heading_windows)

    def test_toc_page_gets_no_heading_window(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertNotIn(0, regions.heading_windows)

    def test_heading_window_matches_what_locate_chapter_start_actually_scores(self):
        # Consistency guard for the literal 200-char window mirrored from
        # chapter_segmentation.locate_chapter_start_candidates -- if that
        # literal ever changes, this test catches the drift.
        from backend.services.chapter_segmentation import (
            _running_header_lines,
            _strip_running_headers,
        )
        regions = classify_regions(_FAKE_BOOK_PAGES)
        header_lines = _running_header_lines(tuple(_FAKE_BOOK_PAGES))
        for index in (1, 3):
            start, end = regions.heading_windows[index]
            production_head = _strip_running_headers(_FAKE_BOOK_PAGES[index], header_lines)[:200]
            self.assertEqual(_FAKE_BOOK_PAGES[index][start:end], production_head)


class TestBuildPreserveMask(unittest.TestCase):
    def test_full_page_is_entirely_preserved(self):
        regions = RegionMap(full_pages=frozenset({0}), header_lines=frozenset(), heading_windows={})
        text = "Contents\nIntroduction ..... 1\n"
        mask = build_preserve_mask(text, 0, regions)
        self.assertTrue(all(mask))

    def test_heading_window_is_preserved_rest_is_not(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={1: (0, 12)})
        text = "Introduction\n\nBody prose continues here well past the window."
        mask = build_preserve_mask(text, 1, regions)
        self.assertTrue(all(mask[:12]))
        self.assertFalse(any(mask[12:]))

    def test_running_header_line_preserved_wherever_it_recurs(self):
        regions = RegionMap(
            full_pages=frozenset(), heading_windows={},
            header_lines=frozenset({"my book title"}),
        )
        text = "Body text first.\nMy Book Title\nMore body text after.\n"
        mask = build_preserve_mask(text, 2, regions)
        header_start = text.index("My Book Title")
        header_end = header_start + len("My Book Title\n")
        self.assertTrue(all(mask[header_start:header_end]))
        self.assertFalse(mask[0])

    def test_ordinary_body_page_with_no_regions_is_entirely_unpreserved(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        mask = build_preserve_mask("Just ordinary body prose.\n", 5, regions)
        self.assertFalse(any(mask))


class TestLocaleForDetectedLanguage(unittest.TestCase):
    def test_maps_known_language_codes(self):
        self.assertEqual(locale_for_detected_language("deu"), "de_DE")
        self.assertEqual(locale_for_detected_language("fra"), "fr_FR")
        self.assertEqual(locale_for_detected_language("spa"), "es_ES")
        self.assertEqual(locale_for_detected_language("eng"), "en_US")

    def test_falls_back_to_english_for_the_combined_default(self):
        self.assertEqual(locale_for_detected_language("eng+deu+fra+spa"), "en_US")


class TestBuildWordPool(unittest.TestCase):
    def test_pool_has_real_words_bucketed_by_length(self):
        pool = build_word_pool("deu")
        self.assertIn(5, pool)
        for word in pool[5]:
            self.assertEqual(len(word), 5)

    def test_unknown_language_falls_back_to_english_pool(self):
        pool = build_word_pool("xyz")
        self.assertTrue(pool)


class TestPickWord(unittest.TestCase):
    def test_picks_a_word_of_the_exact_length_when_available(self):
        pool = {4: ["word"], 5: ["words"]}
        self.assertEqual(len(pick_word(pool, 4, seed=0)), 4)

    def test_falls_back_to_nearest_length_when_no_exact_match(self):
        pool = {4: ["word"]}
        self.assertEqual(pick_word(pool, 9, seed=0), "word")

    def test_deterministic_for_the_same_seed(self):
        pool = {5: ["reala", "realb", "realc"]}
        self.assertEqual(pick_word(pool, 5, seed=7), pick_word(pool, 5, seed=7))


class TestRedactPage(unittest.TestCase):
    _POOL = {4: ["real"], 5: ["reals"], 6: ["length"]}

    def test_preserved_span_survives_unchanged(self):
        regions = RegionMap(full_pages=frozenset({0}), header_lines=frozenset(), heading_windows={})
        text = "Contents\nIntroduction ..... 1\n"
        out = redact_page(text, 0, regions, self._POOL, book_salt="book1")
        self.assertEqual(out, text)

    def test_digits_and_whitespace_survive_unchanged(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        text = "words 42\nwords\n"
        out = redact_page(text, 1, regions, self._POOL, book_salt="book1")
        self.assertIn("42", out)
        self.assertEqual(out.count("\n"), text.count("\n"))

    def test_body_word_is_replaced_with_a_same_length_real_word(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        out = redact_page("words", 1, regions, {5: ["reals"]}, book_salt="book1")
        self.assertEqual(out, "reals")

    def test_same_input_produces_same_output(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        text = "words are here"
        out1 = redact_page(text, 1, regions, self._POOL, book_salt="book1")
        out2 = redact_page(text, 1, regions, self._POOL, book_salt="book1")
        self.assertEqual(out1, out2)

    def test_different_book_salt_changes_output(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        pool = {5: ["reala", "realb", "realc", "reald"]}
        out1 = redact_page("words", 1, regions, pool, book_salt="book1")
        out2 = redact_page("words", 1, regions, pool, book_salt="book2")
        self.assertNotEqual(out1, out2)

    def test_roman_numeral_page_number_survives_unchanged(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        out = redact_page("xiv", 1, regions, self._POOL, book_salt="book1")
        self.assertEqual(out, "xiv")


if __name__ == "__main__":
    unittest.main()
