"""Heuristic chapter-boundary detection for script 1 (analyze_book_chapters).

Critical invariant (design spec §5): PDF physical page index and printed
page number are NEVER assumed equal. Page index comes from
extract_page_texts_from_pdf_bytes (pypdf, or a cached per-page OCR result —
see chapter_ocr.py), which is inherently index-based (list position = PDF
page index). Printed page numbers are only ever obtained by reading text
that actually appears on a page (TOC entries, header/footer numerals) —
never assumed from position.
"""

import hashlib
import io
import json
import logging
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import spacy
from pypdf import PdfReader
from rapidfuzz import fuzz

from backend.config.settings import get_settings
from backend.services import review_queue_store
from backend.services.chapter_link_store import parse_links
from backend.services.chapter_ocr import load_cached_ocr
from backend.services.llm import LLMService
from backend.utils.llm_json import parse_json_array, parse_json_object

logger = logging.getLogger(__name__)


def _parses_as_json_array(raw: str) -> bool:
    """is_valid check for LLMService.generate() (see AutoSelectLLMService):
    lets model-rotation retry a different candidate when a model returns
    something other than the requested JSON array (e.g. a hallucinated
    tool-call instead of JSON), not just on an outright exception."""
    try:
        parse_json_array(raw)
        return True
    except Exception:
        return False


def _parses_as_json_object(raw: str) -> bool:
    """Same as _parses_as_json_array, for the JSON-object-shaped prompts."""
    try:
        parse_json_object(raw)
        return True
    except Exception:
        return False


# Matches "<title> <dots-or-spaces> <page number>" — a classic TOC line.
# Requires at least 2 separator characters (dots or spaces) so ordinary
# prose sentences ending in a number don't false-positive. The page may
# also be a lowercase roman numeral ("Foreword vii") -- front-matter
# sections are paginated that way; see _parse_toc_page_number for the
# strict validation that keeps ordinary words made of roman letters
# ("civil", "mix") from being read as page numbers.
_TOC_LINE_RE = re.compile(r"^(?P<title>.{3,120}?)[.\s]{1,}(?P<page>\d{1,4}|[ivxlcdm]{1,6}|[IVXLCDM]{1,6})\s*$")

