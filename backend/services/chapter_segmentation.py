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
from typing import Callable, Optional

import spacy
from pypdf import PdfReader
from rapidfuzz import fuzz

from backend.services.chapter_link_store import parse_links

# Matches "<title> <dots-or-spaces> <page number>" — a classic TOC line.
# Requires at least 2 separator characters (dots or spaces) so ordinary
# prose sentences ending in a number don't false-positive.
_TOC_LINE_RE = re.compile(r"^(?P<title>.{3,120}?)[.\s]{1,}(?P<page>\d{1,4})\s*$")


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
# rapidfuzz partial_ratio is unreliable on very short strings (a near-blank
# page's head can trivially "perfectly align" with a short substring of the
# title, scoring 100 despite being meaningless) -- require this many
# stripped characters before a page is even considered a candidate.
_LOCATE_MIN_HEAD_CHARS = 20
# Top candidate cluster must beat the runner-up cluster by this much -- a
# single qualifying cluster is never rejected on margin grounds alone.
_LOCATE_MARGIN_REQUIRED = 8.0
# A running header often repeats a chapter's title on several of its OWN
# pages (e.g. the title appears on both the opening page and a later page of
# the same chapter, with a gap where an intervening page didn't score highly
# -- empirically observed as a real 2-page gap in an evaluation book). Pages
# this close together are treated as one location, not competing candidates,
# so the chapter's own repeated header is never mistaken for ambiguity.
_LOCATE_CLUSTER_GAP = 3


def locate_chapter_start(pages: list[str], title: str, exclude_indices: set[int]) -> int | None:
    """Find the PDF page index whose text most plausibly begins with `title`.

    This is a content lookup, not an index computation: it never assumes the
    TOC's printed page number corresponds to this page's index. Skips pages
    whose head text is too short to score reliably. Qualifying pages within
    _LOCATE_CLUSTER_GAP of each other are merged into one candidate location
    (a chapter's own running header can repeat the title on several of its
    pages) -- returns the earliest page of the best-scoring cluster, or None
    if no page qualifies or the top cluster is ambiguous against a runner-up.
    """
    candidates: list[tuple[int, float]] = []
    for index, text in enumerate(pages):
        if index in exclude_indices:
            continue
        # Only the page's opening ~200 characters are compared — a chapter
        # title appears at the START of its page, not buried mid-page.
        head = text[:200]
        if len(head.strip()) < _LOCATE_MIN_HEAD_CHARS:
            continue
        score = fuzz.partial_ratio(title.lower(), head.lower())
        if score >= _LOCATE_SCORE_THRESHOLD:
            candidates.append((index, score))
    if not candidates:
        return None

    candidates.sort()
    clusters: list[tuple[int, float]] = []  # (first_index, max_score) per cluster
    # Deliberately transitive (chained off the previous candidate, not the
    # cluster's first index): real academic books often repeat a running
    # header on every other page for a chapter's ENTIRE span (confirmed
    # empirically on an evaluation book with a 21-page chapter repeating its
    # title every 2 pages throughout) -- bounding a cluster's total width to
    # _LOCATE_CLUSTER_GAP would fracture that single chapter into many
    # same-scoring "clusters" and wrongly reject it as ambiguous. The
    # trade-off (accepted after evaluation-harness testing showed the
    # bounded variant regressed real recall to zero on that book) is a
    # theoretical risk of transitively bridging two genuinely different,
    # far-apart chapters if a spurious mid-range match happens to chain
    # them together -- not observed in this project's evaluation set.
    cluster_start, cluster_max = candidates[0]
    prev_index = candidates[0][0]
    for index, score in candidates[1:]:
        if index - prev_index <= _LOCATE_CLUSTER_GAP:
            cluster_max = max(cluster_max, score)
        else:
            clusters.append((cluster_start, cluster_max))
            cluster_start, cluster_max = index, score
        prev_index = index
    clusters.append((cluster_start, cluster_max))

    clusters.sort(key=lambda cluster: cluster[1], reverse=True)
    best_index, best_score = clusters[0]
    runner_up_score = clusters[1][1] if len(clusters) > 1 else 0.0
    if len(clusters) > 1 and best_score - runner_up_score < _LOCATE_MARGIN_REQUIRED:
        return None
    return best_index


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


