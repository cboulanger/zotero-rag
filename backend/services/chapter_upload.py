"""Segment a book PDF into per-chapter files and upload them as new
bookSection items. See design spec §8.
"""

import io

from pypdf import PdfReader, PdfWriter

from backend.db.vector_store import _extract_lastnames


def slice_pdf_range(content: bytes, pdf_start_index: int, pdf_end_index: int) -> bytes:
    """Return a standalone PDF (bytes) containing pages
    [pdf_start_index, pdf_end_index] (both inclusive, 0-based) of `content`.
    Always operates on the PDF-index pair — never on citation_pages, which
    is a different (printed-number) space (design spec §2/§8).
    """
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    for index in range(pdf_start_index, pdf_end_index + 1):
        writer.add_page(reader.pages[index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _split_name(full_name: str) -> tuple[str, str]:
    """Split "First Last" into (first, last); single-token names become
    (last-only)."""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def build_book_section_item_data(template: dict, book_data: dict, chapter: dict) -> dict:
    """Build a bookSection item's field values from a pyzotero item_template,
    inheriting bibliographic metadata from the book and using the chapter's
    own detected title/authors/citation_pages (design spec §8, step 2).

    Never derives `pages` from pdf_start_index/pdf_end_index — only from
    `citation_pages`, left blank when that's None (unmappable printed
    numbers) rather than guessed from a different number space.
    """
    item = dict(template)
    item["title"] = chapter["title"]
    item["bookTitle"] = book_data.get("title", "")
    item["editor"] = book_data.get("creators", [])
    item["publisher"] = book_data.get("publisher", "")
    item["place"] = book_data.get("place", "")
    item["date"] = book_data.get("date", "")
    item["ISBN"] = book_data.get("ISBN", "")
    item["language"] = book_data.get("language", "")
    item["pages"] = chapter.get("citation_pages") or ""
    item["creators"] = [
        {"creatorType": "author", "firstName": first, "lastName": last}
        for first, last in (_split_name(name) for name in chapter.get("authors", []))
    ]
    return item


def author_year_label(authors: list[str], date: str) -> str:
    """Build a short author-year label for a per-book subcollection name,
    e.g. "Miller (2023)" or "Smith et al. (1999)" (3+ authors). Reuses the
    existing lastname-extraction helper already used for Qdrant author
    filtering, for consistency with how author names are normalized
    elsewhere in this codebase.
    """
    lastnames = _extract_lastnames(authors)
    year = date.strip().split("-")[0] if date else "n.d."
    if not lastnames:
        return f"Unknown ({year})"
    first = lastnames[0].capitalize()
    label = first if len(lastnames) == 1 else f"{first} et al."
    return f"{label} ({year})"


def ensure_target_collection(zotero_write_client, top_level_name: str, subcollection_name: str) -> tuple[str, str]:
    """Find-or-create the top-level collection and its per-book
    subcollection, returning (top_level_key, subcollection_key). Idempotent:
    re-running against an already-processed book reuses both collections.
    """
    top_matches = [c for c in zotero_write_client.collections() if c["data"]["name"] == top_level_name]
    if top_matches:
        top_key = top_matches[0]["key"]
    else:
        resp = zotero_write_client.create_collection([{"name": top_level_name}])
        top_key = list(resp["successful"].values())[0]["key"]

    sub_matches = [c for c in zotero_write_client.collections_sub(top_key) if c["data"]["name"] == subcollection_name]
    if sub_matches:
        sub_key = sub_matches[0]["key"]
    else:
        resp = zotero_write_client.create_collection([{"name": subcollection_name, "parentCollection": top_key}])
        sub_key = list(resp["successful"].values())[0]["key"]

    return top_key, sub_key
