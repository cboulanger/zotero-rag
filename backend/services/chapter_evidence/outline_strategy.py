"""PDF embedded outline (bookmark) read. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.1.
"""

import io
import logging

from pypdf import PdfReader

from backend.services.chapter_common import _is_non_chapter_structural_title, _is_part_divider
from backend.services.chapter_evidence.types import ChapterCandidate

logger = logging.getLogger(__name__)

_MIN_ENTRIES = 2
_MIN_PAGES_PER_ENTRY = 3
_MAX_PAGES_PER_ENTRY = 150


def extract_outline_candidates(content: bytes) -> list[ChapterCandidate]:
    """Reads the PDF's embedded outline/bookmark catalog and returns one
    ChapterCandidate per TOP-LEVEL entry, each with pdf_page_index resolved
    directly -- no content search needed. Nested (child) outline entries
    are not surfaced as separate chapters (see design spec 5.1's
    top-level-only limitation).

    Entries recognized as structural (_is_non_chapter_structural_title --
    part dividers, standard back-matter sections, PDF production/
    front-matter bookmarks like "Cover"/"Copyright") ARE included in the
    returned list -- downstream (chapter_segmentation._chapters_from_located)
    relies on their page position to correctly bound neighboring real
    chapters, exactly as it already does for the pure-heuristic TOC-scan
    path. They are excluded only from the "how many real chapters does the
    top level contain" plausibility count below.

    Returns [] if the PDF has no outline catalog, reading it raises
    (malformed/encrypted PDF), a part-divider entry has nested children
    (the real chapters live one level down, so the top level is not
    trustworthy AT ALL -- not merely sparse), the count of non-structural
    entries is fewer than 2, or the average pages-per-non-structural-entry
    ratio is implausible -- never raises.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        outline = reader.outline
    except Exception:
        logger.info("extract_outline_candidates: failed to read PDF/outline", exc_info=True)
        return []

    entries: list[ChapterCandidate] = []
    real_chapter_count = 0
    for index, item in enumerate(outline):
        if isinstance(item, list):
            continue  # nested (child) entries -- not surfaced in Phase 1
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            continue
        title = (item.title or "").strip()
        if not title:
            continue
        if (
            _is_part_divider(title)
            and index + 1 < len(outline)
            and isinstance(outline[index + 1], list)
            and outline[index + 1]
        ):
            # This part divider has nested children -- the real chapters
            # live one level down, so the top level is not a trustworthy
            # chapter list at all (not merely sparse). Bail out entirely
            # rather than return whatever front/back matter survives
            # around the part dividers.
            logger.info(
                "extract_outline_candidates: chapters nested under part divider %r, rejecting",
                title,
            )
            return []
        if not _is_non_chapter_structural_title(title):
            real_chapter_count += 1
        entries.append(ChapterCandidate(title=title, pdf_page_index=page_index, source="outline"))

    if real_chapter_count < _MIN_ENTRIES:
        logger.info("extract_outline_candidates: too few real top-level entries (%d), rejecting", real_chapter_count)
        return []

    total_pages = len(reader.pages)
    pages_per_entry = total_pages / real_chapter_count
    if not (_MIN_PAGES_PER_ENTRY <= pages_per_entry <= _MAX_PAGES_PER_ENTRY):
        logger.info(
            "extract_outline_candidates: implausible pages-per-entry ratio %.1f "
            "(%d real entries, %d pages), rejecting",
            pages_per_entry, real_chapter_count, total_pages,
        )
        return []

    return entries


class OutlineStructureStrategy:
    def applicable(self, pdf_bytes: bytes) -> bool:
        return len(extract_outline_candidates(pdf_bytes)) > 0

    def extract(self, pdf_bytes: bytes) -> list[ChapterCandidate]:
        return extract_outline_candidates(pdf_bytes)
