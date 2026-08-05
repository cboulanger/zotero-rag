#!/usr/bin/env python3
"""Generate backend/evaluation/book-segmentation/public-cache/ -- a
redacted, git-trackable corpus safe to commit and distribute (real
navigational/bibliographic text kept verbatim, chapter prose replaced with
random real words in the book's own language) -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md.

Run by a maintainer who has the real books locally; not something a
contributor without PDFs needs to run.

    uv run python scripts/generate_public_evaluation_cache.py [--book <manifest-key>] [--no-verify]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.evaluation.harness import PUBLIC_CACHE_DIR, analysis_pages_for, available_books
from backend.services.chapter_ocr import detect_language
from backend.services.chapter_segmentation import (
    analyze_attachment,
    extract_page_texts_for_analysis,
    pages_need_ocr,
)
from scripts.evaluation_redaction.redact import redact_book

CIPHER_VERSION = 1


def _verify(real_pages: list[str], redacted_pages: list[str]) -> list[str]:
    """Human-readable diff lines, empty when the two page sets produce
    identical detected chapter boundaries."""
    real_result = analyze_attachment(real_pages)
    redacted_result = analyze_attachment(redacted_pages)
    real_boundaries = {(c["pdf_start_index"], c["pdf_end_index"]) for c in real_result["chapters"]}
    redacted_boundaries = {(c["pdf_start_index"], c["pdf_end_index"]) for c in redacted_result["chapters"]}
    if real_boundaries == redacted_boundaries:
        return []
    return [
        f"  real boundaries:     {sorted(real_boundaries)}",
        f"  redacted boundaries: {sorted(redacted_boundaries)}",
    ]


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", help="Only regenerate this manifest key (filename stem)")
    parser.add_argument("--no-verify", action="store_true", help="Skip the exact-boundary-match check")
    args = parser.parse_args()

    PUBLIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for pdf_path, _expected_path, book in available_books():
        manifest_key = Path(book["filename"]).stem
        if args.book and manifest_key != args.book:
            continue
        file_bytes = pdf_path.read_bytes()
        real_pages = analysis_pages_for(file_bytes)
        if real_pages is None:
            print(f"{manifest_key}: SKIPPED (needs OCR -- run scripts/ocr_evaluation_pdfs.py first)")
            continue
        try:
            raw_pages, _layout_used = extract_page_texts_for_analysis(file_bytes)
            source = "ocr" if pages_need_ocr(raw_pages) else "extracted"
            language = detect_language(book.get("language"), book.get("title", ""))
            redacted_pages = redact_book(real_pages, detected_language=language, book_salt=manifest_key)
            if not args.no_verify:
                diff = _verify(real_pages, redacted_pages)
                if diff:
                    print(f"{manifest_key}: VERIFY FAILED -- redaction changed detected chapter boundaries")
                    print("\n".join(diff))
                    failures += 1
                    continue
        except Exception as exc:
            # One book's failure must not strand the rest of the batch --
            # same catch-log-continue shape as scripts/ocr_evaluation_pdfs.py.
            print(f"{manifest_key}: FAILED ({exc}) -- skipping")
            failures += 1
            continue
        cache_path = PUBLIC_CACHE_DIR / f"{manifest_key}.pages.json"
        cache_path.write_text(
            json.dumps(
                {"cipher_version": CIPHER_VERSION, "source": source, "pages": redacted_pages},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"{manifest_key}: OK, wrote {cache_path}")
    if failures:
        print(f"{failures} book(s) failed -- see above")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