# Strict roman-numeral shape -- "vii"/"XI" pass, letter-soup like "civil"
# (c-i-v-i-l, an ordinary word whose letters all happen to be roman digits)
# does not. All-lowercase or all-uppercase only (the regex alternatives
# above never capture a mixed-case run).
_STRICT_ROMAN_RE = re.compile(r"^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
# A roman-paginated TOC entry beyond this value is front matter no longer --
# real books' roman pagination covers a few dozen pages at most; anything
# larger is almost certainly a mis-read word.
_ROMAN_PAGE_MAX_VALUE = 50


def _parse_toc_page_number(raw: str) -> int | None:
    """The integer value of a TOC line's captured page field, or None if the
    field is not a plausible page number (an invalid or implausibly large
    roman numeral)."""
    if raw.isdigit():
        return int(raw)
    lowered = raw.lower()
    if not _STRICT_ROMAN_RE.match(lowered) or not lowered:
        return None
    total = 0
    for ch, nxt in zip(lowered, lowered[1:] + " "):
        value = _ROMAN_VALUES[ch]
        total += -value if nxt != " " and _ROMAN_VALUES.get(nxt, 0) > value else value
    return total if total <= _ROMAN_PAGE_MAX_VALUE else None

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

# How many preceding TOC-page lines may be merged into a wrapped entry's
# title. Real evaluation books wrap long titles over up to 4 physical lines
# (3 continuation lines + the final page-number line, e.g. Springer's
# "A Double Helix: The Intertwined History / of the Marginalisation ... /
# Activist Lawyers ... / of the Welfare State in England and Wales 83").
_TOC_MAX_CONTINUATION_LINES = 3

# Some TOCs (notably French/OpenEdition-style) attach each chapter's page
# number to an author line under the wrapped title ("MOTS ET CHIFFRES ... /
# par Mustapha Harzoune .... 16") while ALSO listing every sub-heading with
# its own page number. When several entries carry this author-line marker,
# it is the TOC's chapter-level convention: only those entries are chapters,
# everything else on the listing is a sub-heading. "et <Name>" continues a
# multi-author list ("par Stéphanie Alexandre / et Lucie Daudin .... 117").
_TOC_AUTHOR_MARKER_RE = re.compile(r"^(?:par|by|et)\s+(?P<names>[A-ZÀ-Þ].+)$")
_TOC_AUTHOR_MARKER_MIN_ENTRIES = 3


def _toc_scan_indices(pages: list[str], max_front_fraction: float = 0.15, max_back_fraction: float = 0.05) -> set[int]:
    """The front/back-matter page-index range both find_toc_candidates
    (regex) and llm_extract_toc_entries (LLM fallback design spec §4) scan
    for a table-of-contents listing."""
    total = len(pages)
    if total == 0:
        return set()
    front_count = max(1, int(total * max_front_fraction))
    back_count = max(1, int(total * max_back_fraction))
    return set(range(min(front_count, total))) | set(range(max(0, total - back_count), total))


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
    authors: tuple[str, ...] = ()  # populated only by llm_extract_toc_entries (see
    # docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md §4)
    # -- a regex-found TOC line has no author info, so heuristic-found entries always
    # leave this empty. Feeds locate_chapter_start's author-aware disambiguation (§5).
    printed_roman: bool = False  # True when the TOC listed this entry with a
    # roman-numeral page ("Foreword vii") -- i.e. it lives in the book's
    # roman-paginated FRONT matter, so the usual "chapters start after the
    # table of contents" exclusion must not apply to it.
    title_variants: tuple[str, ...] = ()  # longer alternative readings of `title`,
    # built by prepending the preceding non-matching TOC-page line(s): a long
    # title wrapped over several lines puts its page number on the LAST line
    # only, so the regex captures just that tail fragment ("Gaze", "in der
    # Krise") -- far too generic to locate reliably. Which reading is correct
    # can't be known from the TOC page alone (the preceding line may be a
    # previous entry's author line or a part header), so ALL of them are
    # carried and _locate_toc_entries picks whichever variant actually
    # locates best in the book body. Empty for LLM-extracted entries, whose
    # titles are already read whole.


def extract_page_texts_from_pdf_bytes(content: bytes) -> list[str]:
    """Return one text string per physical PDF page, in index order (index 0
    = first page). Uses pypdf directly rather than Kreuzberg's chunking,
    which does not guarantee a clean 1:1 page<->chunk mapping.
    """
    reader = PdfReader(io.BytesIO(content))
    return [page.extract_text() or "" for page in reader.pages]


def _analysis_cache_path(cache_dir: Path, item_key: str, attachment_key: str, version: int, mode: str) -> Path:
    return cache_dir / f"{item_key}-{attachment_key}-v{version}-{mode}.analysis.json"


def load_cached_analysis(cache_dir: Path, item_key: str, attachment_key: str, version: int, mode: str) -> Optional[dict]:
    """Cached run()-entry (has_text_layer/needs_ocr/chapters/diagnostics)
    for this exact attachment VERSION and analysis `mode` ("heuristic" or
    "llm_fallback"), or None on a cache miss. Keyed by the attachment's own
    Zotero version (not a content hash) so a hit never requires downloading
    the PDF at all -- unlike chapter_ocr.py's content-hash cache, which can
    only short-circuit AFTER the download since the hash needs the bytes.
    `mode` is part of the key so a heuristic-only cache entry is never
    served back when the caller now wants the LLM fallback applied (a
    heuristic pass that found zero chapters must not silently shadow a
    later --llm-fallback run against the same unchanged attachment).
    """
    path = _analysis_cache_path(cache_dir, item_key, attachment_key, version, mode)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_analysis_cache(cache_dir: Path, item_key: str, attachment_key: str, version: int, mode: str, entry: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _analysis_cache_path(cache_dir, item_key, attachment_key, version, mode)
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def find_toc_candidates(pages: list[str], max_front_fraction: float = 0.15, max_back_fraction: float = 0.05) -> list[TocEntry]:
    """Scan the front ~max_front_fraction and back ~max_back_fraction of
    pages for TOC-style lines ("<title> ... <printed page number>").

    A page is only trusted as a real chapter listing when at least
    _TOC_MIN_LINES_PER_PAGE of its lines match the TOC-line pattern AND
    survive the per-line content filters (URL/DOI lines, implausible page
    numbers, too-short titles). Filtering must happen before this count:
    an imprint/metadata page full of "DOI: ... 7527" / "Année d'édition:
    2017" lines structurally resembles a listing but contributes no valid
    entries -- counting its raw matches let such a page qualify as the
    book's "first TOC cluster" and shadow the real table of contents
    (found empirically on a French evaluation book). Among qualifying
    pages, only the FIRST (near-)contiguous cluster is used (see
    _TOC_PAGE_CLUSTER_GAP) -- a book has one real table of contents, not
    several.

    Returns entries in the order found; each entry's `printed_page_number`
    is a target to later locate by content search (see Task 6) — never an
    index to jump to directly.
    """
    total = len(pages)
    if total == 0:
        return []
    scan_indices = sorted(_toc_scan_indices(pages, max_front_fraction, max_back_fraction))
    max_plausible_page_number = total * _TOC_MAX_PAGE_NUMBER_RATIO

    def _valid_entries(page_index: int) -> list[TocEntry]:
        out: list[TocEntry] = []
        lines = [ln.strip() for ln in pages[page_index].splitlines()]
        for i, line in enumerate(lines):
            m = _TOC_LINE_RE.match(line)
            if m is None:
                continue
            if _looks_like_url_or_doi(m.group(0)):
                continue
            title = m.group("title").strip(" .")
            page_number = _parse_toc_page_number(m.group("page"))
            if page_number is None or page_number > max_plausible_page_number:
                continue
            if len(title) < 3:
                # A bare dot-leader line (".......... 60") carries only the
                # page number -- its entry's title sits entirely on the
                # preceding line(s) (seen in a real French TOC where the
                # wrapped title and author lines are each too long to share
                # the leader line). Adopt the preceding line as the title
                # so the continuation-merging below can extend it further.
                if i == 0 or not re.fullmatch(r"[.\s…·]*\d{1,4}", line):
                    continue
                prev = lines[i - 1]
                if not prev or _TOC_LINE_RE.match(prev) or _looks_like_url_or_doi(prev) or len(prev) > 120:
                    continue
                title = prev.strip(" .")
                if len(title) < 3:
                    continue
                i = i - 1  # continuation merging walks up from the adopted title line
            # A wrapped title puts its page number on the last line only, so
            # `title` may be just the tail fragment. Collect up to
            # _TOC_MAX_CONTINUATION_LINES preceding lines as progressively
            # longer alternative readings -- stopping at another TOC-style
            # line (the previous entry), a blank line, or a URL/DOI line.
            # Which reading is right is decided later, by which one actually
            # locates in the body (see _locate_toc_entries).
            variants: list[str] = []
            prefix_parts: list[str] = []
            for j in range(i - 1, max(i - 1 - _TOC_MAX_CONTINUATION_LINES, -1), -1):
                prev = lines[j]
                if not prev or _TOC_LINE_RE.match(prev) or _looks_like_url_or_doi(prev) or len(prev) > 120:
                    break
                if _PART_DIVIDER_RE.match(prev) or _normalized_title(prev) in _BACK_MATTER_TITLES:
                    # A part header ("Part I ...", "Teil 2: ...") or the
                    # listing's own heading ("CONTENTS", "Sommaire")
                    # separates entries; neither is ever the opening of the
                    # NEXT entry's wrapped title. Merging a part header in
                    # produced variants that located on the part-divider
                    # page itself and displaced the real chapter (seen on a
                    # real evaluation book).
                    break
                prefix_parts.insert(0, prev)
                variants.append(" ".join(prefix_parts + [title]).strip(" ."))
            out.append(TocEntry(
                title=title, printed_page_number=page_number, source_page_index=page_index,
                printed_roman=not m.group("page").isdigit(),
                title_variants=tuple(variants),
            ))
        return out

    entries_by_page: dict[int, list[TocEntry]] = {}
    for page_index in scan_indices:
        page_entries = _valid_entries(page_index)
        if len(page_entries) >= _TOC_MIN_LINES_PER_PAGE:
            entries_by_page[page_index] = page_entries

    first_cluster: list[int] = []
    for page_index in sorted(entries_by_page):
        if first_cluster and page_index - first_cluster[-1] > _TOC_PAGE_CLUSTER_GAP:
            break
        first_cluster.append(page_index)

    # A listing's LAST page often holds only its final one or two entries --
    # below the density threshold on its own, but a genuine continuation of
    # an already-trusted cluster (a real evaluation book's last two chapters
    # sat alone on the TOC's second page and were silently lost). Extend the
    # cluster forward through adjacent pages that still contribute at least
    # TWO valid entries -- one lone matching line is indistinguishable from
    # an ordinary body page's incidental "text ... number" line, and
    # swallowing the first content page as "TOC" costs a whole chapter
    # (observed on a real evaluation book).
    while first_cluster:
        extended = False
        for page_index in range(first_cluster[-1] + 1, first_cluster[-1] + 1 + _TOC_PAGE_CLUSTER_GAP):
            if page_index in entries_by_page or page_index >= total:
                continue
            trailing_entries = _valid_entries(page_index)
            if len(trailing_entries) >= 2:
                entries_by_page[page_index] = trailing_entries
                first_cluster.append(page_index)
                extended = True
                break
        if not extended:
            break

    entries = [entry for page_index in first_cluster for entry in entries_by_page[page_index]]

    # Author-line convention (see _TOC_AUTHOR_MARKER_RE): when several
    # entries are "par/by <Name>" lines, those mark the chapter-level
    # entries and every other line is a sub-heading -- keep only the
    # chapters, and read the author names straight off the marker line.
    marker_entries = []
    for entry in entries:
        # The marker may only become visible in a longer reading (e.g. the
        # base title is a wrapped author list's tail, "Szejnman", and the
        # variant restores "par Bénédicte Frocaut et Noémie Szejnman").
        m = next(
            (m for t in (entry.title, *entry.title_variants) if (m := _TOC_AUTHOR_MARKER_RE.match(t)) is not None),
            None,
        )
        if m is None:
            continue
        authors = tuple(
            name.strip() for name in re.split(r"\s+et\s+|\s+and\s+|,|\bpar\b", m.group("names")) if name.strip()
        )
        marker_entries.append(replace(entry, authors=authors))
    if len(marker_entries) >= _TOC_AUTHOR_MARKER_MIN_ENTRIES:
        return marker_entries
    return entries


_LLM_TOC_EXTRACTION_PROMPT = """\
You are reading the front and back matter of a scanned/extracted book to \
find its table of contents. Some layouts don't use simple dotted leaders \
(e.g. "Title ..... 12") -- read the text directly rather than pattern-matching.

{page_blocks}

Return ONLY a JSON array, one entry per real chapter -- skip \
acknowledgements, bibliography, index, and part-divider pages:
[{{"title": "...", "authors": ["First Last", ...], "printed_page_number": 12}}]

If a chapter's printed page number is not visible in this text, use null \
for printed_page_number. If authors are not identifiable, use an empty list."""


async def llm_extract_toc_entries(pages: list[str], llm_service: LLMService) -> list[TocEntry]:
    """Reads the same front/back-matter page range find_toc_candidates
    already scans (_toc_scan_indices), sends their raw text verbatim to the
    LLM, and asks it to return the book's chapter listing as it actually
    appears -- for layouts too irregular for the regex (no dot leaders,
    multi-column, unconventional spacing/punctuation) but readable by
    inspection. Never asks the LLM for a physical page index -- only
    title/authors/printed_page_number, exactly like a regex-found TocEntry,
    so the result flows into the SAME locate_chapter_start content-search
    step used for every other TOC entry (design spec §4).
    """
    scan_indices = sorted(_toc_scan_indices(pages))
    if not scan_indices:
        return []
    page_blocks = "\n\n".join(f"[PAGE {i}]\n{pages[i]}" for i in scan_indices)
    prompt = _LLM_TOC_EXTRACTION_PROMPT.format(page_blocks=page_blocks)

    try:
        raw = await llm_service.generate(prompt=prompt, max_tokens=1024, temperature=0.0, is_valid=_parses_as_json_array)
        items = parse_json_array(raw)
    except Exception:
        logger.warning("llm_extract_toc_entries: LLM call or JSON parse failed", exc_info=True)
        return []

    entries: list[TocEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if len(title) < 3:
            continue
        raw_authors = item.get("authors")
        # Guard against a malformed LLM response giving a plain string
        # instead of a list (e.g. "authors": "Jane Doe") -- iterating a
        # string yields one entry per character, silently corrupting
        # author-aware disambiguation downstream.
        authors = tuple(str(a).strip() for a in raw_authors if str(a).strip()) if isinstance(raw_authors, list) else ()
        printed = item.get("printed_page_number")
        # -1 is a sentinel for "unknown" (LLM returned null or an
        # unparseable value) -- never a real printed page number, and
        # currently unread by any downstream consumer (see TocEntry).
        printed_page_number = int(printed) if isinstance(printed, (int, float)) else -1
        # source_page_index is a sentinel here -- unlike a regex-found entry,
        # an LLM-extracted entry has no single "the TOC line was on this
        # page" origin; the orchestration layer excludes the whole scanned
        # front/back-matter range instead (see _toc_scan_indices).
        entries.append(TocEntry(title=title, printed_page_number=printed_page_number, source_page_index=-1, authors=authors))
    return entries


# Running-header detection: a line whose digit-stripped, whitespace-collapsed
# form recurs near the top of at least this fraction of the book's pages is a
# running header (book/series title, "NN | Publisher, Year ..."), not page
# content. Found empirically: one evaluation book prefixes EVERY page with a
# ~150-character two-line header, which consumed locate_chapter_start's whole
# head window so no chapter title was ever visible to the matcher.
_RUNNING_HEADER_MIN_FRACTION = 0.2
_RUNNING_HEADER_MIN_PAGES = 5
_RUNNING_HEADER_SCAN_LINES = 5  # only lines this close to the top can qualify
_RUNNING_HEADER_MIN_NORM_CHARS = 8  # ignore short/near-empty lines (bare page numbers)


def _normalize_header_line(line: str) -> str:
    """Digits removed (page numbers vary per page), whitespace collapsed,
    lowercased -- so the same running header is recognized on every page."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", line)).strip().lower()


@lru_cache(maxsize=8)
def _running_header_lines(pages_key: tuple[str, ...]) -> frozenset[str]:
    """The set of normalized line forms that qualify as running headers for
    this book. Cached on the pages tuple -- every locate call for the same
    book reuses one computation."""
    counts: Counter[str] = Counter()
    for text in pages_key:
        seen_on_page: set[str] = set()
        for line in text.splitlines()[:_RUNNING_HEADER_SCAN_LINES]:
            norm = _normalize_header_line(line)
            if len(norm) >= _RUNNING_HEADER_MIN_NORM_CHARS and norm not in seen_on_page:
                seen_on_page.add(norm)
                counts[norm] += 1
    threshold = max(_RUNNING_HEADER_MIN_PAGES, int(len(pages_key) * _RUNNING_HEADER_MIN_FRACTION))
    return frozenset(norm for norm, count in counts.items() if count >= threshold)


def _strip_running_headers(text: str, header_lines: frozenset[str]) -> str:
    """Drop leading lines that are running headers (or bare page-number
    noise attached to them) so the returned text starts at real content."""
    if not header_lines:
        return text
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines[:_RUNNING_HEADER_SCAN_LINES]):
        norm = _normalize_header_line(line)
        if norm in header_lines or len(norm) == 0:
            start = i + 1
        else:
            break
    return "\n".join(lines[start:])


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
# An author's last name appearing near the top of a candidate page is a much
# stronger opening-page signal than title text alone (a running header can
# repeat the title on every page of a chapter, but rarely repeats the
# author's name on non-opening pages) -- see design spec §5. Chosen so a
# confirmed candidate reliably beats a same-scoring unconfirmed rival
# (15 > _LOCATE_MARGIN_REQUIRED's 8, with room to spare) without being large
# enough to override a rival that scores far higher on title match alone.
_AUTHOR_CONFIRMED_BONUS = 15.0


@dataclass(frozen=True)
class ChapterStartMatch:
    """Result of a successful locate_chapter_start lookup, carrying the raw
    signal needed to derive a real per-chapter confidence (see
    match_confidence) rather than a flat constant.
    """

    index: int
    score: float  # winning cluster's best rapidfuzz partial_ratio, 0-100
    margin: float  # _candidate_ranking_key(best) minus the runner-up cluster's
    # ranking key -- NOT plain score minus score. When either cluster is
    # author_confirmed, up to _AUTHOR_CONFIRMED_BONUS is folded into this
    # number, so a large margin doesn't necessarily mean a large raw title-
    # score gap. Equals _candidate_ranking_key(best) itself when there was no
    # competing cluster at all (uncontested match).
    author_confirmed: bool = False


@dataclass(frozen=True)
class ChapterStartCandidate:
    """One clustered candidate location from locate_chapter_start_candidates
    -- exposes what locate_chapter_start only used internally before, so an
    ambiguous result's competing clusters can be inspected (e.g. for LLM
    disambiguation, see llm_disambiguate_chapter_start)."""

    index: int
    score: float
    # True if any raw candidate merged into this cluster had an author's last
    # name near its head -- since a cluster can span several pages (see
    # _LOCATE_CLUSTER_GAP), this is an OR across the whole cluster, so the
    # specific page ultimately returned (the cluster's earliest index) is not
    # guaranteed to itself contain the author-name evidence. Acceptable: the
    # bonus only breaks ties/ambiguity between clusters, it doesn't affect the
    # stored score.
    author_confirmed: bool


def _candidate_ranking_key(candidate: ChapterStartCandidate) -> float:
    return candidate.score + (_AUTHOR_CONFIRMED_BONUS if candidate.author_confirmed else 0.0)


def locate_chapter_start_candidates(
    pages: list[str], title: str, exclude_indices: set[int], authors: tuple[str, ...] = (),
) -> list[ChapterStartCandidate]:
    """Find every plausible page location for `title`'s chapter opening,
    clustered exactly as locate_chapter_start does internally (see its
    docstring for the clustering rationale), sorted best-first (the
    author-confirmed bonus is already applied to the ranking). Returns an
    empty list when nothing clears the score threshold at all.

    When `authors` is non-empty, a candidate page is additionally checked
    for whether any given author's last name (lowercased) appears in the
    same head-of-page text the title is scored against -- mirrors the
    disambiguation approach scripts/ground_truth_helper.py already
    validated by hand while building this project's evaluation set.
    """
    last_names = tuple(a.split()[-1].lower() for a in authors if a.strip())
    header_lines = _running_header_lines(tuple(pages))
    raw_candidates: list[tuple[int, float, bool]] = []
    for index, text in enumerate(pages):
        if index in exclude_indices:
            continue
        # Only the page's opening ~200 characters are compared — a chapter
        # title appears at the START of its page, not buried mid-page.
        # Running headers are stripped first: a book that stamps every page
        # with a long header would otherwise fill this window with header
        # text and hide the actual title from the matcher.
        head = _strip_running_headers(text, header_lines)[:200]
        if len(head.strip()) < _LOCATE_MIN_HEAD_CHARS:
            continue
        head_lower = head.lower()
        score = fuzz.partial_ratio(title.lower(), head_lower)
        if score >= _LOCATE_SCORE_THRESHOLD:
            confirmed = bool(last_names) and any(name in head_lower for name in last_names)
            raw_candidates.append((index, score, confirmed))
    if not raw_candidates:
        return []

    raw_candidates.sort()
    clusters: list[ChapterStartCandidate] = []
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
    cluster_start, cluster_max, cluster_confirmed = raw_candidates[0]
    prev_index = raw_candidates[0][0]
    for index, score, confirmed in raw_candidates[1:]:
        if index - prev_index <= _LOCATE_CLUSTER_GAP:
            cluster_max = max(cluster_max, score)
            cluster_confirmed = cluster_confirmed or confirmed
        else:
            clusters.append(ChapterStartCandidate(index=cluster_start, score=cluster_max, author_confirmed=cluster_confirmed))
            cluster_start, cluster_max, cluster_confirmed = index, score, confirmed
        prev_index = index
    clusters.append(ChapterStartCandidate(index=cluster_start, score=cluster_max, author_confirmed=cluster_confirmed))

    clusters.sort(key=_candidate_ranking_key, reverse=True)
    return clusters


def locate_chapter_start(
    pages: list[str], title: str, exclude_indices: set[int], authors: tuple[str, ...] = (),
) -> ChapterStartMatch | None:
    """Find the PDF page index whose text most plausibly begins with `title`.

    This is a content lookup, not an index computation: it never assumes the
    TOC's printed page number corresponds to this page's index. Delegates
    candidate-gathering and clustering to locate_chapter_start_candidates,
    then applies the ambiguity guard: returns the earliest page of the
    best-scoring cluster, or None if no page qualifies or the top cluster
    doesn't beat the runner-up by _LOCATE_MARGIN_REQUIRED. Default
    `authors=()` reproduces the exact behavior before author-aware
    disambiguation was added -- see locate_chapter_start_candidates.
    """
    candidates = locate_chapter_start_candidates(pages, title, exclude_indices, authors)
    if not candidates:
        return None

    best = candidates[0]
    runner_up_key = _candidate_ranking_key(candidates[1]) if len(candidates) > 1 else 0.0
    margin = _candidate_ranking_key(best) - runner_up_key
    if len(candidates) > 1 and margin < _LOCATE_MARGIN_REQUIRED:
        return None
    return ChapterStartMatch(index=best.index, score=best.score, margin=margin, author_confirmed=best.author_confirmed)


_LLM_DISAMBIGUATION_PROMPT = """\
A chapter titled "{title}"{author_clause} could not be confidently located \
because more than one page in this book plausibly matches. Below are the \
competing candidate pages -- pick which one is the chapter's TRUE opening \
page (its title page, not a continuation page repeating the same running \
header).

{candidate_blocks}

Return ONLY JSON: {{"chosen_candidate": <candidate number>}} or \
{{"chosen_candidate": null}} if none of them are actually the right page."""


async def llm_disambiguate_chapter_start(
    pages: list[str],
    title: str,
    authors: tuple[str, ...],
    candidates: list[ChapterStartCandidate],
    llm_service: LLMService,
) -> ChapterStartMatch | None:
    """Shows the LLM each competing candidate's page index + a short
    snippet (page head, larger than the ~200-char window locate_chapter_start
    itself scores against, to give the LLM more context than the heuristic
    had) and asks it to pick which one is the chapter's true opening page,
    or none. A small, bounded prompt -- a handful of short snippets, never
    whole-book text -- since locate_chapter_start_candidates has already
    narrowed the field to the real contenders (design spec §6).
    """
    author_clause = f" by {', '.join(authors)}" if authors else ""
    candidate_blocks = "\n\n".join(
        f"[CANDIDATE {n}] page {c.index}:\n{pages[c.index][:300]}"
        for n, c in enumerate(candidates, start=1)
    )
    prompt = _LLM_DISAMBIGUATION_PROMPT.format(title=title, author_clause=author_clause, candidate_blocks=candidate_blocks)

    try:
        raw = await llm_service.generate(prompt=prompt, max_tokens=64, temperature=0.0, is_valid=_parses_as_json_object)
        data = parse_json_object(raw)
    except Exception:
        logger.warning("llm_disambiguate_chapter_start: LLM call or JSON parse failed", exc_info=True)
        return None

    chosen = data.get("chosen_candidate")
    if not isinstance(chosen, int) or not (1 <= chosen <= len(candidates)):
        return None
    winner = candidates[chosen - 1]
    # Deliberately conservative: margin is pinned at exactly the minimum
    # required to clear locate_chapter_start's own ambiguity guard, never
    # higher -- the LLM resolved WHICH candidate is right, it did not make
    # the underlying textual match itself any less genuinely contested
    # (design spec §6).
    return ChapterStartMatch(index=winner.index, score=winner.score, margin=_LOCATE_MARGIN_REQUIRED, author_confirmed=winner.author_confirmed)


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


# Entries that are structural markers, not chapters: part dividers
# ("Teil 1: ...", "Part II ...", "PARTIE I. ...") and standard back-matter
# lists (index, contributors, bibliography, ...). They are still LOCATED --
# a part divider bounds its neighboring chapters' page ranges -- but never
# emitted as chapters themselves. Forewords/prefaces/afterwords are NOT
# here: they carry real authored content and the evaluation ground truth
# counts them as chapters.
_PART_DIVIDER_RE = re.compile(
    r"^(?:teil|part|partie|section|abschnitt)\b[\s.:]*(?:[0-9]+|[ivxlcdm]+)?\b", re.IGNORECASE
)
_ROMAN_PREFIX_RE = re.compile(r"^[IVXLCDM]+\.\s")
_BACK_MATTER_TITLES = {
    "contributors", "notes on contributors", "about the authors", "about the contributors",
    "index", "indexes", "name index", "subject index",
    "register", "sachregister", "personenregister", "namensregister",
    "bibliography", "bibliographie", "literatur", "literaturverzeichnis", "references",
    "quellenverzeichnis", "acknowledgments", "acknowledgements", "danksagung", "remerciements",
    "glossary", "glossar", "glossaire", "abbreviations", "abkurzungsverzeichnis", "abkurzungen",
    "list of figures", "list of tables", "tabellenverzeichnis", "abbildungsverzeichnis",
    "les auteurs", "auteurs", "autorinnen und autoren", "die autorinnen und autoren",
    "verzeichnis der autorinnen und autoren", "zu den autorinnen und autoren",
    "liste des auteurs", "memento", "autorinnenverzeichnis", "autorenverzeichnis",
    # The table of contents can list ITSELF (and does, in a real evaluation
    # book: "Sommaire .... VII").
    "contents", "table of contents", "inhalt", "inhaltsverzeichnis", "inhaltsubersicht",
    "sommaire", "table des matieres",
}


def _normalized_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", stripped)).strip()


def _is_part_divider(title: str) -> bool:
    return _PART_DIVIDER_RE.match(title) is not None


def _is_back_matter(title: str) -> bool:
    normalized = _normalized_title(title)
    if normalized in _BACK_MATTER_TITLES:
        return True
    # Author-marker TOCs (see _TOC_AUTHOR_MARKER_RE) fold the author into
    # the entry title ("MÉMENTO par Lucie Daudin") -- test the part before
    # the marker too.
    stripped = re.split(r"\b(?:par|by)\b", normalized, maxsplit=1)[0].strip()
    return stripped in _BACK_MATTER_TITLES


# A trailing page with fewer than this many stripped characters is treated
# as a blank/divider page (e.g. forcing the next chapter onto a recto page)
# rather than real chapter content -- same threshold and rationale as
# scripts/ground_truth_helper.py's build_draft, which needed the identical
# trim when constructing ground truth by hand from real books.
_TRAILING_BLANK_PAGE_MAX_CHARS = 150


# A page qualifies as a secondary listing (see _secondary_listing_pages)
# when at least this many distinct TOC entries' titles appear on it at this
# fuzzy score. Long readings only -- short/generic fragments would match
# ordinary body pages and mark them as listings by accident.
_LISTING_PAGE_MIN_ENTRIES = 3
_LISTING_PAGE_SCORE_THRESHOLD = 90.0
_LISTING_PAGE_MIN_TITLE_CHARS = 15
_LISTING_PAGE_BODY_WINDOW = 2000


def _secondary_listing_pages(pages: list[str], toc_entries: list[TocEntry], exclude_indices: set[int]) -> set[int]:
    """Pages outside the TOC itself whose text contains several different
    TOC entries' titles: a part-divider page listing that part's chapters, a
    series/half-title page or back cover listing the whole book's
    contributions. Such a page fuzzy-matches many entries and either
    competes with the true opening page (ambiguity) or -- sitting directly
    before it, as part dividers do -- merges into the same candidate
    cluster and displaces the returned start index one page early (both
    observed on real evaluation books). Never a chapter opening itself, so
    exclude it from location entirely."""
    header_lines = _running_header_lines(tuple(pages))
    counts: Counter[int] = Counter()
    for entry in toc_entries:
        reading = max((entry.title, *entry.title_variants), key=len)
        if len(reading) < _LISTING_PAGE_MIN_TITLE_CHARS:
            continue
        reading_lower = reading.lower()
        for index, text in enumerate(pages):
            if index in exclude_indices:
                continue
            body = _strip_running_headers(text, header_lines)[:_LISTING_PAGE_BODY_WINDOW].lower()
            if len(body.strip()) < _LOCATE_MIN_HEAD_CHARS:
                continue
            if fuzz.partial_ratio(reading_lower, body) >= _LISTING_PAGE_SCORE_THRESHOLD:
                counts[index] += 1
    return {index for index, count in counts.items() if count >= _LISTING_PAGE_MIN_ENTRIES}


def _locate_toc_entries(
    pages: list[str], toc_entries: list[TocEntry], exclude_indices: set[int]
) -> tuple[list[tuple[TocEntry, ChapterStartMatch]], list[TocEntry], set[int]]:
    """Attempts locate_chapter_start for every entry. Returns (located pairs
    sorted by match index, entries that failed to locate at all -- either
    zero candidates cleared the score threshold, or a genuine unresolved
    ambiguity, and the full set of non-content page indices used as location
    excludes: the caller-provided excludes plus detected secondary listing
    pages -- _chapters_from_located trims these off chapter ends too).
    Passes each entry's own authors through (empty for regex-found entries)
    for author-aware disambiguation.

    Two structural constraints sharpen the raw per-entry lookup:

    - Chapters cannot start on or before the table of contents itself, so
      when the TOC sits in the front matter every page up to it is excluded
      -- this removes cover/half-title pages (which repeat the book title,
      and with it any chapter title that embeds the book title) from the
      candidate pool entirely.
    - TOC listing order is book order. An entry left ambiguous by the
      per-entry lookup is retried against only the candidates that fall
      strictly between its nearest successfully-located neighbors in TOC
      order -- an Introduction/Conclusion pair sharing one title suffix is
      unambiguous once each is pinned to its own side of the book. The
      margin is pinned to the bare minimum (same convention as
      llm_disambiguate_chapter_start): ordering resolved WHICH candidate,
      it did not make the textual match less contested.
    """
    exclude_indices = exclude_indices | _secondary_listing_pages(pages, toc_entries, exclude_indices)
    front_toc_pages = [e.source_page_index for e in toc_entries if 0 <= e.source_page_index < len(pages) // 2]
    pre_toc_pages = set(range(max(front_toc_pages) + 1)) if front_toc_pages else set()

    def _entry_excludes(entry: TocEntry) -> set[int]:
        # Roman-paginated entries (Foreword, Avant-propos, ...) live in the
        # front matter BEFORE the table of contents -- only they may locate
        # on pre-TOC pages.
        return exclude_indices if entry.printed_roman else exclude_indices | pre_toc_pages

    resolved: list[tuple[TocEntry, ChapterStartMatch] | None] = []
    for entry in toc_entries:
        # Try the entry's own (possibly tail-fragment) title plus every
        # longer wrapped-title reading (see TocEntry.title_variants), and
        # keep whichever locates most TRUSTWORTHILY -- ranked by
        # min(score, margin), i.e. how certain the localization is, not raw
        # score alone. A short tail fragment can score a perfect 100 on a
        # wrong page (a back-cover blurb containing the fragment) while
        # barely clearing the ambiguity margin; the full wrapped title
        # scoring 95 uncontested on the true opening page is far stronger
        # evidence (both cases observed on real evaluation books). Ties fall
        # back to score, then length -- the longer reading matched more
        # actual page evidence and reads better as a chapter title.
        best: tuple[tuple[float, float, int], str, ChapterStartMatch] | None = None
        for variant_title in (entry.title, *entry.title_variants):
            match = locate_chapter_start(pages, variant_title, exclude_indices=_entry_excludes(entry), authors=entry.authors)
            if match is None:
                continue
            score = match.score + (_AUTHOR_CONFIRMED_BONUS if match.author_confirmed else 0.0)
            key = (min(score, match.margin), score, len(variant_title))
            if best is None or key > best[0]:
                best = (key, variant_title, match)
        if best is not None:
            _, winning_title, match = best
            resolved.append((replace(entry, title=winning_title) if winning_title != entry.title else entry, match))
        else:
            resolved.append(None)

    # Second pass: ordering-based disambiguation for the entries the
    # independent lookup left ambiguous (see docstring).
    unlocated: list[TocEntry] = []
    for k, entry in enumerate(toc_entries):
        if resolved[k] is not None:
            continue
        lower = max((pair[1].index for pair in resolved[:k] if pair is not None), default=-1)
        upper = min((pair[1].index for pair in resolved[k + 1:] if pair is not None), default=len(pages))
        best_feasible: tuple[float, int, str, ChapterStartCandidate] | None = None
        for variant_title in (entry.title, *entry.title_variants):
            candidates = locate_chapter_start_candidates(pages, variant_title, exclude_indices=_entry_excludes(entry), authors=entry.authors)
            feasible = [c for c in candidates if lower < c.index < upper]
            if len(feasible) < 2 and len(feasible) == len(candidates):
                continue  # ordering added no information; the first pass already ruled
            for c in feasible:
                # Ties on ranking go to the EARLIEST feasible page: within
                # the feasible interval, a same-scoring later page is a
                # running header or reference repeating the title, never a
                # page before the chapter began.
                key = (_candidate_ranking_key(c), -c.index, len(variant_title))
                if best_feasible is None or key > (best_feasible[0], -best_feasible[3].index, best_feasible[1]):
                    best_feasible = (key[0], len(variant_title), variant_title, c)
        if best_feasible is None:
            unlocated.append(entry)
            continue
        _, _, winning_title, winner = best_feasible
        match = ChapterStartMatch(
            index=winner.index, score=winner.score, margin=_LOCATE_MARGIN_REQUIRED,
            author_confirmed=winner.author_confirmed,
        )
        resolved[k] = (replace(entry, title=winning_title) if winning_title != entry.title else entry, match)

    located = [pair for pair in resolved if pair is not None]
    # Order by PDF page index (not TOC order, which is printed-page order
    # and could disagree if entries were matched to pages out of sequence).
    located.sort(key=lambda pair: pair[1].index)
    return located, unlocated, exclude_indices


def _chapters_from_located(
    pages: list[str],
    located: list[tuple[TocEntry, ChapterStartMatch]],
    entry_source: dict[TocEntry, str] | None = None,
    non_content_pages: set[int] | None = None,
) -> list[dict]:
    """Turns located (entry, match) pairs into the final chapter dict list:
    clusters each entry's page range against its neighbor, trims trailing
    blank/divider pages, extracts citation_pages and confidence. entry_source
    marks which TocEntry objects were LLM-sourced (default "heuristic" for
    any entry not present in the mapping) -- see design spec §7.
    """
    entry_source = entry_source or {}
    non_content_pages = non_content_pages or set()
    total_pages = len(pages)
    header_lines = _running_header_lines(tuple(pages))
    # Roman-numeral prefixes ("II. Politische und soziale Determinanten...")
    # mark part dividers only when a minority of entries carry them -- a book
    # that numbers its actual chapters with roman numerals would prefix most
    # entries, and skipping those would discard the whole book.
    roman_prefixed = sum(1 for entry, _ in located if _ROMAN_PREFIX_RE.match(entry.title))
    roman_marks_dividers = 0 < roman_prefixed <= len(located) // 2
    chapters: list[dict] = []
    for i, (entry, match) in enumerate(located):
        if _is_part_divider(entry.title) or _is_back_matter(entry.title) or (
            roman_marks_dividers and _ROMAN_PREFIX_RE.match(entry.title)
        ):
            # Structural marker: bounds its neighbors (via its position in
            # `located`) but is not itself a chapter.
            continue
        start_index = match.index
        end_index = (located[i + 1][1].index - 1) if i + 1 < len(located) else (total_pages - 1)
        if end_index < start_index:
            continue  # degenerate/ambiguous overlap — skip rather than guess

        # Back off past trailing blank/divider pages (e.g. a blank page
        # forcing the next chapter to start on a recto page) -- these
        # belong to neither neighboring chapter. Mirrors the proven
        # build_draft logic in scripts/ground_truth_helper.py, which needed
        # the identical fix when building ground truth by hand.
        # Measured on header-stripped text: a book that stamps every page
        # with a long running header would otherwise make even a genuinely
        # blank divider page look like it has content. Known non-content
        # pages (the TOC itself, detected listing/part-divider pages) are
        # trimmed regardless of how much text they carry -- a dense
        # part-divider page that lists its part's chapters belongs to no
        # chapter, but has far too much text for the blank-page test.
        while end_index > start_index and (
            end_index in non_content_pages
            or len(_strip_running_headers(pages[end_index], header_lines).strip()) < _TRAILING_BLANK_PAGE_MAX_CHARS
        ):
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
            "source": entry_source.get(entry, "heuristic"),
        })
    return chapters


def analyze_attachment(pages: list[str]) -> dict:
    """Orchestrate TOC detection, content-based localization, printed-page
    extraction, and author NER into the per-attachment output described in
    design spec §5. Pure and synchronous -- unchanged behavior/speed from
    before the LLM fallback project; every chapter now also carries
    "source": "heuristic" (see design spec §7).
    """
    total_pages = len(pages)
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}

    located, _unlocated, non_content_pages = _locate_toc_entries(pages, toc_entries, exclude_indices=toc_page_indices)
    chapters = _chapters_from_located(pages, located, non_content_pages=non_content_pages)

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


async def analyze_attachment_with_llm_fallback(pages: list[str], llm_service: LLMService) -> dict:
    """Runs the heuristic TOC/locate pipeline first, then applies the two
    LLM fallback paths (design spec §4/§6) only where the heuristic pass
    reports a failure: TOC extraction when it found nothing usable at all,
    and per-chapter start disambiguation for any entry left genuinely
    ambiguous (as opposed to zero candidates -- out of scope, see spec §2).
    Any LLM failure (network, malformed JSON) is swallowed and treated as
    "fallback unavailable" -- never leaves the result worse than the pure
    heuristic pass would have (spec §11).
    """
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}
    located, unlocated, non_content_pages = _locate_toc_entries(pages, toc_entries, exclude_indices=toc_page_indices)
    heuristic_chapters = _chapters_from_located(pages, located, non_content_pages=non_content_pages)

    entry_source: dict[TocEntry, str] = {}
    llm_toc_extraction_used = False
    if len(toc_entries) == 0 or len(heuristic_chapters) == 0:
        try:
            llm_entries = await llm_extract_toc_entries(pages, llm_service)
        except Exception:
            logger.warning("analyze_attachment_with_llm_fallback: TOC extraction failed", exc_info=True)
            llm_entries = []
        if llm_entries:
            toc_entries = llm_entries
            toc_page_indices = _toc_scan_indices(pages)
            # If two entries are structurally equal (rare), this dict
            # comprehension collapses them to one key -- harmless, since
            # toc_entries/located/unlocated below stay plain lists and each
            # equal entry is still processed independently; do not "fix" by
            # deduplicating toc_entries, that would drop a real chapter.
            entry_source = {e: "llm" for e in toc_entries}
            located, unlocated, non_content_pages = _locate_toc_entries(pages, toc_entries, exclude_indices=toc_page_indices)
            llm_toc_extraction_used = True

    disambiguation_count = 0
    for entry in unlocated:
        candidates = locate_chapter_start_candidates(pages, entry.title, exclude_indices=toc_page_indices, authors=entry.authors)
        if len(candidates) <= 1:
            continue  # zero-candidate case is out of scope for v1 (design spec §2)
        try:
            resolved = await llm_disambiguate_chapter_start(pages, entry.title, entry.authors, candidates, llm_service)
        except Exception:
            logger.warning("analyze_attachment_with_llm_fallback: disambiguation failed for %r", entry.title, exc_info=True)
            continue
        if resolved is not None:
            located.append((entry, resolved))
            entry_source[entry] = "llm"
            disambiguation_count += 1

    if llm_toc_extraction_used or disambiguation_count:
        located.sort(key=lambda pair: pair[1].index)
        chapters = _chapters_from_located(pages, located, entry_source=entry_source, non_content_pages=non_content_pages)
    else:
        # Nothing changed since heuristic_chapters was computed above (no
        # LLM path fired) -- reuse it instead of re-running clustering/NER
        # a second time on the common (heuristic-succeeds) path.
        chapters = heuristic_chapters

    return {
        "total_pdf_pages": len(pages),
        "segmentation_confidence": "high" if chapters else "low",
        "chapters": chapters,
        "diagnostics": {
            "toc_pages_scanned": sorted(toc_page_indices),
            "toc_matches_found": len(toc_entries),
            "toc_matches_located": len(located),
            "llm_toc_extraction_used": llm_toc_extraction_used,
            "llm_disambiguation_used": disambiguation_count,
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
    llm_service: Optional[LLMService] = None,
    ocr_cache_dir: Optional[Path] = None,
) -> dict:
    """Core logic for script 1 (analyze_book_chapters). Scans `book`-type
    items in the library (or the explicit `item_keys` list), skips already-
    linked ones unless `relink`, downloads each PDF attachment, and runs
    analyze_attachment on its page text. See design spec §5.

    If a PDF has no extractable text layer and `ocr_cache_dir` is given,
    checks script 2's (chapter_ocr.py) on-disk cache -- keyed by the same
    content hash -- for already-OCR'd page text before falling back to
    reporting `needs_ocr: True`. This is what lets a re-run of this script
    pick up chapters from a book that was OCR'd since the previous run.
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
    analysis_mode = "llm_fallback" if llm_service is not None else "heuristic"
    for i, book in enumerate(books):
        item_key = book["data"]["key"]
        progress_callback(i / total, f"Analyzing {item_key} ({i + 1}/{total})")

        children = await zotero_client.get_item_children(library_id, item_key, library_type=library_type)
        pdf_attachments = [c for c in children if c["data"].get("contentType") == "application/pdf"]
        if not pdf_attachments:
            continue
        attachment_key = pdf_attachments[0]["data"]["key"]
        attachment_version = pdf_attachments[0]["data"].get("version", 0)

        if ocr_cache_dir is not None:
            cached_entry = load_cached_analysis(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode)
            if cached_entry is not None:
                attachments_out.append(cached_entry)
                continue

        file_bytes = await zotero_client.get_attachment_file(library_id, attachment_key, library_type=library_type)
        if not file_bytes:
            continue

        pages = extract_page_texts_from_pdf_bytes(file_bytes)
        has_text_layer = sum(len(p.strip()) for p in pages) > 100

        if not has_text_layer and ocr_cache_dir is not None:
            content_hash = hashlib.sha256(file_bytes).hexdigest()
            cached = load_cached_ocr(ocr_cache_dir, content_hash)
            if cached is not None:
                pages = cached["pages"]
                has_text_layer = sum(len(p.strip()) for p in pages) > 100

        if not has_text_layer:
            attachments_out.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "has_text_layer": False,
                "needs_ocr": True,
            })
            continue

        if llm_service is not None:
            analysis = await analyze_attachment_with_llm_fallback(pages, llm_service)
        else:
            analysis = analyze_attachment(pages)
        result_entry = {
            "item_key": item_key,
            "attachment_key": attachment_key,
            "has_text_layer": True,
            "needs_ocr": False,
            **analysis,
        }
        if ocr_cache_dir is not None:
            save_analysis_cache(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode, result_entry)
        attachments_out.append(result_entry)

    ocr_entries = [
        {
            "queue_id": f"ocr:{a['attachment_key']}",
            "type": "ocr",
            "bucket": "review",
            "payload": {"book_key": a["item_key"], "attachment_key": a["attachment_key"]},
        }
        for a in attachments_out
        if a.get("needs_ocr")
    ]
    if ocr_entries:
        review_queue_store.upsert_many(get_settings().review_queue_path, slug, ocr_entries)

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
