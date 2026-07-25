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

# A real chapter TOC page has several title-like lines close together; a
# back-of-book subject index, a bibliography, or an ordinary content page
# can each contribute one or two incidental lines (a footnote reference, a
# citation, a running header) that happen to fit the same pattern without
# being a chapter listing at all. Requiring this many qualifying lines on
# the SAME page before trusting any of them mirrors the already-proven
# find_toc_pages logic in scripts/ground_truth_helper.py (built there for a
# different purpose -- excluding TOC pages from content search -- but the
# same page-density signal discriminates a real listing from scattered
# noise here too). Found empirically: on real evaluation books, ~60-70% of
# raw regex matches turned out to be isolated 1-2-line noise on unrelated
# pages, not genuine TOC entries.
_TOC_MIN_LINES_PER_PAGE = 3

# A printed page number more than this many times the PDF's total physical
# page count is implausible for a real chapter -- almost always a
# publication year, a law/citation reference number, or similar noise
# picked up from imprint or citation text that happens to match the
# "<text> ... <number>" pattern. Never a real chapter's page number.
_TOC_MAX_PAGE_NUMBER_RATIO = 2.0

# A book has exactly one real table of contents, appearing once as a small
# number of (near-)contiguous pages -- gap tolerance for grouping qualifying
# pages (see _TOC_MIN_LINES_PER_PAGE) into that one listing. Only the FIRST
# such cluster in page order is trusted; any later one is discarded, since
# it's far more likely to be a chapter's own internal sub-outline or a
# back-of-book index/bibliography (both structurally resemble a listing --
# many short lines each ending in a number -- but are not the book's table
# of contents). Found empirically: a legal-studies book had four qualifying
# pages beyond its real 1-page front TOC, all internal chapter sub-outlines,
# whose sub-heading entries (numbered/lettered "II.", "B.", "5.") corrupted
# top-level chapter boundary detection when treated as competing chapters;
# another book's real 2-page front TOC was joined by a 4-page, much denser
# back-of-book index that would otherwise dominate with pure noise.
_TOC_PAGE_CLUSTER_GAP = 2


