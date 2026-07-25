#!/usr/bin/env python3
"""Draft a chapter-segmentation ground-truth `.expected.json` from a real PDF
and a hand-transcribed table of contents.

This is a starting point, not an oracle: it locates each TOC entry's true
chapter-OPENING page by content search (never by assuming
pdf_index == printed_page_number -- see docs/superpowers/specs/
2026-07-24-chapter-segmentation-linking-design.md section 2/5 for why that
assumption breaks on real books), and separately tries to read the printed
page number actually shown on that page for `citation_pages`. Always spot-check
a handful of the output's pdf_start_index/pdf_end_index values by opening the
PDF at those physical page indices before trusting them -- see
backend/evaluation/book-segmentation/CLAUDE.md for the full workflow this
script is one step of.

Usage:
    1. Open the PDF and transcribe its table of contents into a small JSON
       file: a list of {"title": ..., "authors": [...]} objects in reading
       order. Add {"skip": true} entries (authors: []) for any front/back
       matter section between two real chapters that you don't want in the
       final ground truth (e.g. "Acknowledgements", "Bibliography") -- they
       are still needed here to correctly bound their neighbors' ranges.

    2. Run:
        uv run python scripts/ground_truth_helper.py \
            --pdf backend/evaluation/book-segmentation/<name>.pdf \
            --toc /tmp/<name>_toc.json \
            --output backend/evaluation/book-segmentation/<name>.expected.json

    3. Open the output and manually verify every entry (this script has no
       way to know if it's wrong -- it found the best-scoring match, not
       necessarily the correct one). Pay special attention to:
       - Any `match_score` below ~90 -- likely a wrong match.
       - `citation_start`/`citation_end` of `null` -- the printed-number
         heuristic didn't find a footer/header number on that page; leave it
         null in the final file rather than guessing.
       - Gaps between one chapter's pdf_end_index and the next chapter's
         pdf_start_index bigger than 1-2 pages -- often a Part-divider page
         (correctly excluded) but sometimes a sign the real boundary is off.
"""

import argparse
import json
import re

from pypdf import PdfReader
from rapidfuzz import fuzz

_PAGE_NUM_RE = re.compile(r"^[0-9]{1,4}$|^[ivxlcdm]{1,7}$", re.IGNORECASE)

# Roman-numeral matches require a non-letter boundary on the adjacent side --
# otherwise a bare trailing/leading "d", "i", "v", "x", "l", "c", or "m" from
# an ordinary word (e.g. "Afterword", "Index") false-positives as a
# roman-numeral page number.
_TRAILING_NUM_RE = re.compile(r"(?<![A-Za-z])(\d{1,4}|[ivxlcdm]{1,7})\s*$", re.IGNORECASE)
_LEADING_NUM_RE = re.compile(r"^(\d{1,4}|[ivxlcdm]{1,7})(?![A-Za-z])", re.IGNORECASE)

# A real table-of-contents page has several "<title> ... <page number>" lines
# close together; a real chapter-start page does not. Requiring 3+ such lines
# on one page (rather than fuzzy-matching titles against every page, which
# false-positives on chapter-start pages that legitimately mention other
# chapters) is what distinguishes a TOC/listing page to exclude from search.
_TOC_LINE_RE = re.compile(r"^.{3,100}?[.\s]{1,}\d{1,4}\s*$")


def load_pages(pdf_path: str) -> list[str]:
    reader = PdfReader(pdf_path)
    return [page.extract_text() or "" for page in reader.pages]


def _looks_like_url(line: str) -> bool:
    return "doi.org" in line or "http" in line.lower() or line.count(".") >= 3


def extract_printed_number(text: str) -> str | None:
    """Best-effort extraction of the printed page number shown on a page,
    checking (1) an isolated footer/header line that's just the number, then
    (2) a number embedded at either end of the first non-URL line (running
    headers alternate between "<num> <author>" and "<title> ... <num>"
    depending on recto/verso convention). Returns None rather than guessing.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not _looks_like_url(line)]
    if not lines:
        return None
    for line in (lines[-2:] + lines[:2]):
        if _PAGE_NUM_RE.match(line):
            return line
    if len(lines[0]) < 120:
        match = _TRAILING_NUM_RE.search(lines[0])
        if match:
            return match.group(1)
        match = _LEADING_NUM_RE.match(lines[0])
        if match:
            return match.group(1)
    return None


def find_toc_pages(pages: list[str]) -> set[int]:
    toc_pages = set()
    for index, text in enumerate(pages):
        hits = sum(1 for line in text.splitlines() if _TOC_LINE_RE.match(line.strip()))
        if hits >= 3:
            toc_pages.add(index)
    return toc_pages


def locate_chapter_start(
    pages: list[str],
    title: str,
    authors: list[str],
    start_search: int,
    exclude: set[int],
) -> tuple[int | None, float]:
    """Find the true chapter-OPENING page, not merely a continuation page
    whose running header repeats the chapter title (common in academic
    layouts). Requires a supplied author's last name to appear near the top
    of the page too -- the title+byline block is unique to the opening page.
    Falls back to a title-only match if no author-confirmed page is found.
    """
    last_names = [a.split()[-1].lower() for a in authors if a.strip()]

    best_confirmed = (None, 0.0)
    best_title_only = (None, 0.0)
    for index in range(start_search, len(pages)):
        if index in exclude:
            continue
        head = pages[index][:250].lower()
        score = fuzz.partial_ratio(title.lower(), head)
        if score > best_title_only[1]:
            best_title_only = (index, score)
        if score >= 90 and last_names and any(name in head for name in last_names):
            if score > best_confirmed[1]:
                best_confirmed = (index, score)

    return best_confirmed if best_confirmed[0] is not None else best_title_only


def build_draft(pdf_path: str, toc: list[dict]) -> list[dict]:
    pages = load_pages(pdf_path)
    toc_pages = find_toc_pages(pages)

    results = []
    search_from = 0
    for entry in toc:
        index, score = locate_chapter_start(
            pages, entry["title"], entry.get("authors", []), search_from, toc_pages
        )
        results.append({
            "title": entry["title"],
            "authors": entry.get("authors", []),
            "pdf_start_index": index,
            "match_score": score,
        })
        if index is not None:
            search_from = index + 1

    for i, result in enumerate(results):
        if result["pdf_start_index"] is None:
            result["pdf_end_index"] = None
            continue
        next_start = results[i + 1]["pdf_start_index"] if i + 1 < len(results) else len(pages)
        end = (next_start - 1) if next_start is not None else len(pages) - 1
        # Back off past short "Part N" divider pages (title-only, no body
        # text) -- these belong to neither neighboring chapter.
        while end > result["pdf_start_index"] and len(pages[end].strip()) < 150:
            end -= 1
        result["pdf_end_index"] = end
        result["citation_start"] = extract_printed_number(pages[result["pdf_start_index"]])
        result["citation_end"] = extract_printed_number(pages[end])

    # Entries marked "skip" (e.g. Acknowledgements, Contributors) were only
    # needed to bound their neighbors -- drop them from the final output.
    return [r for i, r in enumerate(results) if not toc[i].get("skip")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pdf", required=True, help="Path to the real PDF")
    parser.add_argument("--toc", required=True, help="JSON file: [{title, authors, skip?}] in reading order")
    parser.add_argument("--output", default=None, help="Write draft JSON here instead of stdout")
    args = parser.parse_args()

    toc = json.loads(open(args.toc, encoding="utf-8").read())
    draft = build_draft(args.pdf, toc)
    output = json.dumps(draft, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote draft to {args.output} -- now verify every entry by hand before trusting it.")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
