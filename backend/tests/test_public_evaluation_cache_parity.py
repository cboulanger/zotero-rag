"""Aggregate precision/recall parity check for the redacted public-cache
corpus (backend/evaluation/book-segmentation/public-cache/) -- see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md
section 9.

Needs no PDFs or .ocr-cache/ -- only the committed public-cache/ directory,
so this is what CI and contributors without the source books actually run.

Marked "integration" so it's excluded from the default `uv run pytest` run
(see pyproject.toml's addopts) -- reported, not gated, same as
test_chapter_segmentation_accuracy.py.

    uv run pytest backend/tests/test_public_evaluation_cache_parity.py -q -s
"""

import json
import unittest

import pytest

from backend.evaluation.harness import available_public_books, public_pages_for
from backend.services.chapter_segmentation import analyze_attachment

pytestmark = pytest.mark.integration


@unittest.skipUnless(
    available_public_books(),
    "No public-cache entries present -- run: "
    "uv run python scripts/generate_public_evaluation_cache.py",
)
class TestPublicEvaluationCacheParity(unittest.TestCase):
    def test_boundary_precision_recall_per_book(self):
        for manifest_key, expected_path, _book in available_public_books():
            with self.subTest(book=manifest_key):
                expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
                pages = public_pages_for(manifest_key)
                result = analyze_attachment(pages)

                expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
                found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
                true_positives = expected_ranges & found_ranges

                precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
                recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
                print(f"{manifest_key}: precision={precision:.2f} recall={recall:.2f} "
                      f"({len(true_positives)}/{len(found_ranges)} found, "
                      f"{len(true_positives)}/{len(expected_ranges)} expected)")


if __name__ == "__main__":
    unittest.main()
