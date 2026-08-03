"""Unit tests for backend.services.chapter_evidence.fusion."""

import unittest

from backend.services.chapter_evidence.fusion import merge_candidates, merge_metadata_sources
from backend.services.chapter_evidence.types import ChapterCandidate


class TestMergeMetadataSources(unittest.TestCase):
    def test_single_source_passthrough(self):
        candidates = [ChapterCandidate(title="Introduction", source="crossref")]
        result = merge_metadata_sources([candidates])
        self.assertEqual(result, candidates)

    def test_all_empty_returns_empty(self):
        self.assertEqual(merge_metadata_sources([[], []]), [])

    def test_single_source_is_sorted_by_printed_page_number(self):
        # A real Crossref API response is not guaranteed to list chapters in
        # book order (found empirically -- see
        # docs/superpowers/plans/2026-08-01-chapter-segmentation-strategy-pipeline.md).
        # chapter_segmentation._locate_toc_entries' second-pass ordering-based
        # disambiguation assumes list POSITION mirrors book order (mirroring
        # find_toc_candidates' own regex-scan entries, which are naturally
        # read top-to-bottom off the printed TOC) -- with only one metadata
        # source, _merge_two_metadata_lists' own sort never runs, so an
        # unsorted single source silently broke that assumption and caused
        # genuinely locatable chapters to be wrongly reported as ambiguous.
        candidates = [
            ChapterCandidate(title="Third Chapter", source="crossref", printed_page_number=30),
            ChapterCandidate(title="First Chapter", source="crossref", printed_page_number=1),
            ChapterCandidate(title="Second Chapter", source="crossref", printed_page_number=15),
        ]
        result = merge_metadata_sources([candidates])
        self.assertEqual([c.title for c in result], ["First Chapter", "Second Chapter", "Third Chapter"])

    def test_higher_confidence_candidate_wins_aligned_pair(self):
        crossref = [ChapterCandidate(title="Introduction to the Subject", source="crossref", metadata_confidence=1.0)]
        catalog = [ChapterCandidate(title="Introduction to the Subject", source="zotero_catalog", metadata_confidence=0.6)]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "crossref")

    def test_tie_prefers_crossref(self):
        crossref = [ChapterCandidate(title="Introduction to the Subject", source="crossref", metadata_confidence=0.8)]
        catalog = [ChapterCandidate(title="Introduction to the Subject", source="zotero_catalog", metadata_confidence=0.8)]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(result[0].source, "crossref")

    def test_exact_doi_agreement_forces_confidence_to_one(self):
        crossref = [ChapterCandidate(
            title="Introduction to the Subject", source="crossref",
            metadata_confidence=1.0, chapter_doi="10.1234/abc",
        )]
        catalog = [ChapterCandidate(
            title="Introduction to the Subject", source="zotero_catalog",
            metadata_confidence=0.6, chapter_doi="10.1234/abc",
        )]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(result[0].metadata_confidence, 1.0)

    def test_unmatched_entries_from_both_lists_are_kept(self):
        crossref = [ChapterCandidate(title="Chapter One", source="crossref")]
        catalog = [ChapterCandidate(title="Totally Different Chapter", source="zotero_catalog")]
        result = merge_metadata_sources([crossref, catalog])
        titles = {c.title for c in result}
        self.assertEqual(titles, {"Chapter One", "Totally Different Chapter"})

    def test_non_contiguous_matches_preserve_book_order(self):
        # Regression test: an unmatched entry that belongs between two matched
        # chapters must stay in its book position instead of being pushed to
        # the tail of the merged list (see fusion.py's _merge_two_metadata_lists).
        # Titles are chosen distinct enough (per rapidfuzz.token_sort_ratio) to
        # avoid an unrelated false-positive fuzzy match between the two
        # non-corresponding entries ("Chapter Two" vs "Chapter Three" style
        # titles score ~75, above the alignment threshold, and would collapse
        # into a single false match instead of exercising the ordering bug).
        crossref = [
            ChapterCandidate(title="Introduction to the Subject", source="crossref", printed_page_number=1),
            ChapterCandidate(title="Concluding Remarks and Future Work", source="crossref", printed_page_number=30),
        ]
        catalog = [
            ChapterCandidate(title="Introduction to the Subject", source="zotero_catalog", printed_page_number=1),
            ChapterCandidate(title="Historical Background and Context", source="zotero_catalog", printed_page_number=15),
        ]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(
            [c.title for c in result],
            ["Introduction to the Subject", "Historical Background and Context", "Concluding Remarks and Future Work"],
        )


class TestMergeCandidates(unittest.TestCase):
    def test_passthrough_when_metadata_empty(self):
        outline = [ChapterCandidate(title="Chapter One", pdf_page_index=5, source="outline")]
        self.assertEqual(merge_candidates(outline, []), outline)

    def test_passthrough_when_outline_empty(self):
        metadata = [ChapterCandidate(title="Chapter One", source="crossref")]
        self.assertEqual(merge_candidates([], metadata), metadata)

    def test_matched_pair_combines_outline_location_with_metadata_fields(self):
        outline = [ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline")]
        metadata = [ChapterCandidate(
            title="Chapter One", authors=("Jane Author",), chapter_doi="10.1234/xyz",
            source="crossref", metadata_confidence=1.0,
        )]
        result = merge_candidates(outline, metadata)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pdf_page_index, 10)
        self.assertEqual(result[0].authors, ("Jane Author",))
        self.assertEqual(result[0].source, "outline+crossref")

    def test_unmatched_outline_entry_kept_without_localization(self):
        outline = [
            ChapterCandidate(title="Foreword", pdf_page_index=2, source="outline"),
            ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline"),
        ]
        metadata = [ChapterCandidate(title="Chapter One", source="crossref")]
        result = merge_candidates(outline, metadata)
        by_title = {c.title: c for c in result}
        self.assertIn("Foreword", by_title)
        self.assertIsNone(by_title["Foreword"].chapter_doi)
        self.assertEqual(by_title["Foreword"].pdf_page_index, 2)

    def test_unmatched_metadata_entry_kept_without_pdf_page_index(self):
        outline = [ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline")]
        metadata = [
            ChapterCandidate(title="Chapter One", source="crossref"),
            ChapterCandidate(title="Chapter Two", source="crossref"),
        ]
        result = merge_candidates(outline, metadata)
        chapter_two = next(c for c in result if c.title == "Chapter Two")
        self.assertIsNone(chapter_two.pdf_page_index)
        self.assertEqual(chapter_two.source, "crossref")

    def test_filters_structural_entries_from_both_lists(self):
        outline = [
            ChapterCandidate(title="Part I", pdf_page_index=0, source="outline"),
            ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline"),
        ]
        metadata = [ChapterCandidate(title="Chapter One", source="crossref")]
        result = merge_candidates(outline, metadata)
        self.assertEqual([c.title for c in result], ["Chapter One"])


if __name__ == "__main__":
    unittest.main()
