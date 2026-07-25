"""Language-aware OCR for attachments lacking a text layer (script 2:
ocr_attachments). See design spec §6.

Slices the book PDF page-by-page (pypdf) and OCRs each page individually
via the Kreuzberg sidecar, rather than sending the whole PDF as one
request — this guarantees a clean 1:1 page-index<->text mapping (matching
pypdf's own indexing used elsewhere in chapter_segmentation.py), avoiding
any ambiguity in how Kreuzberg's own chunking groups multi-page text.
"""

import hashlib
import io
import json
from pathlib import Path
from typing import Callable, Optional

from langdetect import DetectorFactory, LangDetectException, detect
from pypdf import PdfReader, PdfWriter

# Deterministic detection (langdetect is otherwise seeded from wall-clock time).
DetectorFactory.seed = 0

_INSTALLED_PACKS = {"de": "deu", "fr": "fra", "es": "spa", "en": "eng"}
_COMBINED_DEFAULT = "eng+deu+fra+spa"


def detect_language(item_language: Optional[str], title: str) -> str:
    """Prefer the Zotero item's own `language` field; otherwise detect from
    the title; otherwise fall back to the sidecar's combined default.
    """
    if item_language:
        code = item_language.split("-")[0].lower()
        if code in _INSTALLED_PACKS:
            return _INSTALLED_PACKS[code]

    if title.strip():
        try:
            code = detect(title)
        except LangDetectException:
            code = None
        if code in _INSTALLED_PACKS:
            return _INSTALLED_PACKS[code]

    return _COMBINED_DEFAULT


def _cache_path(cache_dir: Path, content_hash: str) -> Path:
    return cache_dir / f"{content_hash}.json"


def load_cached_ocr(cache_dir: Path, content_hash: str) -> Optional[dict]:
    path = _cache_path(cache_dir, content_hash)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_ocr_cache(cache_dir: Path, content_hash: str, *, detected_language: str, pages: list[str]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, content_hash)
    path.write_text(
        json.dumps({"detected_language": detected_language, "pages": pages}, indent=2),
        encoding="utf-8",
    )
    return path


def slice_single_page_pdf(content: bytes, page_index: int) -> bytes:
    """Return a standalone one-page PDF (bytes) for the given 0-based page index."""
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def run(
    *,
    zotero_client,
    extractor,
    library_id: str,
    library_type: str,
    attachment_specs: list[dict],
    max_items: Optional[int],
    cache_dir: Path,
    progress_callback: Callable[[float, str], None],
) -> dict:
    """Core logic for script 2 (ocr_attachments). `attachment_specs` is
    typically script 1's `needs_ocr: true` output list. See design spec §6.
    """
    specs = attachment_specs[:max_items] if max_items is not None else attachment_specs
    results: list[dict] = []
    total = len(specs) or 1

    for i, spec in enumerate(specs):
        item_key = spec["item_key"]
        attachment_key = spec["attachment_key"]
        progress_callback(i / total, f"OCR-ing {item_key} ({i + 1}/{total})")

        # NOTE: get_attachment_file's URL is /items/{key}/file — it needs the
        # PDF attachment's OWN key, not the containing book item's key (same
        # bug pattern fixed in chapter_segmentation.run() for script 1).
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

        item = await zotero_client.get_item(library_id, item_key, library_type=library_type)
        language = detect_language(item["data"].get("language"), item["data"].get("title", ""))

        reader = PdfReader(io.BytesIO(file_bytes))
        page_texts: list[str] = []
        for page_index in range(len(reader.pages)):
            single_page_bytes = slice_single_page_pdf(file_bytes, page_index)
            chunks = await extractor.extract_and_chunk(single_page_bytes, "application/pdf", ocr_language=language)
            page_texts.append(" ".join(c.text for c in chunks))

        cache_path = save_ocr_cache(cache_dir, content_hash, detected_language=language, pages=page_texts)
        results.append({
            "item_key": item_key,
            "attachment_key": attachment_key,
            "detected_language": language,
            "ocr_succeeded": True,
            "char_count": sum(len(p) for p in page_texts),
            "cache_path": str(cache_path),
        })

    progress_callback(1.0, "Done")
    return {"results": results}
