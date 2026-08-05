"""Precision/recall scoring for chapter_segmentation.analyze_attachment
against the real, hand-verified ground-truth books in
backend/evaluation/book-segmentation/ (design spec §5, §12).

The PDFs themselves are gitignored — run
`uv run python scripts/fetch_evaluation_pdfs.py` first to download the
open-access ones. A book is skipped (not failed) if its PDF isn't present
locally yet, or if it needs OCR and the evaluation OCR cache hasn't been
populated (run `uv run python scripts/ocr_evaluation_pdfs.py` with the
Kreuzberg sidecar up) — both are real, checkable states, not placeholders.

Pages are loaded exactly the way production's run() sees them (layout-mode
fallback + OCR cache) via backend/evaluation/harness.py.

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

import pytest

from backend.evaluation.harness import analysis_pages_for, available_books
from backend.services.chapter_segmentation import analyze_attachment

pytestmark = pytest.mark.integration


@unittest.skipUnless(
    available_books(),
    "No evaluation PDFs present — run: uv run python scripts/fetch_evaluation_pdfs.py",
)
class TestChapterSegmentationAccuracy(unittest.TestCase):
    # The default 30s global timeout (pyproject.toml) is sized for the
    # original 7-book committed set; the layout-mode re-extraction pass on
    # large manifest.local.json books is slow (whole-book re-extraction per
    # book that triggers it), so give the single all-books method plenty of
    # room.
    @pytest.mark.timeout(900)
    def test_boundary_precision_recall_per_book(self):
        for pdf_path, expected_path, book in available_books():
            with self.subTest(book=pdf_path.name):
                expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
                pages = analysis_pages_for(pdf_path.read_bytes())
                if pages is None:
                    print(f"{pdf_path.name}: SKIPPED (needs OCR — populate the cache with: "
                          f"uv run python scripts/ocr_evaluation_pdfs.py)")
                    continue
                result = analyze_attachment(pages)

                expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
                found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
                true_positives = expected_ranges & found_ranges

                precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
                recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
                print(f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
                      f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected)")
                if book.get("heuristic_expected_zero", False):
                    # This book is a known, accepted heuristic limitation --
                    # zero recall even after the layout fallback and OCR
                    # route (see book-segmentation/RESULTS.md) -- so zero is
                    # the expected outcome here, not a regression.
                    continue
                # Reported, not gated (design spec §12: probabilistic, not pass/fail) —
                # this assertion only catches a total regression to zero detection.
                self.assertGreater(recall, 0.0, f"{pdf_path.name}: detected zero of {len(expected_ranges)} known chapters")


if __name__ == "__main__":
    unittest.main()
