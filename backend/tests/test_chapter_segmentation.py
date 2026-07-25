"""Unit tests for backend.services.chapter_segmentation."""

import unittest

from backend.services.chapter_segmentation import (
    TocEntry,
    extract_page_texts_from_pdf_bytes,
    find_toc_candidates,
)
from backend.services.chapter_segmentation import (
    extract_printed_page_number,
    locate_chapter_start,
)
from backend.services.chapter_segmentation import extract_authors_near


class TestFindTocCandidates(unittest.TestCase):
    def test_finds_dotted_leader_entries(self):
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
            "Some front-matter page with no TOC pattern at all.",
        ]
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0], TocEntry(title="Introduction to Reference Management", printed_page_number=1, source_page_index=0))
        self.assertEqual(entries[2].title, "Zotero in Practice")
        self.assertEqual(entries[2].printed_page_number, 89)

    def test_ignores_non_toc_lines(self):
        pages = ["Just some ordinary prose with numbers like 1999 in it, no leaders here."]
        entries = find_toc_candidates(pages)
        self.assertEqual(entries, [])

    def test_matches_whitespace_leaders_too(self):
        pages = ["Bibliographic Software Overview          12"]
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Bibliographic Software Overview")
        self.assertEqual(entries[0].printed_page_number, 12)


class TestLocateChapterStart(unittest.TestCase):
    def test_finds_matching_page_by_content(self):
        pages = [
            "CONTENTS\nComparing Citation Styles ..... 3\n",  # TOC page itself
            "Some unrelated front matter.",
            "Comparing Citation Styles\nBy Jane Author\n\nThis chapter examines...",
        ]
        index = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices={0})
        self.assertEqual(index, 2)

    def test_returns_none_when_no_good_match(self):
        pages = ["Nothing related to the query here at all, just filler prose."]
        index = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertIsNone(index)


class TestExtractPrintedPageNumber(unittest.TestCase):
    def test_finds_arabic_footer_number(self):
        text = "Comparing Citation Styles\nBy Jane Author\n\nBody text here.\n\n45"
        self.assertEqual(extract_printed_page_number(text), "45")

    def test_finds_roman_numeral_header(self):
        text = "xii\nPreface\n\nBody text of the preface."
        self.assertEqual(extract_printed_page_number(text), "xii")

    def test_returns_none_when_no_number_present(self):
        text = "Just a page of prose with no isolated numeral line at all here."
        self.assertIsNone(extract_printed_page_number(text))


class TestExtractAuthorsNear(unittest.TestCase):
    def test_finds_person_entities_at_chapter_start(self):
        # "By" is needed as an authorial cue -- spaCy's small model doesn't
        # reliably tag bare names without it
        text = "Comparing Citation Styles\n\nBy Jane Author and John Smith\n\nThis chapter examines APA and MLA styles."
        authors = extract_authors_near(text)
        self.assertIn("Jane Author", authors)
        self.assertIn("John Smith", authors)

    def test_returns_empty_list_when_no_names_found(self):
        # Full names avoid a known false positive: spaCy's small model can
        # misclassify bare acronyms like "APA"/"MLA" as PERSON entities.
        text = "This chapter examines American Psychological Association and Modern Language Association citation styles in detail."
        authors = extract_authors_near(text)
        self.assertEqual(authors, [])


if __name__ == "__main__":
    unittest.main()
