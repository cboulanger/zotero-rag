"""
Metadata filter model shared across the query pipeline.
"""

from typing import Optional
from pydantic import BaseModel


class MetadataFilters(BaseModel):
    """Bibliographic metadata filters applied during vector search and catalog lookup."""

    year_min: Optional[int] = None
    year_max: Optional[int] = None
    authors: list[str] = []         # last names (lowercase) to match against stored author strings
    item_types: list[str] = []      # e.g. ["book", "journalArticle"]
    title_keywords: list[str] = []  # words to match against the title field

    def is_empty(self) -> bool:
        return not any([
            self.year_min is not None,
            self.year_max is not None,
            self.authors,
            self.item_types,
            self.title_keywords,
        ])
