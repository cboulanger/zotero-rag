"""Language-aware OCR for attachments lacking a text layer (script 2:
ocr_attachments). See design spec §6.

Slices the book PDF page-by-page (pypdf) and OCRs each page individually
via the Kreuzberg sidecar, rather than sending the whole PDF as one
request — this guarantees a clean 1:1 page-index<->text mapping (matching
pypdf's own indexing used elsewhere in chapter_segmentation.py), avoiding
any ambiguity in how Kreuzberg's own chunking groups multi-page text.
"""

import io
import json
from pathlib import Path
from typing import Optional

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
