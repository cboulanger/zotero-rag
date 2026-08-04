#!/usr/bin/env python3
# scripts/evaluate_chapter_segmentation_strategies.py
"""Runs the chapter-segmentation evaluation set (see backend/evaluation/
book-segmentation/) through analyze_attachment_with_strategies instead of
the pure-heuristic analyze_attachment, and prints the same precision/recall
table format backend/tests/test_chapter_segmentation_accuracy.py already
uses, plus per-book strategies_used diagnostics.

Not a pytest test -- makes real (free, cached) Crossref API calls per book:

    uv run python scripts/evaluate_chapter_segmentation_strategies.py

Pass --no-crossref to disable the Crossref lookup and see outline-only
numbers:

    uv run python scripts/evaluate_chapter_segmentation_strategies.py --no-crossref

See docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md section 12.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.evaluation.harness import analysis_pages_for, available_books
from backend.services.chapter_evidence.crossref_strategy import CrossrefMetadataStrategy
from backend.services.chapter_evidence.types import BookContext
from backend.services.chapter_evidence.zotero_catalog_strategy import ZoteroCatalogMetadataStrategy
from backend.services.chapter_segmentation import analyze_attachment_with_strategies


async def _main(enable_crossref: bool) -> int:
    triples = available_books()
    if not triples:
        print("No evaluation PDFs present -- run: uv run python scripts/fetch_evaluation_pdfs.py")
        return 1

    zotero_catalog_strategy = ZoteroCatalogMetadataStrategy({})  # no live library in this script

    async with httpx.AsyncClient() as http_client:
        crossref_strategy = (
            CrossrefMetadataStrategy(http_client, cache_dir=Path("data/crossref_cache"), contact_email=None)
            if enable_crossref else None
        )
        for pdf_path, expected_path, book in triples:
            expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
            file_bytes = pdf_path.read_bytes()
            pages = analysis_pages_for(file_bytes)
            if pages is None:
                print(f"{pdf_path.name}: SKIPPED (needs OCR — populate the cache with: "
                      f"uv run python scripts/ocr_evaluation_pdfs.py)")
                continue
            # The evaluation manifest names each PDF after its own ISBN-13
            # (see backend/evaluation/book-segmentation/README.md), so the
            # filename stem doubles as the ISBN BookContext needs.
            isbn = Path(book["filename"]).stem
            context = BookContext(
                item_key=book["filename"], isbn=isbn, title=book["title"],
                editors=(), publisher=None, year=None,
            )
            result = await analyze_attachment_with_strategies(
                pages, file_bytes, context, zotero_catalog_strategy,
                crossref_strategy=crossref_strategy,
            )

            expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
            found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
            true_positives = expected_ranges & found_ranges

            precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
            recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
            diag = result["diagnostics"]
            print(
                f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
                f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected) "
                f"strategies_used={diag.get('strategies_used')}"
            )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-crossref", action="store_true", help="Disable the Crossref lookup strategy")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(enable_crossref=not args.no_crossref)))
