"""Same-library exact-bookTitle lookup of already-catalogued bookSection
items. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.3.
"""

from backend.services.chapter_common import year_from_date
from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, _first_page_number

_BASE_SCORE = 0.6
_YEAR_BONUS = 0.15
_PUBLISHER_BONUS = 0.15
_EDITOR_OVERLAP_BONUS = 0.2


def _last_names(creators: list[dict], creator_type: str) -> set[str]:
    return {
        c.get("lastName", "").strip().lower()
        for c in creators
        if c.get("creatorType") == creator_type and c.get("lastName", "").strip()
    }


def score_zotero_catalog_candidate(book_section_data: dict, context: BookContext) -> float:
    """0.0-1.0. An exact match on the candidate's own (book-inherited) ISBN
    field against context.isbn short-circuits to 1.0. Otherwise starts from
    a base score for the bookTitle match already required to reach this
    function at all, then adds one bonus per additional corroborating
    field, capped at 1.0. These constants are initial estimates, not
    empirically calibrated -- see design spec 5.3 for the recalibration
    plan.
    """
    candidate_isbn = (book_section_data.get("ISBN") or "").replace("-", "").strip()
    context_isbn = (context.isbn or "").replace("-", "").strip()
    if candidate_isbn and context_isbn and candidate_isbn == context_isbn:
        return 1.0

    score = _BASE_SCORE

    candidate_year = year_from_date(book_section_data.get("date"))
    if candidate_year is not None and context.year is not None and candidate_year == context.year:
        score += _YEAR_BONUS

    candidate_publisher = (book_section_data.get("publisher") or "").strip().lower()
    context_publisher = (context.publisher or "").strip().lower()
    if candidate_publisher and context_publisher and candidate_publisher == context_publisher:
        score += _PUBLISHER_BONUS

    candidate_editors = _last_names(book_section_data.get("creators", []), "editor")
    context_editor_last_names = {name.split()[-1].lower() for name in context.editors if name.strip()}
    if candidate_editors and context_editor_last_names and candidate_editors & context_editor_last_names:
        score += _EDITOR_OVERLAP_BONUS

    return min(score, 1.0)


def find_zotero_catalog_candidates(
    context: BookContext, book_sections_by_title: dict[str, list[dict]],
) -> list[ChapterCandidate]:
    """Looks up book_sections_by_title[context.title.strip()] -- an exact,
    non-fuzzy string match on the bookSection item's own `bookTitle` field
    against the book's `title` field. No match key -> []. Each matching
    bookSection item becomes one ChapterCandidate; candidates are sorted by
    printed_page_number (unknowns last) to approximate book order, then
    deduplicated by title, keeping the higher-metadata_confidence one.
    """
    matches = book_sections_by_title.get(context.title.strip(), [])
    candidates: list[ChapterCandidate] = []
    for item in matches:
        data = item["data"]
        authors = tuple(
            f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
            for c in data.get("creators", [])
            if c.get("creatorType") == "author"
        )
        pages = data.get("pages") or ""
        candidates.append(ChapterCandidate(
            title=data.get("title", ""),
            authors=authors,
            printed_page_number=_first_page_number(pages) if pages else None,
            chapter_doi=data.get("DOI") or None,
            source="zotero_catalog",
            metadata_confidence=score_zotero_catalog_candidate(data, context),
        ))

    candidates.sort(key=lambda c: (c.printed_page_number is None, c.printed_page_number or 0))

    deduped: dict[str, ChapterCandidate] = {}
    for c in candidates:
        existing = deduped.get(c.title)
        if existing is None or c.metadata_confidence > existing.metadata_confidence:
            deduped[c.title] = c
    return list(deduped.values())


class ZoteroCatalogMetadataStrategy:
    def __init__(self, book_sections_by_title: dict[str, list[dict]]):
        self._book_sections_by_title = book_sections_by_title

    def applicable(self, context: BookContext) -> bool:
        return context.title.strip() in self._book_sections_by_title

    async def fetch(self, context: BookContext) -> list[ChapterCandidate]:
        return find_zotero_catalog_candidates(context, self._book_sections_by_title)
