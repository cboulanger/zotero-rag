"""Turns a RegionMap (scripts/evaluation_redaction/region_classification.py)
into per-character preserve/redact decisions and applies word-level
redaction -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md sections 3.1, 5."""

import hashlib
import re

from backend.services.chapter_segmentation import _LISTING_PAGE_BODY_WINDOW, _normalize_header_line, _PAGE_NUMBER_TOKEN_RE
from scripts.evaluation_redaction.region_classification import RegionMap, _header_stripped_offset, classify_regions
from scripts.evaluation_redaction.wordlists import pick_word, build_word_pool

_TOKEN_RE = re.compile(r"(?P<word>\w+)|(?P<other>\W+)", re.UNICODE)

# Beyond _LISTING_PAGE_BODY_WINDOW characters past the header-stripped start
# of a page, no production heuristic reads content anymore (see
# chapter_segmentation._secondary_listing_pages -- the largest fixed window
# any heuristic uses; locate_chapter_start_candidates' 200-char heading
# window is smaller and already handled verbatim via heading_windows). Only
# the page's TOTAL length still matters past that point, for the trailing-
# blank-page trim in chapter_segmentation._chapters_from_located -- so word
# tokens past the cutoff get length-preserving filler instead of a real
# pool word: same effect on every measured heuristic, without generating
# realistic-looking prose nothing ever reads. "q" is not a roman-numeral
# letter (chapter_segmentation._PAGE_NUMBER_TOKEN_RE's [ivxlcdm]) or a
# digit, so a filler run can never be mistaken for a page number.
_FILLER_CHAR = "q"


def build_preserve_mask(text: str, page_index: int, regions: RegionMap) -> list[bool]:
    """True at every character index in `text` that must survive redaction
    unchanged: this page's chapter-heading window (if any) and every line
    on this page whose normalized form is a recognized running header."""
    if page_index in regions.full_pages:
        return [True] * len(text)
    mask = [False] * len(text)
    window = regions.heading_windows.get(page_index)
    if window is not None:
        start, end = window
        for i in range(max(start, 0), min(end, len(text))):
            mask[i] = True
    pos = 0
    for line in text.splitlines(keepends=True):
        if _normalize_header_line(line.rstrip("\r\n")) in regions.header_lines:
            for i in range(pos, pos + len(line)):
                mask[i] = True
        pos += len(line)
    return mask


def _match_case(word: str, original: str) -> str:
    if original.isupper() and len(original) > 1:
        return word.upper()
    if original[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


def redact_page(text: str, page_index: int, regions: RegionMap, pool: dict[int, list[str]], book_salt: str) -> str:
    """Replaces every word token outside `regions`' preserved spans with a
    deterministic, same-length (or nearest-length) real word from `pool`,
    up to `_LISTING_PAGE_BODY_WINDOW` characters past the header-stripped
    start of the page; word tokens beyond that get length-preserving
    filler instead (see `_FILLER_CHAR`). Digits, roman-numeral-shaped
    tokens, and preserved spans pass through unchanged everywhere."""
    mask = build_preserve_mask(text, page_index, regions)
    real_content_cutoff = _header_stripped_offset(text, regions.header_lines) + _LISTING_PAGE_BODY_WINDOW
    out: list[str] = []
    pos = 0
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        start = pos
        pos += len(token)
        if match.group("word") is None:
            out.append(token)  # whitespace/punctuation run
            continue
        if all(mask[start:pos]):
            out.append(token)  # inside a preserved span
            continue
        if token.isdigit() or _PAGE_NUMBER_TOKEN_RE.match(token):
            out.append(token)  # page-number-shaped token, never prose
            continue
        if start >= real_content_cutoff:
            out.append(_FILLER_CHAR * len(token))  # past every heuristic's read window
            continue
        seed = int.from_bytes(
            hashlib.sha256(f"{book_salt}:{page_index}:{start}:{token.casefold()}".encode("utf-8")).digest()[:8],
            "big",
        )
        out.append(_match_case(pick_word(pool, len(token), seed), token))
    return "".join(out)


def redact_book(pages: list[str], detected_language: str, book_salt: str) -> list[str]:
    """Full per-book redaction pipeline: classify regions once, build the
    language's word pool once, then redact every page."""
    regions = classify_regions(pages)
    pool = build_word_pool(detected_language)
    return [redact_page(text, index, regions, pool, book_salt) for index, text in enumerate(pages)]
