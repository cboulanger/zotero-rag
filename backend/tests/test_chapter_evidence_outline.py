"""Unit tests for backend.services.chapter_evidence.outline_strategy."""

import io
import unittest
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter

from backend.services.chapter_evidence.outline_strategy import (
    OutlineStructureStrategy,
    extract_outline_candidates,
)


def _build_pdf(num_pages: int, outline_entries: list[tuple[str, int]] | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in outline_entries or []:
        writer.add_outline_item(title, page_number)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestExtractOutlineCandidates(unittest.TestCase):
    def test_extracts_top_level_entries_with_page_index(self):
        # 5 entries over 100 pages -> 20 pages/entry, comfortably within the
        # plausibility band (3-150 pages/entry).
        entries = [(f"Chapter {i}", i * 20) for i in range(5)]
        pdf_bytes = _build_pdf(100, entries)
        candidates = extract_outline_candidates(pdf_bytes)
        self.assertEqual(len(candidates), 5)
        self.assertEqual(candidates[0].title, "Chapter 0")
        self.assertEqual(candidates[0].pdf_page_index, 0)
        self.assertEqual(candidates[1].pdf_page_index, 20)
        self.assertEqual(candidates[0].source, "outline")
        self.assertIsNone(candidates[0].chapter_doi)
        self.assertIsNone(candidates[0].printed_page_number)

    def test_returns_empty_list_when_no_outline(self):
        pdf_bytes = _build_pdf(10)
        self.assertEqual(extract_outline_candidates(pdf_bytes), [])

    def test_returns_empty_list_on_malformed_pdf(self):
        self.assertEqual(extract_outline_candidates(b"not a pdf at all"), [])

    def test_filters_part_divider_and_back_matter_titles(self):
        entries = [
            ("Part I", 0), ("Chapter One", 5), ("Chapter Two", 25),
            ("Chapter Three", 45), ("Bibliography", 65),
        ]
        pdf_bytes = _build_pdf(100, entries)
        candidates = extract_outline_candidates(pdf_bytes)
        titles = [c.title for c in candidates]
        self.assertNotIn("Part I", titles)
        self.assertNotIn("Bibliography", titles)
        self.assertIn("Chapter One", titles)

    def test_rejects_implausibly_sparse_outline(self):
        # Only 2 top-level entries (e.g. Intro/Conclusion) over 400 pages
        # implies real chapters are nested one level down (a Part-divider
        # top level) -- 200 pages/entry exceeds the 150 max.
        entries = [("Introduction", 0), ("Conclusion", 390)]
        pdf_bytes = _build_pdf(400, entries)
        self.assertEqual(extract_outline_candidates(pdf_bytes), [])

    def test_rejects_single_entry_outline(self):
        pdf_bytes = _build_pdf(50, [("Only Entry", 0)])
        self.assertEqual(extract_outline_candidates(pdf_bytes), [])

    def test_ignores_nested_child_entries(self):
        # Build a PDF whose outline has one top-level entry with a nested
        # child -- the child must not be surfaced as its own candidate.
        writer = PdfWriter()
        for _ in range(60):
            writer.add_blank_page(width=200, height=200)
        parent = writer.add_outline_item("Part I", 0)
        writer.add_outline_item("Nested Chapter", 5, parent=parent)
        writer.add_outline_item("Chapter Two", 30)
        buf = io.BytesIO()
        writer.write(buf)
        candidates = extract_outline_candidates(buf.getvalue())
        titles = [c.title for c in candidates]
        self.assertNotIn("Nested Chapter", titles)

    def test_skips_entry_with_none_title_instead_of_literal_none_string(self):
        # pypdf's Destination.title is typed Optional[str] -- a malformed or
        # corrupted outline entry can yield item.title is None. Exercised via
        # a mocked PdfReader since pypdf's own outline-parsing path normally
        # defaults a missing /Title to "" rather than None, making this hard
        # to reproduce through PdfWriter/PdfReader alone.
        none_title_item = MagicMock(title=None)
        real_item = MagicMock(title="Chapter Two")
        mock_reader = MagicMock()
        mock_reader.outline = [none_title_item, real_item, MagicMock(title="Chapter Three")]
        mock_reader.pages = list(range(30))
        mock_reader.get_destination_page_number.side_effect = [0, 10, 20]

        with patch(
            "backend.services.chapter_evidence.outline_strategy.PdfReader",
            return_value=mock_reader,
        ):
            candidates = extract_outline_candidates(b"irrelevant -- reader is mocked")

        titles = [c.title for c in candidates]
        self.assertNotIn("None", titles)
        self.assertEqual(titles, ["Chapter Two", "Chapter Three"])


class TestOutlineStructureStrategy(unittest.TestCase):
    def test_applicable_true_and_extract_matches_module_function(self):
        entries = [(f"Chapter {i}", i * 20) for i in range(5)]
        pdf_bytes = _build_pdf(100, entries)
        strategy = OutlineStructureStrategy()
        self.assertTrue(strategy.applicable(pdf_bytes))
        self.assertEqual(len(strategy.extract(pdf_bytes)), 5)

    def test_applicable_false_when_no_outline(self):
        pdf_bytes = _build_pdf(10)
        strategy = OutlineStructureStrategy()
        self.assertFalse(strategy.applicable(pdf_bytes))
