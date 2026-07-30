"""Precision/recall scoring for chapter_segmentation.analyze_attachment
against the real, hand-verified ground-truth books in
backend/evaluation/book-segmentation/ (design spec §5, §12).

The PDFs themselves are gitignored — run
`uv run python scripts/fetch_evaluation_pdfs.py` first to download the
open-access ones. A book is skipped (not failed) if its PDF isn't present
locally yet (covers "not fetched yet", the non-OA scans that can never be
auto-fetched, and any manifest.local.json entries a developer hasn't placed
the PDF for) — this is real, checkable state, not a placeholder standing in
for unwritten logic.

Marked "integration" so it's excluded from the default `uv run pytest` /
`npm test` run (see pyproject.toml's addopts) -- this is a reported, not
gated, benchmark (design spec §12: probabilistic, not pass/fail), not
something that should ever block CI. Run it directly:

    uv run pytest backend/tests/test_chapter_segmentation_accuracy.py -q -s

`-s` is required to see the per-book summary lines (pytest swallows `print`
output by default).
"""

import json
import unittest
from pathlib import Path

import pytest

from backend.services.chapter_segmentation import (
    analyze_attachment,
    extract_page_texts_from_pdf_bytes,
)

pytestmark = pytest.mark.integration

_EVAL_DIR = Path(__file__).parent.parent / "evaluation" / "book-segmentation"


def _load_manifest_books() -> list[dict]:
    """Merge the committed manifest.json with the gitignored, optional
    manifest.local.json (see backend/evaluation/book-segmentation/CLAUDE.md)
    -- the latter holds "difficult" books found during live testing that
    have no DOI or otherwise can't be shared, so they stay local-only but
    are still exercised by this harness on the machine that added them.
    """
    books = json.loads((_EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))["books"]
    local_manifest_path = _EVAL_DIR / "manifest.local.json"
    if local_manifest_path.exists():
        books = books + json.loads(local_manifest_path.read_text(encoding="utf-8"))["books"]
    return books


def _available_books() -> list[tuple[Path, Path]]:
    """Return (pdf_path, expected_json_path) pairs for every manifest entry
    (committed or local-only) whose PDF is actually present locally right
    now."""
    pairs = []
    for book in _load_manifest_books():
        pdf_path = _EVAL_DIR / book["filename"]
        expected_path = _EVAL_DIR / (Path(book["filename"]).stem + ".expected.json")
        if pdf_path.exists() and expected_path.exists():
            pairs.append((pdf_path, expected_path))
    return pairs


@unittest.skipUnless(
    _available_books(),
    "No evaluation PDFs present — run: uv run python scripts/fetch_evaluation_pdfs.py",
)
class TestChapterSegmentationAccuracy(unittest.TestCase):
    def test_boundary_precision_recall_per_book(self):
        for pdf_path, expected_path in _available_books():
            with self.subTest(book=pdf_path.name):
                expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
                pages = extract_page_texts_from_pdf_bytes(pdf_path.read_bytes())
                result = analyze_attachment(pages)

                expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
                found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
                true_positives = expected_ranges & found_ranges

                precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
                recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
                print(f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
                      f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected)")
                # Reported, not gated (design spec §12: probabilistic, not pass/fail) —
                # this assertion only catches a total regression to zero detection.
                self.assertGreater(recall, 0.0, f"{pdf_path.name}: detected zero of {len(expected_ranges)} known chapters")


if __name__ == "__main__":
    unittest.main()
