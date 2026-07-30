#!/usr/bin/env python3
"""Runs the chapter-segmentation evaluation set (see backend/evaluation/
book-segmentation/) through analyze_attachment_with_llm_fallback instead of
the pure-heuristic analyze_attachment, and prints the same precision/recall
table format backend/tests/test_chapter_segmentation_accuracy.py already uses, plus
per-book fallback-usage counts.

Requires a real, working LLM (reads normal app settings/API keys) and costs
a paid API call per book -- not a pytest test, run manually:

    uv run python scripts/evaluate_chapter_segmentation_llm_fallback.py

Pass --auto-select-model to retry across the active preset's available
models (most-available first, resolved live -- never a hardcoded model
name) instead of a single fixed model, useful when the preset's configured
default model is unreachable or overloaded:

    uv run python scripts/evaluate_chapter_segmentation_llm_fallback.py --auto-select-model

See docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md §10.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.dependencies import make_llm_service
from backend.services.chapter_segmentation import (
    analyze_attachment_with_llm_fallback,
    extract_page_texts_from_pdf_bytes,
)

_EVAL_DIR = Path(__file__).resolve().parent.parent / "backend" / "evaluation" / "book-segmentation"


def _load_manifest_books() -> list[dict]:
    # Mirrors backend/tests/test_chapter_segmentation_accuracy.py's identically-named
    # helper -- kept as a separate copy rather than importing across the
    # tests/scripts boundary (backend/tests/ is deliberately not a runtime
    # dependency of anything under scripts/).
    books = json.loads((_EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))["books"]
    local_manifest_path = _EVAL_DIR / "manifest.local.json"
    if local_manifest_path.exists():
        books = books + json.loads(local_manifest_path.read_text(encoding="utf-8"))["books"]
    return books


def _available_books() -> list[tuple[Path, Path]]:
    pairs = []
    for book in _load_manifest_books():
        pdf_path = _EVAL_DIR / book["filename"]
        expected_path = _EVAL_DIR / (Path(book["filename"]).stem + ".expected.json")
        if pdf_path.exists() and expected_path.exists():
            pairs.append((pdf_path, expected_path))
    return pairs


async def _main(auto_select_model: bool) -> int:
    pairs = _available_books()
    if not pairs:
        print("No evaluation PDFs present -- run: uv run python scripts/fetch_evaluation_pdfs.py")
        return 1

    llm_service = make_llm_service(auto_select_model=auto_select_model)

    for pdf_path, expected_path in pairs:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
        pages = extract_page_texts_from_pdf_bytes(pdf_path.read_bytes())
        result = await analyze_attachment_with_llm_fallback(pages, llm_service)

        expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
        found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
        true_positives = expected_ranges & found_ranges

        precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
        recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
        diag = result["diagnostics"]
        print(
            f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
            f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected) "
            f"llm_toc_extraction_used={diag.get('llm_toc_extraction_used')} "
            f"llm_disambiguation_used={diag.get('llm_disambiguation_used')}"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--auto-select-model",
        action="store_true",
        help="Retry across the active preset's available models (most-available "
             "first, resolved live) on error or an unusable response, instead of "
             "a single fixed model.",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(auto_select_model=args.auto_select_model)))
