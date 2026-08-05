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

from rapidfuzz import fuzz

from backend.services.chapter_segmentation import (
    _LOCATE_MIN_HEAD_CHARS,
    _LOCATE_SCORE_THRESHOLD,
    _RUNNING_HEADER_SCAN_LINES,
    _locate_toc_entries,
    _normalize_header_line,
    _running_header_lines,
    find_toc_candidates,
)

# Mirrors the literal `[:200]` head-window slice in chapter_segmentation's
# locate_chapter_start_candidates -- not a named constant there, so this
# comment plus TestClassifyRegionsHeadingWindows's consistency test are what
# keep the two in sync if that literal ever changes.
_HEAD_WINDOW_CHARS = 200


@dataclass(frozen=True)
class RegionMap:
    full_pages: frozenset[int]  # pages kept 100% verbatim: the TOC and any
    # secondary listing page (a part divider, a repeated chapter list, ...)
    header_lines: frozenset[str]  # normalized running-header line forms
    # (chapter_segmentation._normalize_header_line) kept verbatim wherever
    # they recur, on any page
    heading_windows: dict[int, tuple[int, int]]  # page_index -> (start, end)
    # character span kept verbatim: the chapter-heading text a title was
    # actually fuzzy-matched against.


def _header_stripped_offset(text: str, header_lines: frozenset[str]) -> int:
    """Character offset in `text` where content begins after skipping
    leading running-header/blank lines -- mirrors
    chapter_segmentation._strip_running_headers's line-skip decision
    exactly, but returns a position in `text`'s own coordinates instead of
    the trimmed string, so a verbatim span can be sliced directly out of
    the original page."""
    if not header_lines:
        return 0
    offset = 0
    for line in text.splitlines(keepends=True)[:_RUNNING_HEADER_SCAN_LINES]:
        norm = _normalize_header_line(line.rstrip("\r\n"))
        if norm in header_lines or len(norm) == 0:
            offset += len(line)
        else:
            break
    return offset


def classify_regions(pages: list[str]) -> RegionMap:
    """Region classification driven by the real production TOC/listing
    detection -- see module docstring."""
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}
    located, _unlocated, non_content_pages = _locate_toc_entries(
        pages, toc_entries, exclude_indices=toc_page_indices,
    )
    header_lines = _running_header_lines(tuple(pages))

    # Every winning title's variant, pooled together: a page qualifies for a
    # heading window if it scores above threshold against ANY of them, not
    # just the specific entry it was originally located for -- deliberately
    # more inclusive than strictly necessary (preserving one extra page's
    # heading text is harmless), and it naturally covers the case where a
    # chapter's title legitimately re-scores on more than one of its own
    # pages without needing to reimplement locate_chapter_start_candidates's
    # internal clustering.
    titles = {variant for entry, _match in located for variant in (entry.title, *entry.title_variants)}

    heading_windows: dict[int, tuple[int, int]] = {}
    for index, text in enumerate(pages):
        if index in non_content_pages:
            continue
        offset = _header_stripped_offset(text, header_lines)
        head = text[offset:offset + _HEAD_WINDOW_CHARS]
        if len(head.strip()) < _LOCATE_MIN_HEAD_CHARS:
            continue
        head_lower = head.lower()
        if any(fuzz.partial_ratio(title.lower(), head_lower) >= _LOCATE_SCORE_THRESHOLD for title in titles):
            heading_windows[index] = (offset, offset + len(head))

    return RegionMap(
        full_pages=frozenset(non_content_pages),
        header_lines=header_lines,
        heading_windows=heading_windows,
    )
