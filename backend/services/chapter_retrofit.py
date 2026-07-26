"""Retrofit-link existing, separately-catalogued book/bookSection pairs.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 7. Matching is deliberately biased toward precision: an ambiguous
or below-threshold match is reported for manual review rather than linked.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz

from backend.services.chapter_link_store import format_chapter_id, parse_links, write_links

_SCORE_THRESHOLD = 90.0  # rapidfuzz token_sort_ratio, 0-100
# NOTE: _MARGIN_REQUIRED increased from 5.0 to 11.0 to catch ambiguous cases
# where multiple candidates have similar titles (e.g., "Title" vs "Title Vol 2").
# With token_sort_ratio, these produce scores like 100 vs 89.3, margin ~10.7.
# A margin requirement of 11.0 ensures such close-but-different titles are
# flagged as ambiguous for manual review, maintaining the "precision-biased"
# matching philosophy stated in the module docstring.
_MARGIN_REQUIRED = 11.0   # top candidate must beat the runner-up by this much
_YEAR_TOLERANCE = 1


@dataclass(frozen=True)
class BookMatch:
    book_key: str
    score: float  # 0-1, normalized from rapidfuzz's 0-100 scale


def find_best_book_match(book_title: str, year: int | None, candidate_books: list[dict]) -> BookMatch | None:
    """Return the best-matching book (by title similarity + year tolerance)
    among `candidate_books` (each a dict with "key", "title", "year"), or
    None if no candidate clears both the score threshold and the margin
    requirement over the runner-up.
    """
    scored: list[tuple[str, float]] = []
    for book in candidate_books:
        if year is not None and book.get("year") is not None:
            if abs(book["year"] - year) > _YEAR_TOLERANCE:
                continue
        score = fuzz.token_sort_ratio(book_title.lower(), book["title"].lower())
        scored.append((book["key"], score))

    if not scored:
        return None

    scored.sort(key=lambda pair: pair[1], reverse=True)
    top_key, top_score = scored[0]
    if top_score < _SCORE_THRESHOLD:
        return None
    if len(scored) > 1:
        _, runner_up_score = scored[1]
        if top_score - runner_up_score < _MARGIN_REQUIRED:
            return None
    return BookMatch(book_key=top_key, score=top_score / 100.0)


_RANGE_SCORE_THRESHOLD = 70.0


def locate_chapter_pdf_range(chapter_text: str, book_pages: list[str]) -> tuple[int, int] | None:
    """Find the contiguous PDF page-index span within `book_pages` whose
    concatenated text best matches `chapter_text` (the chapter item's own
    extracted attachment text).

    Unlike script 4 (which knows the range by construction, having just
    sliced it), this links two pre-existing items with no given page
    relationship — the chapter's own attachment may even be a different
    scan of the same content. Returns None (never a guessed range) when no
    contiguous span scores confidently — design spec §7's safe-degradation
    rule: the identity link is still written by the caller, suppression
    just doesn't apply to that pair.
    """
    chapter_head = chapter_text[:300].lower()
    best_start: int | None = None
    best_score = 0.0
    for start in range(len(book_pages)):
        window_text = book_pages[start][:300].lower()
        score = fuzz.partial_ratio(chapter_head, window_text)
        if score > best_score:
            best_score = score
            best_start = start

    if best_start is None or best_score < _RANGE_SCORE_THRESHOLD:
        return None

    # Extend forward while subsequent pages still plausibly belong to this
    # chapter's content (their text appears within the chapter's own text).
    end = best_start
    chapter_lower = chapter_text.lower()
    for index in range(best_start + 1, len(book_pages)):
        page_head = book_pages[index][:100].lower().strip()
        if page_head and fuzz.partial_ratio(page_head, chapter_lower) >= _RANGE_SCORE_THRESHOLD:
            end = index
        else:
            break
    return (best_start, end)


def _year_from_date(date_str: str | None) -> int | None:
    if not date_str:
        return None
    for token in date_str.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None


def run(
    *,
    zotero_write_client,
    slug: str,
    item_keys: list[str] | None,
    max_items: int | None,
    commit: bool = False,
) -> dict:
    """Core logic for script 3 (retrofit_chapter_links). Synchronous —
    pyzotero's client is itself synchronous. Defaults to dry-run — `commit`
    must be explicitly True to write the `X-Contains`/`X-Contained-By`
    links to Zotero (mirrors chapter_upload.py's script 4 convention). See
    design spec §7.
    """
    # zot.items() alone returns only the first page — everything() is
    # required to auto-paginate through the full library.
    all_items = zotero_write_client.everything(zotero_write_client.items())
    books = [i for i in all_items if i["data"].get("itemType") == "book"]
    chapters = [i for i in all_items if i["data"].get("itemType") == "bookSection"]

    unlinked_chapters = [c for c in chapters if not parse_links(c["data"].get("extra", "")).contained_by]
    if item_keys is not None:
        wanted = set(item_keys)
        unlinked_chapters = [c for c in unlinked_chapters if c["data"]["key"] in wanted]
    if max_items is not None:
        unlinked_chapters = unlinked_chapters[:max_items]

    book_candidates = [
        {"key": b["data"]["key"], "title": b["data"].get("title", ""), "year": _year_from_date(b["data"].get("date"))}
        for b in books
    ]

    linked: list[dict] = []
    would_link: list[dict] = []
    ambiguous: list[dict] = []
    no_match: list[str] = []
    failed: list[dict] = []

    for chapter in unlinked_chapters:
        chapter_key = chapter["data"]["key"]
        book_title = chapter["data"].get("bookTitle", "")
        year = _year_from_date(chapter["data"].get("date"))

        if not book_title or not book_candidates:
            no_match.append(chapter_key)
            continue

        match = find_best_book_match(book_title, year, book_candidates)
        if match is None:
            ambiguous.append({"chapter_key": chapter_key, "candidates": book_candidates})
            continue

        if not commit:
            would_link.append({"chapter_key": chapter_key, "book_key": match.book_key, "score": match.score})
            continue

        book_item = zotero_write_client.item(match.book_key)
        chapter_id = format_chapter_id(slug, chapter_key)
        book_id = format_chapter_id(slug, match.book_key)

        try:
            # Write the book side (X-Contains) FIRST. If this fails, the
            # chapter is left untouched and still shows up as "unlinked" on
            # the next run. If it succeeds but the chapter write below then
            # fails, the next run will re-match this chapter and re-write
            # X-Contains — a no-op thanks to the deterministic ordering
            # below — and simply retry the chapter write. This makes the
            # two-write sequence self-healing instead of leaving a
            # permanent one-sided link.
            existing_links = parse_links(book_item["data"].get("extra", ""))
            new_contains = list(dict.fromkeys([*existing_links.contains, chapter_id]))
            book_item["data"]["extra"] = write_links(book_item["data"].get("extra", ""), contains=new_contains)
            zotero_write_client.update_item(book_item)

            chapter["data"]["extra"] = write_links(chapter["data"].get("extra", ""), contained_by=book_id)
            zotero_write_client.update_item(chapter)
        except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
            failed.append({"chapter_key": chapter_key, "book_key": match.book_key, "error": str(exc)})
            continue

        linked.append({"chapter_key": chapter_key, "book_key": match.book_key, "score": match.score})

    return {"linked": linked, "would_link": would_link, "ambiguous": ambiguous, "no_match": no_match, "failed": failed}
