"""Zotero-specific batch OCR job (script 2: ocr_attachments) -- fetches
attachments lacking a usable text layer and OCRs them via a pluggable
chapter_segmentation.ocr.OcrBackend. Production always uses
KreuzbergOcrBackend (see backend/api/chapter_linking.py), since the
Kreuzberg sidecar is already part of this deployment. The OCR engine
itself, its caching, and language detection live in the standalone
chapter_segmentation package -- see
docs/superpowers/specs/2026-08-06-chapter-segmentation-extraction-design.md.
"""

import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

from chapter_segmentation.ocr import OcrBackend, detect_language, load_cached_ocr, ocr_pdf_pages

logger = logging.getLogger(__name__)


async def run(
    *,
    zotero_client,
    ocr_backend: OcrBackend,
    library_id: str,
    library_type: str,
    attachment_specs: list[dict],
    max_items: Optional[int],
    cache_dir: Path,
    progress_callback: Callable[[float, str], None],
) -> dict:
    """Core logic for script 2 (ocr_attachments). `attachment_specs` is
    typically script 1's `needs_ocr: true` output list. See module docstring.
    """
    specs = attachment_specs[:max_items] if max_items is not None else attachment_specs
    results: list[dict] = []
    total = len(specs) or 1

    for i, spec in enumerate(specs):
        item_key = spec["item_key"]
        attachment_key = spec["attachment_key"]
        progress_callback(i / total, f"OCR-ing {item_key} ({i + 1}/{total})")

        # NOTE: get_attachment_file's URL is /items/{key}/file -- it needs the
        # PDF attachment's OWN key, not the containing book item's key.
        file_bytes = await zotero_client.get_attachment_file(library_id, attachment_key, library_type=library_type)
        if not file_bytes:
            results.append({"item_key": item_key, "attachment_key": attachment_key, "ocr_succeeded": False})
            continue

        content_hash = hashlib.sha256(file_bytes).hexdigest()
        cached = load_cached_ocr(cache_dir, content_hash)
        if cached is not None:
            results.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "detected_language": cached["detected_language"],
                "ocr_succeeded": True,
                "char_count": sum(len(p) for p in cached["pages"]),
                "cache_path": str(cache_dir / f"{content_hash}.json"),
            })
            continue

        try:
            item = await zotero_client.get_item(library_id, item_key, library_type=library_type)
            language = detect_language(item["data"].get("language"), item["data"].get("title", ""))

            page_texts = await ocr_pdf_pages(file_bytes, backend=ocr_backend, cache_dir=cache_dir, language=language)
            results.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "detected_language": language,
                "ocr_succeeded": True,
                "char_count": sum(len(p) for p in page_texts),
                "cache_path": str(cache_dir / f"{content_hash}.json"),
            })
        except Exception as exc:
            # A failure on one attachment (e.g. a Kreuzberg sidecar timeout partway
            # through a large scanned book) must not discard other already-cached
            # results or abort the rest of the batch.
            logger.error(f"OCR failed for item {item_key} attachment {attachment_key}: {exc}")
            results.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "ocr_succeeded": False,
                "error": str(exc),
            })

    progress_callback(1.0, "Done")
    return {"results": results}
