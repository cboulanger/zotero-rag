"""
Metadata search agent — catalog lookup using bibliographic filters.

Searches the Qdrant payload index (no query vector / no semantic similarity)
to return items matching author, year range, item type, or title keywords.
"""

import logging
from typing import Optional
from pydantic import BaseModel

from backend.db.vector_store import VectorStore
from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent

logger = logging.getLogger(__name__)


class MetadataResult(BaseModel):
    """A single item returned by the metadata catalog search."""

    item_id: str
    library_id: str
    title: str
    authors: list[str]
    year: Optional[int]
    item_type: Optional[str]
    text_preview: Optional[str]   # first few words of the first indexed chunk


def _format_authors(authors: list[str]) -> str:
    if not authors:
        return "Unknown"
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]} & {authors[1]}"
    return f"{authors[0]} et al."


def _results_to_context(results: list[MetadataResult]) -> str:
    """Format metadata results as a numbered list for the synthesis prompt."""
    if not results:
        return "No items found matching the metadata criteria."
    lines = ["Items found in the library catalog:\n"]
    for i, r in enumerate(results, 1):
        year_str = f" ({r.year})" if r.year else ""
        type_str = f" [{r.item_type}]" if r.item_type else ""
        lines.append(f"[S{i}] {_format_authors(r.authors)}{year_str} — {r.title}{type_str}")
        if r.text_preview:
            lines.append(f"      \"{r.text_preview}...\"")
    return "\n".join(lines)


class MetadataAgent(BaseAgent):
    """Bibliographic catalog search agent (no re-ranking by semantic similarity)."""

    def __init__(self, vector_store: VectorStore):
        self._vector_store = vector_store

    @property
    def name(self) -> str:
        return "metadata"

    @property
    def capability_prompt(self) -> str:
        return (
            "Searches the library catalog by bibliographic metadata without reading document content.\n"
            "Best for: listing or finding items by author name, publication year range, item type\n"
            "(book / journalArticle / etc.), or title keywords.\n"
            "Use when the question asks WHAT items exist rather than WHAT they contain."
        )

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        **kwargs,
    ) -> AgentResult:
        limit: int = kwargs.get("metadata_limit", 30)

        raw = self._vector_store.get_items_by_metadata(
            library_ids=library_ids if library_ids else None,
            filters=filters,
            limit=limit,
        )

        results: list[MetadataResult] = []
        for payload in raw:
            results.append(MetadataResult(
                item_id=payload.get("item_key", "unknown"),
                library_id=payload.get("library_id", ""),
                title=payload.get("title") or "Untitled",
                authors=payload.get("authors") or [],
                year=payload.get("year"),
                item_type=payload.get("item_type"),
                text_preview=payload.get("text_preview"),
            ))

        # Sort by year (ascending, unknowns last)
        results.sort(key=lambda r: (r.year is None, r.year or 0))

        # Build SourceInfo list so the orchestrator can link items in the UI
        sources = [
            dict(
                item_id=r.item_id,
                library_id=r.library_id,
                title=r.title,
                authors=r.authors,
                year=r.year,
                score=1.0,   # metadata matches have no similarity score
            )
            for r in results
        ]

        context_text = _results_to_context(results)
        logger.info(f"MetadataAgent returned {len(results)} items")
        return AgentResult(
            agent_name=self.name,
            context_text=context_text,
            sources=sources,
        )
