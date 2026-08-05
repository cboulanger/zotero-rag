"""Identifies which spans of a book's page text are navigational/
bibliographic material (table of contents, secondary listings, running
headers, chapter headings) that backend/services/chapter_segmentation.py's
heuristics actually key off -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md sections 3-4.

Deliberately imports "private" helpers directly from chapter_segmentation.py
rather than reimplementing equivalent logic, so the preserved regions are
exactly what the real algorithm reads, by construction.
"""

from dataclasses import dataclass

from backend.services.chapter_segmentation import (
    _locate_toc_entries,
    _running_header_lines,
    find_toc_candidates,
)


@dataclass(frozen=True)
class RegionMap:
    full_pages: frozenset[int]  # pages kept 100% verbatim: the TOC and any
    # secondary listing page (a part divider, a repeated chapter list, ...)
    header_lines: frozenset[str]  # normalized running-header line forms
    # (chapter_segmentation._normalize_header_line) kept verbatim wherever
    # they recur, on any page
    heading_windows: dict[int, tuple[int, int]]  # page_index -> (start, end)
    # character span kept verbatim: the chapter-heading text a title was
    # actually fuzzy-matched against. Filled in by Task 2; empty for now.


def classify_regions(pages: list[str]) -> RegionMap:
    """Region classification driven by the real production TOC/listing
    detection -- see module docstring."""
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}
    _located, _unlocated, non_content_pages = _locate_toc_entries(
        pages, toc_entries, exclude_indices=toc_page_indices,
    )
    header_lines = _running_header_lines(tuple(pages))
    return RegionMap(
        full_pages=frozenset(non_content_pages),
        header_lines=header_lines,
        heading_windows={},
    )
