"""Shared loading helpers for the book-segmentation evaluation set.

Single home for the manifest-merging, PDF-availability, and page-loading
logic that backend/tests/test_chapter_segmentation_accuracy.py and the
scripts/evaluate_chapter_segmentation_*.py scripts previously each carried
their own copy of. Lives under backend/evaluation/ (not backend/tests/)
because scripts/ must not depend on the test tree.

Page loading mirrors production's chapter_segmentation.run(): default
extraction with the layout-mode fallback, then -- for books whose text
layer is absent or degenerate (pages_need_ocr) -- the content-hash-keyed
OCR cache populated by scripts/ocr_evaluation_pdfs.py. A book whose OCR
cache entry is missing loads as None and should be skipped by the caller
with a pointer to that script.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from backend.services.chapter_ocr import load_cached_ocr
from backend.services.chapter_segmentation import (
    extract_page_texts_for_analysis,
    pages_need_ocr,
)

EVAL_DIR = Path(__file__).resolve().parent / "book-segmentation"
OCR_CACHE_DIR = EVAL_DIR / ".ocr-cache"


def load_manifest_books() -> list[dict]:
    """Merge the committed manifest.json with the gitignored, optional
    manifest.local.json (see book-segmentation/CLAUDE.md) -- the latter
    holds books that have no DOI or otherwise can't be shared, still
    exercised in local runs on the machine that added them."""
    books = json.loads((EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))["books"]
    local_manifest_path = EVAL_DIR / "manifest.local.json"
    if local_manifest_path.exists():
        books = books + json.loads(local_manifest_path.read_text(encoding="utf-8"))["books"]
    return books


def available_books() -> list[tuple[Path, Path, dict]]:
    """(pdf_path, expected_json_path, manifest_entry) for every manifest
    book whose PDF and ground truth are both present locally right now."""
    triples = []
    for book in load_manifest_books():
        pdf_path = EVAL_DIR / book["filename"]
        expected_path = EVAL_DIR / (Path(book["filename"]).stem + ".expected.json")
        if pdf_path.exists() and expected_path.exists():
            triples.append((pdf_path, expected_path, book))
    return triples


def analysis_pages_for(file_bytes: bytes) -> Optional[list[str]]:
    """Page texts for this PDF the same way production run() would see
    them, or None when the book needs OCR and the eval OCR cache has no
    entry yet (run scripts/ocr_evaluation_pdfs.py to populate it)."""
    pages, _layout_used = extract_page_texts_for_analysis(file_bytes)
    if not pages_need_ocr(pages):
        return pages
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    cached = load_cached_ocr(OCR_CACHE_DIR, content_hash)
    if cached is not None:
        return cached["pages"]
    return None
