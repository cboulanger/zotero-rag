"""Segment a book PDF into per-chapter files and upload them as new
bookSection items. See design spec §8.
"""

import io
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from backend.services.chapter_link_store import (
    add_related_item,
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
    zotero_item_uri,
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

    The book's own creators become the chapter's "editor" creators. Zotero
    has no standalone "editor" item field (a real API call rejects one with
    "Invalid property 'editor'") -- "editor" is only valid as a creatorType
    entry inside the shared "creators" array, alongside the chapter's own
    "author" entries.
    """
    item = dict(template)
    item["title"] = chapter["title"]
    item["bookTitle"] = book_data.get("title", "")
    item["publisher"] = book_data.get("publisher", "")
    item["place"] = book_data.get("place", "")
    item["date"] = book_data.get("date", "")
    item["ISBN"] = book_data.get("ISBN", "")
    item["language"] = book_data.get("language", "")
    item["pages"] = chapter.get("citation_pages") or ""
    authors = [
        {"creatorType": "author", "firstName": first, "lastName": last}
        for first, last in (_split_name(name) for name in chapter.get("authors", []))
    ]
    editors = [
        {**{k: v for k, v in creator.items() if k != "creatorType"}, "creatorType": "editor"}
        for creator in book_data.get("creators", [])
    ]
    item["creators"] = authors + editors
    return item


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

        # Download the book's PDF attachment ONCE per book, not once per
        # chapter -- this used to sit inside the per-chapter loop below and
        # re-downloaded the same (often large) file for every confident
        # chapter detected in the same book. A download failure is deferred
        # and raised inside the loop so it's still reported per-chapter,
        # isolated the same way any other per-chapter failure already is.
        book_file_bytes: bytes | None = None
        book_download_error: str | None = None
        if confident_chapters:
            try:
                book_file_bytes = await zotero_read_client.get_attachment_file(
                    library_id, attachment_key, library_type=library_type
                ) if hasattr(zotero_read_client, "get_attachment_file") else None
            except Exception as exc:  # noqa: BLE001 - reported per chapter below
                book_download_error = str(exc)

        for chapter in confident_chapters:
            # Isolate per-chapter failures so one corrupt PDF / API error does
            # not abort the whole batch (matches chapter_retrofit.run()).
            try:
                if book_download_error is not None:
                    raise RuntimeError(book_download_error)
                sliced = slice_pdf_range(book_file_bytes or b"", chapter["pdf_start_index"], chapter["pdf_end_index"])

                template = zotero_write_client.item_template("bookSection")
                item_data = build_book_section_item_data(template, book_data, chapter)
                resp = zotero_write_client.create_items([item_data])
                created_item = list(resp["successful"].values())[0]
                chapter_key = created_item["key"]

                tmp_path = Path(tempfile.gettempdir()) / f"{chapter_key}.pdf"
                try:
                    tmp_path.write_bytes(sliced)
                    # NOTE: attachment_simple() sends str(path) verbatim as
                    # the Zotero "filename" field, which the API rejects
                    # ("cannot contain a directory path") for any non-bare
                    # filename -- and tempfile.gettempdir() is always an
                    # absolute path. Create the attachment item with just the
                    # bare filename, then upload the actual bytes via
                    # upload_attachments(basedir=...), which resolves the
                    # local file path separately from the server-side field.
                    attachment_template = zotero_write_client.item_template("attachment", linkmode="imported_file")
                    attachment_template["title"] = tmp_path.name
                    attachment_template["filename"] = tmp_path.name
                    attachment_template["contentType"] = "application/pdf"
                    attachment_template["parentItem"] = chapter_key
                    attach_resp = zotero_write_client.create_items([attachment_template])
                    attachment_item = list(attach_resp["successful"].values())[0]
                    upload_resp = zotero_write_client.upload_attachments(
                        [{"key": attachment_item["key"], "filename": tmp_path.name}],
                        basedir=str(tmp_path.parent),
                    )
                    if upload_resp["failure"]:
                        raise RuntimeError(f"attachment upload failed: {upload_resp['failure']}")
                finally:
                    tmp_path.unlink(missing_ok=True)

                # Write the chapter side (X-Contained-By) from the CHAPTER's own
                # extra — never the book's extra.
                chapter_item = created_item
                chapter_id = format_chapter_id(slug, chapter_key)
                book_id = format_chapter_id(slug, book_key)
                chapter_extra = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
                chapter_item["data"]["extra"] = chapter_extra
                # Also set a native Zotero "relations" (dc:relation) connection
                # -- the "Related" tab in the Zotero client -- independent of
                # the Extra-field convention above and never read back by this
                # pipeline's own logic (see chapter_link_store.add_related_item).
                chapter_item["data"]["relations"] = add_related_item(
                    chapter_item["data"].get("relations", {}), zotero_item_uri(slug, book_key)
                )

                # Resolve the per-book target subcollection and fold membership
                # into the SAME update_item PATCH as the extra-field write.
                # pyzotero's update_item and addto_collection are both item
                # PATCHes that bump the server-side version and both derive
                # their If-Unmodified-Since-Version header from
                # payload["version"]; update_item returns the raw response
                # without refreshing the local dict's cached version, so
                # issuing them as two separate calls makes the second send a
                # now-stale version and 412 against real pyzotero. Reordering
                # does not help (both calls bump the version); one combined
                # PATCH sidesteps the staleness entirely.
                label = author_year_label(
                    [c.get("lastName", "") for c in book_data.get("creators", [])],
                    book_data.get("date", ""),
                )
                _, sub_key = ensure_target_collection(zotero_write_client, target_collection, label)
                existing_collections = chapter_item["data"].get("collections", [])
                if sub_key not in existing_collections:
                    chapter_item["data"]["collections"] = [*existing_collections, sub_key]
                zotero_write_client.update_item(chapter_item)

                new_chapter_ids.append(chapter_id)
                pdf_ranges[chapter_id] = (chapter["pdf_start_index"], chapter["pdf_end_index"])

                created.append({"book_key": book_key, "chapter_key": chapter_key})
            except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
                failed.append({"book_key": book_key, "title": chapter.get("title", ""), "error": str(exc)})
                continue

        if new_chapter_ids:
            # Re-fetch the book right before this PATCH: each chapter's own
            # update_item() call above (setting chapter->book in `relations`)
            # made Zotero's API auto-mirror the reverse book->chapter relation
            # onto the book server-side, bumping its version -- the `book_item`
            # fetched at the top of this loop is now stale, and a PATCH built
            # from it would 412. Re-fetching also means the book's `relations`
            # here already reflects every chapter's auto-mirrored link, so no
            # manual book-side relations write is needed at all.
            book_item = zotero_write_client.item(book_key)
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
