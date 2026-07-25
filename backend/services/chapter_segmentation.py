"""Heuristic chapter-boundary detection for script 1 (analyze_book_chapters).

Critical invariant (design spec §5): PDF physical page index and printed
page number are NEVER assumed equal. Page index comes from
extract_page_texts_from_pdf_bytes (pypdf, or a cached per-page OCR result —
see chapter_ocr.py), which is inherently index-based (list position = PDF
page index). Printed page numbers are only ever obtained by reading text
that actually appears on a page (TOC entries, header/footer numerals) —
never assumed from position.
"""

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

# Matches "<title> <dots-or-spaces> <page number>" — a classic TOC line.
# Requires at least 2 separator characters (dots or spaces) so ordinary
# prose sentences ending in a number don't false-positive.
_TOC_LINE_RE = re.compile(r"^(?P<title>.{3,120}?)[.\s]{2,}(?P<page>\d{1,4})\s*$")


@dataclass(frozen=True)
class TocEntry:
    title: str
    printed_page_number: int
    source_page_index: int  # which page (0-based) the TOC entry itself was found on


def extract_page_texts_from_pdf_bytes(content: bytes) -> list[str]:
    """Return one text string per physical PDF page, in index order (index 0
    = first page). Uses pypdf directly rather than Kreuzberg's chunking,
    which does not guarantee a clean 1:1 page<->chunk mapping.
    """
    reader = PdfReader(io.BytesIO(content))
    return [page.extract_text() or "" for page in reader.pages]


def find_toc_candidates(pages: list[str], max_front_fraction: float = 0.15, max_back_fraction: float = 0.05) -> list[TocEntry]:
    """Scan the front ~max_front_fraction and back ~max_back_fraction of
    pages for TOC-style lines ("<title> ... <printed page number>").

    Returns entries in the order found; each entry's `printed_page_number`
    is a target to later locate by content search (see Task 6) — never an
    index to jump to directly.
    """
    total = len(pages)
    if total == 0:
        return []
    front_count = max(1, int(total * max_front_fraction))
    back_count = max(1, int(total * max_back_fraction))
    scan_indices = list(range(min(front_count, total))) + list(range(max(0, total - back_count), total))
    scan_indices = sorted(set(scan_indices))

    entries: list[TocEntry] = []
    for page_index in scan_indices:
        for line in pages[page_index].splitlines():
            m = _TOC_LINE_RE.match(line.strip())
            if not m:
                continue
            title = m.group("title").strip(" .")
            if len(title) < 3:
                continue
            entries.append(
                TocEntry(
                    title=title,
                    printed_page_number=int(m.group("page")),
                    source_page_index=page_index,
                )
            )
    return entries
