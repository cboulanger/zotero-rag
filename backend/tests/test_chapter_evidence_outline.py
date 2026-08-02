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

    def test_keeps_part_divider_and_back_matter_titles_as_boundary_markers(self):
        # Part I/Bibliography are excluded from the "real chapter" count
        # used for the plausibility gate below, but ARE kept in the
        # returned candidate list -- downstream (chapter_segmentation.
        # _chapters_from_located) relies on their page position to
        # correctly bound their neighbors' ranges, exactly as it already
        # does for the pure-heuristic printed-TOC-scan path. Only
        # chapter_segmentation is responsible for excluding them from the
        # final emitted chapter list.
        entries = [
            ("Part I", 0), ("Chapter One", 5), ("Chapter Two", 25),
            ("Chapter Three", 45), ("Bibliography", 65),
        ]
        pdf_bytes = _build_pdf(100, entries)
        candidates = extract_outline_candidates(pdf_bytes)
        titles = [c.title for c in candidates]
        self.assertIn("Part I", titles)
        self.assertIn("Bibliography", titles)
        self.assertIn("Chapter One", titles)

    def test_plausibility_gate_counts_only_real_chapters(self):
        # 2 real chapters over 300 pages -> 150 pages/real-entry, right at
        # the plausibility band's edge. Adding Cover/Index (structural,
        # excluded from the count) must not push the ratio over the edge
        # by inflating the entry count to 4 (which would give 75/entry --
        # a different, misleading number).
        entries = [
            ("Cover", 0), ("Chapter One", 5), ("Chapter Two", 155), ("Index", 295),
        ]
        pdf_bytes = _build_pdf(300, entries)
        titles = [c.title for c in extract_outline_candidates(pdf_bytes)]
        self.assertIn("Chapter One", titles)
        self.assertIn("Chapter Two", titles)

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

    def test_rejects_outline_when_chapters_nested_under_part_dividers(self):
        # Real books commonly structure their actual chapters as children of
        # "Part I"/"Part II" top-level nodes. Without this check, the
        # top-level-only read silently returns a sparse, WRONG "chapter" list
        # (just front/back matter around the part dividers) instead of
        # deferring to another strategy or the heuristic fallback -- this
        # reproduces a real regression found evaluating against
        # 9781771993661.pdf / 9782375460122.pdf (see
        # docs/superpowers/plans/2026-08-01-chapter-segmentation-strategy-pipeline.md).
        writer = PdfWriter()
        for _ in range(200):
            writer.add_blank_page(width=200, height=200)
        writer.add_outline_item("Foreword", 5)
        part1 = writer.add_outline_item("Part I Something", 10)
        writer.add_outline_item("Chapter One", 12, parent=part1)
        writer.add_outline_item("Chapter Two", 60, parent=part1)
        part2 = writer.add_outline_item("Part II Something Else", 100)
        writer.add_outline_item("Chapter Three", 102, parent=part2)
        writer.add_outline_item("Chapter Four", 150, parent=part2)
        writer.add_outline_item("Afterword", 190)
        buf = io.BytesIO()
        writer.write(buf)
        self.assertEqual(extract_outline_candidates(buf.getvalue()), [])

    def test_does_not_reject_outline_solely_for_production_bookmarks(self):
        # Cover/Half Title/Title/Copyright/Backcover never appear in a
        # printed table of contents (which is what _is_back_matter's title
        # list was built from) but are common PDF outline bookmark labels
        # -- reproduces a real false-positive found evaluating against
        # 9783907297339.pdf. They must not count toward the plausibility
        # ratio (3 real chapters over 100 pages is plausible; treating all
        # 7 entries as "chapters" would give a different, misleading
        # ratio) -- excluding them from the FINAL chapter list is
        # chapter_segmentation._chapters_from_located's job, not this
        # function's (see test_keeps_part_divider_and_back_matter_titles_
        # as_boundary_markers above).
        entries = [
            ("Cover", 0), ("Half Title", 2), ("Title", 3), ("Copyright", 4),
            ("Chapter One", 10), ("Chapter Two", 40), ("Chapter Three", 70),
            ("Backcover", 99),
        ]
        pdf_bytes = _build_pdf(100, entries)
        titles = [c.title for c in extract_outline_candidates(pdf_bytes)]
        self.assertIn("Chapter One", titles)
        self.assertIn("Chapter Two", titles)
        self.assertIn("Chapter Three", titles)

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
