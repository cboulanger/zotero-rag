"""
Utility for splitting large PDF byte payloads into smaller parts.

Used by the document processor to avoid OOM-killing the kreuzberg sidecar
when processing very large scanned PDFs.
"""

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import pikepdf

# Text-showing operators; a page containing one has a real (or invisible OCR) text layer.
_TEXT_OP = re.compile(rb"(?<![A-Za-z0-9])(?:Tj|TJ)(?![A-Za-z0-9])")
_TEXT_SAMPLE_PAGES = 20


@dataclass(frozen=True)
class PdfProfile:
    """Cheap structural facts about a PDF (no OCR, no Kreuzberg round-trip)."""

    page_count: int
    has_text_layer: bool


def _page_has_text(page: pikepdf.Page) -> bool:
    contents = page.obj.get("/Contents")
    if contents is None:
        return False
    streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
    return any(_TEXT_OP.search(stream.read_bytes()) for stream in streams)


def inspect_pdf(pdf_bytes: bytes) -> PdfProfile:
    """Count pages and detect a text layer by sampling up to 20 evenly spread pages.

    Raises:
        ValueError: If the PDF cannot be parsed.
    """
    try:
        with pikepdf.open(BytesIO(pdf_bytes)) as pdf:
            total = len(pdf.pages)
            if total == 0:
                return PdfProfile(0, False)
            step = max(1, total // _TEXT_SAMPLE_PAGES)
            has_text = any(_page_has_text(pdf.pages[i]) for i in range(0, total, step))
            return PdfProfile(total, has_text)
    except pikepdf.PdfError as e:
        raise ValueError(f"Cannot parse PDF: {e}") from e


def split_pdf_bytes(
    pdf_bytes: bytes,
    target_part_bytes: int,
    max_pages_per_part: Optional[int] = None,
) -> list[tuple[bytes, int]]:
    """
    Split PDF bytes into parts each approximately `target_part_bytes` in size.

    Pages per part is derived from the average bytes-per-page of the original
    file, so parts are sized by content weight rather than page count.  This is
    important for scanned PDFs where a small number of high-resolution pages
    can dominate the file size.

    Uses pikepdf (not pypdf) because pikepdf copies page streams without
    decompressing them and only carries over the indirect objects actually
    referenced by each page.  pypdf re-embeds the full document object graph
    in every part, making each split part nearly as large as the original.

    Args:
        pdf_bytes: Raw bytes of the source PDF.
        target_part_bytes: Desired byte size of each output part.
        max_pages_per_part: Optional page cap per part, applied on top of the
            byte target (OCR memory cost scales with pages, not bytes).

    Returns:
        List of (part_bytes, page_offset) tuples where page_offset is the
        0-based index of the first page of that part within the original
        document.  A single-element list is returned when the PDF fits in one
        part (i.e. splitting is a no-op).

    Raises:
        ValueError: If the PDF cannot be parsed or has no pages.
    """
    with pikepdf.open(BytesIO(pdf_bytes)) as src:
        total_pages = len(src.pages)
        if total_pages == 0:
            raise ValueError("PDF has no pages")

        bytes_per_page = len(pdf_bytes) / total_pages
        pages_per_part = max(1, int(target_part_bytes / bytes_per_page))
        if max_pages_per_part:
            pages_per_part = min(pages_per_part, max_pages_per_part)

        parts: list[tuple[bytes, int]] = []
        for start in range(0, total_pages, pages_per_part):
            end = min(start + pages_per_part, total_pages)
            dst = pikepdf.Pdf.new()
            dst.pages.extend(src.pages[start:end])
            buf = BytesIO()
            dst.save(buf)
            dst.close()
            parts.append((buf.getvalue(), start))

    return parts
