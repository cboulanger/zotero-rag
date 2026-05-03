"""
Utility for splitting large PDF byte payloads into smaller parts.

Used by the document processor to avoid OOM-killing the kreuzberg sidecar
when processing very large scanned PDFs.
"""

from io import BytesIO

import pypdf


def split_pdf_bytes(
    pdf_bytes: bytes,
    target_part_bytes: int,
) -> list[tuple[bytes, int]]:
    """
    Split PDF bytes into parts each approximately `target_part_bytes` in size.

    Pages per part is derived from the average bytes-per-page of the original
    file, so parts are sized by content weight rather than page count.  This is
    important for scanned PDFs where a small number of high-resolution pages
    can dominate the file size.

    Args:
        pdf_bytes: Raw bytes of the source PDF.
        target_part_bytes: Desired byte size of each output part.

    Returns:
        List of (part_bytes, page_offset) tuples where page_offset is the
        0-based index of the first page of that part within the original
        document.  A single-element list is returned when the PDF fits in one
        part (i.e. splitting is a no-op).

    Raises:
        ValueError: If the PDF cannot be parsed or has no pages.
    """
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    if total_pages == 0:
        raise ValueError("PDF has no pages")

    bytes_per_page = len(pdf_bytes) / total_pages
    pages_per_part = max(1, int(target_part_bytes / bytes_per_page))

    parts: list[tuple[bytes, int]] = []
    for start in range(0, total_pages, pages_per_part):
        end = min(start + pages_per_part, total_pages)
        writer = pypdf.PdfWriter()
        for page_idx in range(start, end):
            writer.add_page(reader.pages[page_idx])
        buf = BytesIO()
        writer.write(buf)
        parts.append((buf.getvalue(), start))

    return parts
