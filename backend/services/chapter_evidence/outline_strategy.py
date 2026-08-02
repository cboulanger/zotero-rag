"""PDF embedded outline (bookmark) read. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.1.
"""

import io
import logging

from pypdf import PdfReader

from backend.services.chapter_common import _is_back_matter, _is_part_divider
from backend.services.chapter_evidence.types import ChapterCandidate

logger = logging.getLogger(__name__)

_MIN_ENTRIES = 2
_MIN_PAGES_PER_ENTRY = 3
_MAX_PAGES_PER_ENTRY = 150


def extract_outline_candidates(content: bytes) -> list[ChapterCandidate]:
    """Reads the PDF's embedded outline/bookmark catalog and returns one
    ChapterCandidate per TOP-LEVEL entry that survives the
    _is_part_divider/_is_back_matter filters, each with pdf_page_index
    resolved directly -- no content search needed. Nested (child) outline
    entries are not surfaced as separate chapters (see design spec 5.1's
    top-level-only limitation). Returns [] if the PDF has no outline
    catalog, reading it raises (malformed/encrypted PDF), the filtered
    result has fewer than 2 entries, or the average pages-per-entry ratio
    is implausible (a sparse top level usually means real chapters are
    nested one level down, e.g. under Part dividers) -- never raises.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        outline = reader.outline
    except Exception:
        logger.info("extract_outline_candidates: failed to read PDF/outline", exc_info=True)
        return []

    entries: list[ChapterCandidate] = []
    for item in outline:
        if isinstance(item, list):
            continue  # nested (child) entries -- not surfaced in Phase 1
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            continue
        title = (item.title or "").strip()
        if not title or _is_part_divider(title) or _is_back_matter(title):
            continue
        entries.append(ChapterCandidate(title=title, pdf_page_index=page_index, source="outline"))

    if len(entries) < _MIN_ENTRIES:
        logger.info("extract_outline_candidates: too few top-level entries (%d), rejecting", len(entries))
        return []

    total_pages = len(reader.pages)
    pages_per_entry = total_pages / len(entries)
    if not (_MIN_PAGES_PER_ENTRY <= pages_per_entry <= _MAX_PAGES_PER_ENTRY):
        logger.info(
            "extract_outline_candidates: implausible pages-per-entry ratio %.1f "
            "(%d entries, %d pages), rejecting",
            pages_per_entry, len(entries), total_pages,
        )
        return []

    return entries


class OutlineStructureStrategy:
    def applicable(self, pdf_bytes: bytes) -> bool:
        return len(extract_outline_candidates(pdf_bytes)) > 0

    def extract(self, pdf_bytes: bytes) -> list[ChapterCandidate]:
        return extract_outline_candidates(pdf_bytes)
