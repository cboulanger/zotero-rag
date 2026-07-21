"""
Metadata filter model shared across the query pipeline.
"""

from typing import Optional
from pydantic import BaseModel


class CitationTarget(BaseModel):
    """A work the question asks about being CITED/DISCUSSED by other items —
    not authored by. Kept structurally separate from MetadataFilters.authors
    (which means "written by") so the router cannot conflate the two."""

    author: str                     # lowercase surname
    year: Optional[int] = None      # LLM disambiguation hint only — never used to filter
    title_keywords: list[str] = []  # salient words from the cited work's title


class MetadataFilters(BaseModel):
    """Bibliographic metadata filters applied during vector search and catalog lookup."""

    year_min: Optional[int] = None
    year_max: Optional[int] = None
    authors: list[str] = []         # last names (lowercase) to match against stored author strings
    item_types: list[str] = []      # e.g. ["book", "journalArticle"]
    title_keywords: list[str] = []  # words to match against the title field
    tags: list[str] = []            # Zotero tags/keywords (case-insensitive) to match
    citation_targets: list[CitationTarget] = []

    def is_empty(self) -> bool:
        return not any([
            self.year_min is not None,
            self.year_max is not None,
            self.authors,
            self.item_types,
            self.title_keywords,
            self.tags,
            self.citation_targets,
        ])
