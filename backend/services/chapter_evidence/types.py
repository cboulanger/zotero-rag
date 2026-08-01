"""Shared types for the chapter_evidence strategy pipeline. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 3 and 4.
"""

from dataclasses import dataclass
from typing import Protocol


def _first_page_number(range_str: str) -> int | None:
    """Parses a printed page-range string ("85-113") into its first page
    number (85). Returns None on anything unparseable -- never raises."""
    if not range_str:
        return None
    first = range_str.split("-")[0].strip()
    return int(first) if first.isdigit() else None


@dataclass(frozen=True)
class ChapterCandidate:
    title: str
    authors: tuple[str, ...] = ()
    printed_page_number: int | None = None   # from a TOC/metadata source;
    # never assumed equal to pdf_page_index.
    pdf_page_index: int | None = None          # set only by a direct-
    # localization strategy (currently: outline). None means "still needs
    # content-search localization."
    chapter_doi: str | None = None             # set by Crossref, or by the
    # zotero_catalog strategy when the matched bookSection item has its own
    # populated DOI field.
    source: str = "heuristic"                  # "outline" | "crossref" |
    # "zotero_catalog" | "outline+crossref" | "outline+zotero_catalog" |
    # "heuristic" | "llm"
    metadata_confidence: float = 1.0            # how certain the SOURCE
    # (not the eventual PDF location) is that this candidate is the right
    # chapter of the right book -- fixed at 1.0 for outline/Crossref
    # candidates, graduated for zotero_catalog candidates.


@dataclass(frozen=True)
class BookContext:
    item_key: str
    isbn: str | None
    title: str
    editors: tuple[str, ...]
    publisher: str | None
    year: int | None


class MetadataStrategy(Protocol):
    def applicable(self, context: BookContext) -> bool: ...
    async def fetch(self, context: BookContext) -> list[ChapterCandidate]: ...


class StructureStrategy(Protocol):
    def applicable(self, pdf_bytes: bytes) -> bool: ...
    def extract(self, pdf_bytes: bytes) -> list[ChapterCandidate]: ...