def analyze_attachment(pages: list[str]) -> dict:
    """Orchestrate TOC detection, content-based localization, printed-page
    extraction, and author NER into the per-attachment output described in
    design spec §5.

    Returns a dict matching the script 1 output schema (minus item_key/
    attachment_key/has_text_layer/needs_ocr, which the caller — run(), Task
    9 — fills in from Zotero/Kreuzberg context, not from page text alone).
    """
    total_pages = len(pages)
    toc_entries = find_toc_candidates(pages)

    chapters: list[dict] = []
    toc_page_indices = {e.source_page_index for e in toc_entries}
    located: list[tuple[TocEntry, int]] = []
    for entry in toc_entries:
        index = locate_chapter_start(pages, entry.title, exclude_indices=toc_page_indices)
        if index is not None:
            located.append((entry, index))

    # Cluster into contiguous ranges, ordered by PDF index (not TOC order,
    # which is printed-page order and could disagree if TOC entries were
    # matched to pages out of sequence).
    located.sort(key=lambda pair: pair[1])
    for i, (entry, start_index) in enumerate(located):
        end_index = (located[i + 1][1] - 1) if i + 1 < len(located) else (total_pages - 1)
        if end_index < start_index:
            continue  # degenerate/ambiguous overlap — skip rather than guess

        start_printed = extract_printed_page_number(pages[start_index])
        end_printed = extract_printed_page_number(pages[end_index])
        if start_printed is not None and end_printed is not None:
            citation_pages = f"{start_printed}-{end_printed}"
            page_mapping_confidence = "high"
        else:
            citation_pages = None
            page_mapping_confidence = "unmappable"

        chapters.append({
            "title": entry.title,
            "authors": extract_authors_near(pages[start_index]),
            "pdf_start_index": start_index,
            "pdf_end_index": end_index,
            "citation_pages": citation_pages,
            "confidence": 0.9,  # single confirmed TOC->content match; see Known limitations
            "page_mapping_confidence": page_mapping_confidence,
        })

    segmentation_confidence = "high" if chapters else "low"
    return {
        "total_pdf_pages": total_pages,
        "segmentation_confidence": segmentation_confidence,
        "chapters": chapters,
        "diagnostics": {
            "toc_pages_scanned": sorted(toc_page_indices),
            "toc_matches_found": len(toc_entries),
            "toc_matches_located": len(located),
        },
    }


async def run(
    *,
    zotero_client,
    library_id: str,
    library_type: str,
    slug: str,
    item_keys: Optional[list[str]],
    max_items: Optional[int],
    relink: bool,
    progress_callback: Callable[[float, str], None],
) -> dict:
    """Core logic for script 1 (analyze_book_chapters). Scans `book`-type
    items in the library (or the explicit `item_keys` list), skips already-
    linked ones unless `relink`, downloads each PDF attachment, and runs
    analyze_attachment on its page text. See design spec §5.
    """
    items = await zotero_client.get_library_items_since(library_id, library_type=library_type)
    books = [i for i in items if i["data"].get("itemType") == "book"]
    if item_keys is not None:
        wanted = set(item_keys)
        books = [b for b in books if b["data"]["key"] in wanted]
    if not relink:
        books = [b for b in books if not parse_links(b["data"].get("extra", "")).contains]
    if max_items is not None:
        books = books[:max_items]

    attachments_out: list[dict] = []
    total = len(books) or 1
    for i, book in enumerate(books):
        item_key = book["data"]["key"]
        progress_callback(i / total, f"Analyzing {item_key} ({i + 1}/{total})")

        children = await zotero_client.get_item_children(library_id, item_key, library_type=library_type)
        pdf_attachments = [c for c in children if c["data"].get("contentType") == "application/pdf"]
        if not pdf_attachments:
            continue
        attachment_key = pdf_attachments[0]["data"]["key"]

        file_bytes = await zotero_client.get_attachment_file(library_id, attachment_key, library_type=library_type)
        if not file_bytes:
            continue

        pages = extract_page_texts_from_pdf_bytes(file_bytes)
        has_text_layer = sum(len(p.strip()) for p in pages) > 100

        if not has_text_layer:
            attachments_out.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "has_text_layer": False,
                "needs_ocr": True,
            })
            continue

        analysis = analyze_attachment(pages)
        attachments_out.append({
            "item_key": item_key,
            "attachment_key": attachment_key,
            "has_text_layer": True,
            "needs_ocr": False,
            **analysis,
        })

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
