"""Fusion of candidate chapter lists from multiple strategies. See design
spec docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 6.
"""

from dataclasses import replace

from rapidfuzz import fuzz

from backend.services.chapter_common import _is_back_matter, _is_part_divider
from backend.services.chapter_evidence.types import ChapterCandidate

_ALIGN_SCORE_THRESHOLD = 70.0


def _align(list_a: list[ChapterCandidate], list_b: list[ChapterCandidate]) -> list[tuple[int, int]]:
    """Returns index pairs (i, j) into list_a/list_b that fuzzy-match on
    title, honoring a monotonic-order constraint: once (i, j) is matched,
    no later pair may use an index <= j from list_b. Greedy, processes
    list_a in order -- mirrors the "TOC listing order is book order"
    constraint chapter_segmentation.py's _locate_toc_entries already uses.
    """
    pairs: list[tuple[int, int]] = []
    last_j = -1
    for i, a in enumerate(list_a):
        best_j = None
        best_score = 0.0
        for j in range(last_j + 1, len(list_b)):
            score = fuzz.token_sort_ratio(a.title.lower(), list_b[j].title.lower())
            if score >= _ALIGN_SCORE_THRESHOLD and score > best_score:
                best_score = score
                best_j = j
        if best_j is not None:
            pairs.append((i, best_j))
            last_j = best_j
    return pairs


def _filter_structural(candidates: list[ChapterCandidate]) -> list[ChapterCandidate]:
    return [c for c in candidates if not _is_part_divider(c.title) and not _is_back_matter(c.title)]


def _merge_two_metadata_lists(
    primary: list[ChapterCandidate], secondary: list[ChapterCandidate],
) -> list[ChapterCandidate]:
    pairs = _align(primary, secondary)
    matched_primary = {i for i, _ in pairs}
    matched_secondary = {j for _, j in pairs}
    merged: list[ChapterCandidate] = []
    for i, j in pairs:
        a, b = primary[i], secondary[j]
        winner = a if a.metadata_confidence >= b.metadata_confidence else b
        if a.chapter_doi and b.chapter_doi and a.chapter_doi == b.chapter_doi:
            winner = replace(winner, metadata_confidence=1.0)
        merged.append(winner)
    merged.extend(c for i, c in enumerate(primary) if i not in matched_primary)
    merged.extend(c for j, c in enumerate(secondary) if j not in matched_secondary)
    merged.sort(key=lambda c: (c.printed_page_number is None, c.printed_page_number or 0))
    return merged


def merge_metadata_sources(strategy_results: list[list[ChapterCandidate]]) -> list[ChapterCandidate]:
    """Consolidates candidate lists from multiple MetadataStrategy
    instances, in priority order (earlier lists win ties), into one list.
    See design spec section 6.1. With a single non-empty source, returns it
    unchanged.
    """
    non_empty = [r for r in strategy_results if r]
    if not non_empty:
        return []
    result = list(non_empty[0])
    for next_list in non_empty[1:]:
        result = _merge_two_metadata_lists(result, next_list)
    if len(non_empty) == 1:
        # _merge_two_metadata_lists sorts its own output by
        # printed_page_number -- do the same here so a single source's
        # result carries the same book-order guarantee (chapter_segmentation.
        # _locate_toc_entries' second-pass disambiguation relies on list
        # position mirroring book order; a real Crossref response is not
        # guaranteed to list chapters in book order).
        result.sort(key=lambda c: (c.printed_page_number is None, c.printed_page_number or 0))
    return result


def merge_candidates(
    outline_candidates: list[ChapterCandidate],
    metadata_candidates: list[ChapterCandidate],
) -> list[ChapterCandidate]:
    """Merges the outline's direct-localization candidates with the
    (already consolidated) metadata candidates. See design spec section 6.2.
    """
    outline_filtered = _filter_structural(outline_candidates)
    metadata_filtered = _filter_structural(metadata_candidates)

    if not outline_filtered:
        return metadata_filtered
    if not metadata_filtered:
        return outline_filtered

    pairs = _align(outline_filtered, metadata_filtered)
    matched_outline = {i for i, _ in pairs}
    matched_metadata = {j for _, j in pairs}

    merged: list[ChapterCandidate] = []
    for i, j in pairs:
        outline_entry = outline_filtered[i]
        metadata_entry = metadata_filtered[j]
        merged.append(replace(
            metadata_entry,
            pdf_page_index=outline_entry.pdf_page_index,
            source=f"outline+{metadata_entry.source}",
        ))
    merged.extend(c for i, c in enumerate(outline_filtered) if i not in matched_outline)
    merged.extend(c for j, c in enumerate(metadata_filtered) if j not in matched_metadata)

    merged.sort(key=lambda c: c.pdf_page_index if c.pdf_page_index is not None else 10 ** 9)
    return merged
