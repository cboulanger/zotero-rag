"""Retrofit-link existing, separately-catalogued book/bookSection pairs.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 7. Matching is deliberately biased toward precision: an ambiguous
or below-threshold match is reported for manual review rather than linked.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz

from chapter_segmentation.common import year_from_date

from backend.config.settings import get_settings
from backend.services import review_queue_store
from backend.services.chapter_link_store import (
    add_related_item,
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_links,
    write_links,
    zotero_item_uri,
)

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


def find_matches(all_items: list[dict], item_keys: list[str] | None, max_items: int | None) -> dict:
    """Pure matching logic: given an already-fetched full-library item list
    (see run()'s zotero_write_client.everything(...) call — the caller's
    job, not this function's), finds book/chapter matches. Returns
    {"would_link": [...], "ambiguous": [...], "no_match": [...]} — the same
    three buckets run()'s dry-run output already has (run() adds the
    "linked"/"failed" keys around this). Never touches Zotero and never
    writes; see commit_links for that half of script 3 (design spec §7).
    """
    books = [i for i in all_items if i["data"].get("itemType") == "book"]
    chapters = [i for i in all_items if i["data"].get("itemType") == "bookSection"]

    unlinked_chapters = [c for c in chapters if not parse_links(c["data"].get("extra", "")).contained_by]
    if item_keys is not None:
        wanted = set(item_keys)
        unlinked_chapters = [c for c in unlinked_chapters if c["data"]["key"] in wanted]
    if max_items is not None:
        unlinked_chapters = unlinked_chapters[:max_items]

    book_candidates = [
        {"key": b["data"]["key"], "title": b["data"].get("title", ""), "year": year_from_date(b["data"].get("date"))}
        for b in books
    ]

    would_link: list[dict] = []
    ambiguous: list[dict] = []
    no_match: list[str] = []

    for chapter in unlinked_chapters:
        chapter_key = chapter["data"]["key"]
        book_title = chapter["data"].get("bookTitle", "")
        year = year_from_date(chapter["data"].get("date"))

        if not book_title or not book_candidates:
            no_match.append(chapter_key)
            continue

        match = find_best_book_match(book_title, year, book_candidates)
        if match is None:
            ambiguous.append({"chapter_key": chapter_key, "candidates": book_candidates})
            continue

        would_link.append({"chapter_key": chapter_key, "book_key": match.book_key, "score": match.score})

    return {"would_link": would_link, "ambiguous": ambiguous, "no_match": no_match}


def commit_links(
    zotero_write_client, slug: str, would_link: list[dict], target_collection: str | None = None,
) -> dict:
    """Writes X-Contains/X-Contained-By links for an already-computed
    would_link list (find_matches()'s output, or a prior dry run's saved
    JSON replayed via the CLI's --input / the API's would_link field).
    Re-fetches only the two specific items involved in EACH link -- never
    the whole library -- which is what makes replaying a prior dry run's
    matches fast (design spec §7's write ordering/self-healing behavior is
    unchanged: book side written first, so a chapter-write failure after a
    successful book write just re-writes a no-op X-Contains on retry).

    If `target_collection` is given, the CHAPTER side of each written link
    is additionally filed into a `<target_collection>/<Author (Year)>`
    subcollection (created if absent, reused otherwise), mirroring
    chapter_upload.py's own collection-filing scheme -- the book's own
    collection membership is left untouched. Off by default: a retrofit
    run links items the caller has already organized themselves, so
    moving them into a new collection structure is opt-in only.
    """
    linked: list[dict] = []
    failed: list[dict] = []

    for entry in would_link:
        try:
            chapter_key = entry["chapter_key"]
            book_key = entry["book_key"]
            score = entry["score"]

            book_item = zotero_write_client.item(book_key)
            chapter_item = zotero_write_client.item(chapter_key)

            existing_links = parse_links(book_item["data"].get("extra", ""))
            chapter_id = format_chapter_id(slug, chapter_key)
            book_id = format_chapter_id(slug, book_key)
            new_contains = list(dict.fromkeys([*existing_links.contains, chapter_id]))
            book_item["data"]["extra"] = write_links(book_item["data"].get("extra", ""), contains=new_contains)
            book_item["data"]["relations"] = add_related_item(
                book_item["data"].get("relations", {}), zotero_item_uri(slug, chapter_key)
            )
            zotero_write_client.update_item(book_item)

            # Re-fetch the chapter right before its own PATCH: the book's
            # update_item() call above (setting book->chapter in `relations`)
            # made Zotero's API auto-mirror the reverse chapter->book relation
            # onto the chapter server-side, bumping its version -- the
            # `chapter_item` fetched at the top of this iteration is now
            # stale, and a PATCH built from it would 412. Re-fetching also
            # means `chapter_item["data"]["relations"]` here already reflects
            # the auto-mirrored link, so no manual chapter-side relations
            # write is needed at all.
            chapter_item = zotero_write_client.item(chapter_key)
            chapter_item["data"]["extra"] = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
            if target_collection is not None:
                label = author_year_label(
                    [c.get("lastName", "") for c in book_item["data"].get("creators", [])],
                    book_item["data"].get("date", ""),
                )
                _, sub_key = ensure_target_collection(zotero_write_client, target_collection, label)
                existing_collections = chapter_item["data"].get("collections", [])
                if sub_key not in existing_collections:
                    chapter_item["data"]["collections"] = [*existing_collections, sub_key]
            zotero_write_client.update_item(chapter_item)
        except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
            failed.append({
                "chapter_key": entry.get("chapter_key", "?"),
                "book_key": entry.get("book_key", "?"),
                "error": str(exc),
            })
            continue

        linked.append({"chapter_key": chapter_key, "book_key": book_key, "score": score})

    return {"linked": linked, "failed": failed}


def run(
    *,
    zotero_write_client,
    slug: str,
    item_keys: list[str] | None,
    max_items: int | None,
    commit: bool = False,
    would_link: list[dict] | None = None,
    target_collection: str | None = None,
) -> dict:
    """Core logic for script 3 (retrofit_chapter_links). Synchronous --
    pyzotero's client is itself synchronous. Defaults to dry-run -- `commit`
    must be explicitly True to write the `X-Contains`/`X-Contained-By`
    links to Zotero (mirrors chapter_upload.py's script 4 convention). See
    design spec §7.

    If `would_link` is given (e.g. a prior dry run's output, replayed via
    the CLI's --input flag or the API's `would_link` request field) AND
    commit=True, this skips the full-library fetch and matching pass
    entirely and goes straight to commit_links() -- this is what makes a
    commit run after a dry run fast: the full-library fetch is what
    dominates a fresh run's cost, not the fuzzy matching itself.
    `would_link` is ignored when commit=False; a dry run always matches
    fresh (there is nothing to preview if it just replayed a prior
    preview).

    Note `would_link=[]` (an empty list) still counts as "given" here --
    it takes the same fast path as a non-empty list, short-circuiting to
    an all-empty no-op result via commit_links(..., []) rather than
    falling back to a fresh full match. This is intentional: an empty
    would_link legitimately means "a prior dry run already determined
    there's nothing to link" and replaying that is correct. Only
    would_link=None triggers a fresh match.

    `target_collection`, when given, is passed straight through to
    commit_links() -- see its docstring for the opt-in collection-filing
    behavior. Has no effect when commit=False (a dry run never writes
    anything, including collection membership).
    """
    if commit and would_link is not None:
        result = commit_links(zotero_write_client, slug, would_link, target_collection)
        return {**result, "would_link": [], "ambiguous": [], "no_match": []}

    all_items = zotero_write_client.everything(zotero_write_client.items())
    matches = find_matches(all_items, item_keys, max_items)

    if not commit:
        review_entries = [
            {"queue_id": f"match:{a['chapter_key']}", "type": "match", "bucket": "review",
             "payload": {"chapter_key": a["chapter_key"], "candidates": a["candidates"], "target_collection": target_collection}}
            for a in matches["ambiguous"]
        ]
        commit_entries = [
            {"queue_id": f"match:{m['chapter_key']}", "type": "match", "bucket": "commit",
             "payload": {"chapter_key": m["chapter_key"], "book_key": m["book_key"], "score": m["score"],
                         "target_collection": target_collection}}
            for m in matches["would_link"]
        ]
        if review_entries or commit_entries:
            review_queue_store.upsert_many(get_settings().review_queue_path, slug, review_entries + commit_entries)
        return {
            "linked": [], "would_link": matches["would_link"],
            "ambiguous": matches["ambiguous"], "no_match": matches["no_match"], "failed": [],
        }

    result = commit_links(zotero_write_client, slug, matches["would_link"], target_collection)
    return {**result, "would_link": [], "ambiguous": matches["ambiguous"], "no_match": matches["no_match"]}
