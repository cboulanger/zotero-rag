"""Zotero-specific orchestration for script 1 (analyze_book_chapters) --
scans `book`-type items in the library (or the explicit `item_keys` list),
skips already-linked ones unless `relink`, downloads each PDF attachment,
and delegates the actual chapter-boundary detection to the standalone
chapter_segmentation package's analyze_attachment_with_strategies. See
docs/superpowers/specs/2026-08-06-chapter-segmentation-extraction-design.md
and 2026-08-01-zotero-library-sync-cache-design.md for the ZoteroLibraryCache
used below to fetch the item list.

If a PDF has no extractable text layer and `ocr_cache_dir` is given, checks
chapter_ocr.py's on-disk cache -- keyed by the same content hash -- for
already-OCR'd page text before falling back to reporting `needs_ocr: True`.
This is what lets a re-run of this script pick up chapters from a book that
was OCR'd since the previous run.
"""

import hashlib
from pathlib import Path
from typing import Callable, Optional

import httpx

from backend.config.settings import get_settings
from backend.services import review_queue_store
from backend.services.chapter_link_store import parse_links
from backend.services.llm import LLMService
from backend.zotero.library_cache import ZoteroLibraryCache
from chapter_segmentation.evidence.crossref_strategy import CrossrefMetadataStrategy
from chapter_segmentation.evidence.zotero_catalog_strategy import ZoteroCatalogMetadataStrategy
from chapter_segmentation.ocr import load_cached_ocr
from chapter_segmentation.segmentation import (
    analyze_attachment_with_strategies,
    build_book_context,
    extract_page_texts_for_analysis,
    load_cached_analysis,
    pages_need_ocr,
    save_analysis_cache,
)


async def run(
    *,
    zotero_client,
    library_id: str,
    library_type: str,
    slug: str,
    item_keys: Optional[list[str]],
    max_items: Optional[int],
    relink: bool,
    progress_callback: Callable[[float, str], None],
    llm_service: Optional[LLMService] = None,
    ocr_cache_dir: Optional[Path] = None,
    enable_crossref: bool = True,
    crossref_cache_dir: Optional[Path] = None,
    crossref_contact_email: Optional[str] = None,
    zotero_cache_dir: Optional[Path] = None,
) -> dict:
    """Core logic for script 1 (analyze_book_chapters). See module docstring."""
    if zotero_cache_dir is None:
        zotero_cache_dir = get_settings().zotero_cache_path
    zotero_cache = ZoteroLibraryCache(
        client=zotero_client,
        library_id=library_id,
        library_type=library_type,
        cache_path=zotero_cache_dir,
    )
    try:
        items = await zotero_cache.get_all_items()
    finally:
        zotero_cache.close()
    books = [i for i in items if i["data"].get("itemType") == "book"]
    if item_keys is not None:
        wanted = set(item_keys)
        books = [b for b in books if b["data"]["key"] in wanted]
    if not relink:
        books = [b for b in books if not parse_links(b["data"].get("extra", "")).contains]
    if max_items is not None:
        books = books[:max_items]

    book_sections_by_title: dict[str, list[dict]] = {}
    for item in items:
        if item["data"].get("itemType") != "bookSection":
            continue
        if parse_links(item["data"].get("extra", "")).contained_by:
            continue
        title = item["data"].get("bookTitle", "").strip()
        if title:
            book_sections_by_title.setdefault(title, []).append(item)
    zotero_catalog_strategy = ZoteroCatalogMetadataStrategy(book_sections_by_title)

    if crossref_cache_dir is None:
        crossref_cache_dir = get_settings().crossref_cache_path
    if crossref_contact_email is None:
        crossref_contact_email = get_settings().crossref_contact_email

    attachments_out: list[dict] = []
    total = len(books) or 1
    analysis_mode = "strategies_llm" if llm_service is not None else "strategies"

    async with httpx.AsyncClient() as http_client:
        crossref_strategy = (
            CrossrefMetadataStrategy(http_client, crossref_cache_dir, crossref_contact_email)
            if enable_crossref else None
        )
        for i, book in enumerate(books):
            item_key = book["data"]["key"]
            progress_callback(i / total, f"Analyzing {item_key} ({i + 1}/{total})")

            children = await zotero_client.get_item_children(library_id, item_key, library_type=library_type)
            pdf_attachments = [c for c in children if c["data"].get("contentType") == "application/pdf"]
            if not pdf_attachments:
                continue
            attachment_key = pdf_attachments[0]["data"]["key"]
            attachment_version = pdf_attachments[0]["data"].get("version", 0)

            if ocr_cache_dir is not None:
                cached_entry = load_cached_analysis(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode)
                if cached_entry is not None:
                    attachments_out.append(cached_entry)
                    continue

            file_bytes = await zotero_client.get_attachment_file(library_id, attachment_key, library_type=library_type)
            if not file_bytes:
                continue

            pages, layout_mode_used = extract_page_texts_for_analysis(file_bytes)

            if pages_need_ocr(pages) and ocr_cache_dir is not None:
                content_hash = hashlib.sha256(file_bytes).hexdigest()
                cached = load_cached_ocr(ocr_cache_dir, content_hash)
                if cached is not None:
                    pages = cached["pages"]

            if pages_need_ocr(pages):
                attachments_out.append({
                    "item_key": item_key,
                    "attachment_key": attachment_key,
                    "has_text_layer": False,
                    "needs_ocr": True,
                })
                continue

            book_context = build_book_context(book["data"])
            analysis = await analyze_attachment_with_strategies(
                pages, file_bytes, book_context, zotero_catalog_strategy,
                crossref_strategy=crossref_strategy, llm_client=llm_service,
            )
            result_entry = {
                "item_key": item_key,
                "attachment_key": attachment_key,
                "has_text_layer": True,
                "needs_ocr": False,
                "layout_mode_used": layout_mode_used,
                **analysis,
            }
            if ocr_cache_dir is not None:
                save_analysis_cache(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode, result_entry)
            attachments_out.append(result_entry)

    ocr_entries = [
        {
            "queue_id": f"ocr:{a['attachment_key']}",
            "type": "ocr",
            "bucket": "review",
            "payload": {"book_key": a["item_key"], "attachment_key": a["attachment_key"]},
        }
        for a in attachments_out
        if a.get("needs_ocr")
    ]
    if ocr_entries:
        review_queue_store.upsert_many(get_settings().review_queue_path, slug, ocr_entries)

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
