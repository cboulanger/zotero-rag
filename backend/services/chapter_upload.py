"""Segment a book PDF into per-chapter files and upload them as new
bookSection items. See design spec §8.
"""

import io

from pypdf import PdfReader, PdfWriter


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
