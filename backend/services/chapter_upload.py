"""Segment a book PDF into per-chapter files and upload them as new
bookSection items. See design spec §8.
"""

import io
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from backend.db.vector_store import _extract_lastnames
from backend.services.chapter_link_store import (
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
)


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


async def run(
    *,
    zotero_write_client,
    zotero_read_client,
    slug: str,
    analyses: list[dict],
    commit: bool,
    confidence_threshold: float,
    target_collection: str,
    max_items: int | None,
) -> dict:
    """Core logic for script 4 (upload_chapters). `analyses` is script 1's
    output list (one entry per book attachment). Defaults to dry-run —
    `commit` must be explicitly True to write to Zotero. See design spec §8.
    """
    # Derive the real library type / backend-format id once from the slug —
    # a Zotero item's `data` dict never carries a "library_id" key (library
    # info lives in the API envelope's separate top-level "library" key), and
    # get_attachment_file needs the backend-format id + real type, matching
    # how chapter_segmentation.run() calls it.
    library_type, _numeric_id, library_id = parse_library_slug(slug)

    analyses = analyses[:max_items] if max_items is not None else analyses

    would_create: list[dict] = []
    created: list[dict] = []
    skipped_low_confidence: list[dict] = []
    failed: list[dict] = []

    for analysis in analyses:
        book_key = analysis["item_key"]
        attachment_key = analysis.get("attachment_key", "")
        book_item = zotero_write_client.item(book_key)
        book_data = book_item["data"]

        confident_chapters = [c for c in analysis.get("chapters", []) if c["confidence"] >= confidence_threshold]
        low_confidence = [c for c in analysis.get("chapters", []) if c["confidence"] < confidence_threshold]
        skipped_low_confidence.extend({"book_key": book_key, "title": c["title"]} for c in low_confidence)

        if not commit:
            would_create.extend({"book_key": book_key, "title": c["title"], "pdf_start_index": c["pdf_start_index"],
                                  "pdf_end_index": c["pdf_end_index"]} for c in confident_chapters)
            continue

        new_chapter_ids: list[str] = []
        pdf_ranges: dict[str, tuple[int, int]] = {}
        for chapter in confident_chapters:
            # Isolate per-chapter failures so one corrupt PDF / API error does
            # not abort the whole batch (matches chapter_retrofit.run()).
            try:
                # Fetch the BOOK's PDF ATTACHMENT (not the book item itself) —
                # analysis["attachment_key"] is the attachment key detected by
                # script 1.
                file_bytes = await zotero_read_client.get_attachment_file(
                    library_id, attachment_key, library_type=library_type
                ) if hasattr(zotero_read_client, "get_attachment_file") else None
                sliced = slice_pdf_range(file_bytes or b"", chapter["pdf_start_index"], chapter["pdf_end_index"])

                template = zotero_write_client.item_template("bookSection")
                item_data = build_book_section_item_data(template, book_data, chapter)
                resp = zotero_write_client.create_items([item_data])
                chapter_key = list(resp["successful"].values())[0]["key"]

                tmp_path = Path(tempfile.gettempdir()) / f"{chapter_key}.pdf"
                tmp_path.write_bytes(sliced)
                zotero_write_client.attachment_simple([str(tmp_path)], parentid=chapter_key)
                tmp_path.unlink(missing_ok=True)

                # Write the chapter side (X-Contained-By) from the CHAPTER's own
                # freshly-fetched extra — never the book's extra.
                chapter_item = zotero_write_client.item(chapter_key)
                chapter_id = format_chapter_id(slug, chapter_key)
                book_id = format_chapter_id(slug, book_key)
                chapter_extra = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
                chapter_item["data"]["extra"] = chapter_extra
                zotero_write_client.update_item(chapter_item)

                new_chapter_ids.append(chapter_id)
                pdf_ranges[chapter_id] = (chapter["pdf_start_index"], chapter["pdf_end_index"])

                label = author_year_label(
                    book_data.get("creators_names", [])
                    or [c.get("lastName", "") for c in book_data.get("creators", [])],
                    book_data.get("date", ""),
                )
                _, sub_key = ensure_target_collection(zotero_write_client, target_collection, label)
                zotero_write_client.addto_collection(sub_key, chapter_item)

                created.append({"book_key": book_key, "chapter_key": chapter_key})
            except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
                failed.append({"book_key": book_key, "title": chapter.get("title", ""), "error": str(exc)})
                continue

        if new_chapter_ids:
            existing = parse_links(book_item["data"].get("extra", ""))
            merged_contains = list(dict.fromkeys([*existing.contains, *new_chapter_ids]))
            merged_ranges = {**existing.pdf_ranges, **pdf_ranges}
            book_item["data"]["extra"] = write_links(
                book_item["data"].get("extra", ""), contains=merged_contains, pdf_ranges=merged_ranges
            )
            zotero_write_client.update_item(book_item)

    return {
        "would_create": would_create,
        "created": created,
        "skipped_low_confidence": skipped_low_confidence,
        "failed": failed,
    }