def _looks_like_url_or_doi(line: str) -> bool:
    """A chapter title is never a URL or DOI -- these show up in imprint/
    copyright front-matter text and can otherwise fuzzy-match the TOC-line
    pattern (a run of digits at the end looks like a "title ... page
    number" line). Deliberately does NOT use a generic dot-count heuristic
    (unlike a similar check in scripts/ground_truth_helper.py, written for
    different input) -- a real TOC dot-leader line ("Title ..... 12") also
    has several dots, so that would reject genuine entries too.
    """
    lowered = line.lower()
    return "doi.org" in lowered or "http://" in lowered or "https://" in lowered or "www." in lowered


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

    A page is only trusted as a real chapter listing when at least
    _TOC_MIN_LINES_PER_PAGE of its lines structurally match the TOC-line
    pattern at all (see comment on the constant) -- this is a purely
    structural/typographic signal (does this page look like a listing?),
    checked BEFORE any content filtering, so one bad line doesn't disqualify
    an otherwise-genuine listing page. Among qualifying pages, only the
    FIRST (near-)contiguous cluster is used (see _TOC_PAGE_CLUSTER_GAP) --
    a book has one real table of contents, not several. Within that
    cluster, individual lines that look like a URL/DOI, or whose captured
    number is an implausible page number for this PDF's actual length, are
    still excluded from the final result.

    Returns entries in the order found; each entry's `printed_page_number`
    is a target to later locate by content search (see Task 6) — never an
    index to jump to directly.
    """
    total = len(pages)
    if total == 0:
        return []
    front_count = max(1, int(total * max_front_fraction))
    back_count = max(1, int(total * max_back_fraction))
    scan_indices = sorted(set(range(min(front_count, total))) | set(range(max(0, total - back_count), total)))
    max_plausible_page_number = total * _TOC_MAX_PAGE_NUMBER_RATIO

    raw_matches_by_page: dict[int, list[re.Match]] = {}
    for page_index in scan_indices:
        raw_matches = [
            m for line in pages[page_index].splitlines()
            if (m := _TOC_LINE_RE.match(line.strip())) is not None
        ]
        if len(raw_matches) >= _TOC_MIN_LINES_PER_PAGE:
            raw_matches_by_page[page_index] = raw_matches

    qualifying_pages = sorted(raw_matches_by_page)
    first_cluster: list[int] = []
    for page_index in qualifying_pages:
        if first_cluster and page_index - first_cluster[-1] > _TOC_PAGE_CLUSTER_GAP:
            break
        first_cluster.append(page_index)

    entries: list[TocEntry] = []
    for page_index in first_cluster:
        for m in raw_matches_by_page[page_index]:
            stripped_line = m.group(0)
            if _looks_like_url_or_doi(stripped_line):
                continue
            title = m.group("title").strip(" .")
            if len(title) < 3:
                continue
            page_number = int(m.group("page"))
            if page_number > max_plausible_page_number:
                continue
            entries.append(TocEntry(title=title, printed_page_number=page_number, source_page_index=page_index))
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


@dataclass(frozen=True)
class ChapterStartMatch:
    """Result of a successful locate_chapter_start lookup, carrying the raw
    signal needed to derive a real per-chapter confidence (see
    match_confidence) rather than a flat constant.
    """

    index: int
    score: float  # winning cluster's best rapidfuzz partial_ratio, 0-100
    margin: float  # score minus the runner-up cluster's score; equals score
    # itself when there was no competing cluster at all (uncontested match)


def locate_chapter_start(pages: list[str], title: str, exclude_indices: set[int]) -> ChapterStartMatch | None:
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
    margin = best_score - runner_up_score
    if len(clusters) > 1 and margin < _LOCATE_MARGIN_REQUIRED:
        return None
    return ChapterStartMatch(index=best_index, score=best_score, margin=margin)


# How much extra margin (beyond the minimum _LOCATE_MARGIN_REQUIRED) counts
# as "fully certain" for confidence purposes -- chosen so an uncontested
# match (margin == score, since runner_up defaults to 0.0) with a decent raw
# score reaches the certainty ceiling comfortably, while a match that only
# barely cleared the ambiguity guard against a real rival does not.
_CONFIDENCE_MARGIN_SATURATION = 20.0
# A match that reached this point already cleared every locate_chapter_start
# guard (min length, score threshold, ambiguity margin) -- confidence never
# drops below this floor, it only scales how much *better* than bare-minimum
# the match was. Note the bare-minimum case (score=80, margin=8, the
# smallest a contested match can have) actually computes to ~0.62, not this
# floor itself -- the floor is a hard lower bound, not a value ever hit in
# practice, since a contested match's margin can never go below
# _LOCATE_MARGIN_REQUIRED and an uncontested match's margin equals its score
# (>=80, already at the saturation ceiling).
_CONFIDENCE_FLOOR = 0.5


def match_confidence(score: float, margin: float) -> float:
    """Blend match quality (how well the text matched) and match certainty
    (how clearly it beat any rival candidate) into the single float
    downstream consumers (chapter_upload's confidence_threshold gate) use to
    triage proposed chapters -- replaces a previous flat 0.9 constant that
    could not distinguish a clean, uncontested match from one that barely
    survived the ambiguity guard against a real rival.

    Score contributes 40% of the ceiling above the floor, margin 60% --
    weighted toward margin because an ambiguous match with a strong rival is
    a bigger real-world risk (found empirically: several evaluation-book
    misdetections were high-scoring matches with a close runner-up) than a
    merely mediocre but uncontested score.
    """
    score_component = (score - _LOCATE_SCORE_THRESHOLD) / (100.0 - _LOCATE_SCORE_THRESHOLD)
    margin_component = min(margin / _CONFIDENCE_MARGIN_SATURATION, 1.0)
    return round(_CONFIDENCE_FLOOR + (1.0 - _CONFIDENCE_FLOOR) * (0.4 * score_component + 0.6 * margin_component), 2)


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


# A trailing page with fewer than this many stripped characters is treated
# as a blank/divider page (e.g. forcing the next chapter onto a recto page)
# rather than real chapter content -- same threshold and rationale as
# scripts/ground_truth_helper.py's build_draft, which needed the identical
# trim when constructing ground truth by hand from real books.
_TRAILING_BLANK_PAGE_MAX_CHARS = 150


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
    located: list[tuple[TocEntry, ChapterStartMatch]] = []
    for entry in toc_entries:
        match = locate_chapter_start(pages, entry.title, exclude_indices=toc_page_indices)
        if match is not None:
            located.append((entry, match))

    # Cluster into contiguous ranges, ordered by PDF index (not TOC order,
    # which is printed-page order and could disagree if TOC entries were
    # matched to pages out of sequence).
    located.sort(key=lambda pair: pair[1].index)
    for i, (entry, match) in enumerate(located):
        start_index = match.index
        end_index = (located[i + 1][1].index - 1) if i + 1 < len(located) else (total_pages - 1)
        if end_index < start_index:
            continue  # degenerate/ambiguous overlap — skip rather than guess

        # Back off past trailing blank/divider pages (e.g. a blank page
        # forcing the next chapter to start on a recto page) -- these
        # belong to neither neighboring chapter. Mirrors the proven
        # build_draft logic in scripts/ground_truth_helper.py, which needed
        # the identical fix when building ground truth by hand.
        while end_index > start_index and len(pages[end_index].strip()) < _TRAILING_BLANK_PAGE_MAX_CHARS:
            end_index -= 1

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
            "confidence": match_confidence(match.score, match.margin),
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
