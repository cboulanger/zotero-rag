#!/usr/bin/env python3
"""OCR the evaluation books whose text layer is absent or degenerate, into
the gitignored evaluation OCR cache
(backend/evaluation/book-segmentation/.ocr-cache/, content-hash keyed --
the same cache format production's chapter_ocr.py uses), so the accuracy
harness and evaluation scripts can analyze them the way production would.

Requires the Kreuzberg sidecar to be running (podman; see Settings.kreuzberg_url,
default http://localhost:8100). Books already cached are skipped instantly,
so re-runs are cheap; the first run over several full scanned books takes a
long time (per-page OCR over HTTP -- expect tens of minutes per scan book).

    uv run python scripts/ocr_evaluation_pdfs.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config.settings import get_settings
from backend.evaluation.harness import OCR_CACHE_DIR, available_books
from backend.services.chapter_ocr import detect_language, ocr_pdf_pages
from backend.services.chapter_segmentation import (
    extract_page_texts_for_analysis,
    pages_need_ocr,
)
from backend.services.extraction.kreuzberg import KreuzbergExtractor


async def _main() -> int:
    extractor = KreuzbergExtractor(kreuzberg_url=get_settings().kreuzberg_url)
    for pdf_path, _expected_path, book in available_books():
        file_bytes = pdf_path.read_bytes()
        pages, _layout_used = extract_page_texts_for_analysis(file_bytes)
        if not pages_need_ocr(pages):
            print(f"{pdf_path.name}: text layer usable, no OCR needed")
            continue
        # The manifest's language field is a plain code ("de"/"en") --
        # detect_language maps it onto the sidecar's installed tesseract
        # packs, falling back to title-based detection.
        language = detect_language(book.get("language"), book.get("title", ""))
        print(f"{pdf_path.name}: OCR-ing {len(pages)} pages (language={language}) ...", flush=True)
        page_texts = await ocr_pdf_pages(
            file_bytes,
            extractor=extractor,
            cache_dir=OCR_CACHE_DIR,
            language=language,
            on_page=lambda done, total: print(
                f"  {pdf_path.name}: {done}/{total} pages", flush=True
            ) if done % 25 == 0 or done == total else None,
        )
        print(f"{pdf_path.name}: done, {sum(len(p) for p in page_texts)} chars cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
