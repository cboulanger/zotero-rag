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

import spacy
from pypdf import PdfReader
from rapidfuzz import fuzz

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


_PAGE_NUMBER_TOKEN_RE = re.compile(r"^[0-9]{1,4}$|^[ivxlcdm]{1,7}$", re.IGNORECASE)
_LOCATE_SCORE_THRESHOLD = 80.0  # rapidfuzz partial_ratio, 0-100


def locate_chapter_start(pages: list[str], title: str, exclude_indices: set[int]) -> int | None:
    """Find the PDF page index whose text most plausibly begins with `title`.

    This is a content lookup, not an index computation: it never assumes the
    TOC's printed page number corresponds to this page's index. Returns the
    best-scoring page index at/above the match threshold, or None if no page
    scores highly enough.
    """
    best_index: int | None = None
    best_score = 0.0
    for index, text in enumerate(pages):
        if index in exclude_indices:
            continue
        # Only the page's opening ~200 characters are compared — a chapter
        # title appears at the START of its page, not buried mid-page.
        head = text[:200]
        score = fuzz.partial_ratio(title.lower(), head.lower())
        if score > best_score:
            best_score = score
            best_index = index
    if best_score >= _LOCATE_SCORE_THRESHOLD:
        return best_index
    return None


def extract_printed_page_number(page_text: str) -> str | None:
    """Read the printed page number actually shown on a page, by looking for
    an isolated numeral/roman-numeral line near the top or bottom of the
    page's text (a running header/footer). Returns None if no such line is
    found — callers must treat this as "unknown", never guess.
    """
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    if not lines:
        return None
    candidates = lines[:2] + lines[-2:]
    for line in candidates:
        if _PAGE_NUMBER_TOKEN_RE.match(line):
            return line
    return None


_NLP = None


def _get_nlp():
    global _NLP
    if _NLP is None:
        _NLP = spacy.load("en_core_web_sm")
    return _NLP


def extract_authors_near(page_text: str, max_chars: int = 500) -> list[str]:
    """Run spaCy NER on the opening text of a chapter-start page to extract
    candidate author names. Best-effort: returns an empty list rather than
    raising when nothing plausible is found.

    Known limitation: the small spaCy model (en_core_web_sm) can misclassify
    bare acronyms (e.g. "MLA", "APA") as PERSON entities -- this is a
    best-effort heuristic, not a guarantee.
    """
    doc = _get_nlp()(page_text[:max_chars])
    seen: list[str] = []
    for ent in doc.ents:
        if ent.label_ == "PERSON" and ent.text not in seen:
            seen.append(ent.text)
    return seen
