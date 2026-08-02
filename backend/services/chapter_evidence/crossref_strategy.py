"""Crossref chapter lookup by ISBN. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.2.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, _first_page_number

logger = logging.getLogger(__name__)

_CROSSREF_BASE_URL = "https://api.crossref.org/works"
_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY_SECONDS = 1.0


def normalize_isbn(raw: str) -> Optional[str]:
    """Extracts the first ISBN-13 (13 digits after stripping separators)
    from a Zotero ISBN field, falling back to the first ISBN-10 (10
    digits, last may be 'X'), or None if nothing usable is present."""
    if not raw:
        return None
    candidates = re.split(r"[;\s]+", raw.strip())
    stripped = [re.sub(r"[-\s]", "", c) for c in candidates if c.strip()]
    for c in stripped:
        if len(c) == 13 and c.isdigit():
            return c
    for c in stripped:
        if len(c) == 10 and c[:-1].isdigit() and (c[-1].isdigit() or c[-1].upper() == "X"):
            return c[:-1] + c[-1].upper()
    return None


def _cache_path(cache_dir: Path, isbn: str) -> Path:
    return cache_dir / f"{isbn}.json"


def _load_cache(cache_dir: Optional[Path], isbn: str) -> Optional[list[ChapterCandidate]]:
    if cache_dir is None:
        return None
    path = _cache_path(cache_dir, isbn)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            ChapterCandidate(
                title=c["title"], authors=tuple(c["authors"]),
                printed_page_number=c["printed_page_number"], pdf_page_index=c["pdf_page_index"],
                chapter_doi=c["chapter_doi"], source=c["source"], metadata_confidence=c["metadata_confidence"],
            )
            for c in data["chapters"]
        ]
    except Exception:
        logger.warning("_load_cache: corrupted cache file for ISBN %s, treating as miss", isbn, exc_info=True)
        return None


def _save_cache(cache_dir: Optional[Path], isbn: str, candidates: list[ChapterCandidate]) -> None:
    if cache_dir is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "isbn": isbn,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "chapters": [
            {
                "title": c.title, "authors": list(c.authors),
                "printed_page_number": c.printed_page_number, "pdf_page_index": c.pdf_page_index,
                "chapter_doi": c.chapter_doi, "source": c.source,
                "metadata_confidence": c.metadata_confidence,
            }
            for c in candidates
        ],
    }
    _cache_path(cache_dir, isbn).write_text(json.dumps(payload), encoding="utf-8")


def _parse_crossref_item(item: dict) -> Optional[ChapterCandidate]:
    if item.get("type") != "book-chapter":
        return None
    titles = item.get("title") or []
    if not titles:
        return None
    authors = tuple(
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in item.get("author", []) if a.get("family")
    )
    page = item.get("page")
    printed_page_number = _first_page_number(page) if page else None
    return ChapterCandidate(
        title=titles[0], authors=authors, printed_page_number=printed_page_number,
        chapter_doi=item.get("DOI"), source="crossref",
    )


async def fetch_crossref_chapters(
    isbn: str,
    http_client: httpx.AsyncClient,
    cache_dir: Optional[Path],
    contact_email: Optional[str],
) -> list[ChapterCandidate]:
    """GET .../works?filter=isbn:{isbn}, keeping only type=="book-chapter"
    records. Cached on disk by ISBN (including empty results, so a
    known-unregistered book is never re-queried on repeat runs). Any
    network, HTTP-status, or JSON-shape failure is logged and treated as
    an empty (uncached) result -- never raises.
    """
    cached = _load_cache(cache_dir, isbn)
    if cached is not None:
        return cached

    params: dict[str, str | int] = {
        "filter": f"isbn:{isbn}",
        "select": "DOI,title,author,page,type,container-title",
        "rows": 100,
    }
    if contact_email:
        params["mailto"] = contact_email

    response = None
    for _attempt in range(_MAX_RETRIES):
        try:
            response = await http_client.get(_CROSSREF_BASE_URL, params=params, timeout=10.0)
        except httpx.HTTPError:
            logger.warning("fetch_crossref_chapters: network error for ISBN %s", isbn, exc_info=True)
            return []
        if response.status_code != 429:
            break
        retry_after = response.headers.get("Retry-After") if response.headers else None
        delay = float(retry_after) if retry_after and retry_after.isdigit() else _DEFAULT_RETRY_DELAY_SECONDS
        await asyncio.sleep(delay)
    else:
        logger.warning("fetch_crossref_chapters: exhausted retries (429) for ISBN %s", isbn)
        return []

    try:
        response.raise_for_status()
        data = response.json()
        items = data["message"]["items"]
    except Exception:
        logger.warning("fetch_crossref_chapters: bad response for ISBN %s", isbn, exc_info=True)
        return []

    candidates = [c for item in items if (c := _parse_crossref_item(item)) is not None]
    _save_cache(cache_dir, isbn, candidates)
    return candidates


class CrossrefMetadataStrategy:
    def __init__(self, http_client: httpx.AsyncClient, cache_dir: Optional[Path], contact_email: Optional[str]):
        self._http_client = http_client
        self._cache_dir = cache_dir
        self._contact_email = contact_email

    def applicable(self, context: BookContext) -> bool:
        return context.isbn is not None

    async def fetch(self, context: BookContext) -> list[ChapterCandidate]:
        if context.isbn is None:
            return []
        return await fetch_crossref_chapters(context.isbn, self._http_client, self._cache_dir, self._contact_email)
