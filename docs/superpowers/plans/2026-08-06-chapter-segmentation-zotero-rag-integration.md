# Chapter-Segmentation zotero-rag Integration — Implementation Plan (Part 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the chapter-segmentation engine/evaluation-harness files from zotero-rag (now living in the standalone `chapter-segmentation` package), add it as a dependency, and rewrite the two thin Zotero-integration orchestrators (`chapter_segmentation.run()`, `chapter_ocr.run()`) to delegate the actual analysis/OCR work to the package.

**Architecture:** `backend/services/chapter_segmentation.py` and `backend/services/chapter_ocr.py` **keep their file paths and public `run()` signatures** (so every caller — `backend/api/chapter_linking.py`, `scripts/analyze_book_chapters.py`, `scripts/ocr_attachments.py` — needs zero changes to its own call sites beyond two small param-name updates in `chapter_linking.py`), but their bodies shrink to pure Zotero/settings/review-queue orchestration, importing the actual analysis engine and OCR helpers from the installed `chapter_segmentation` package.

**Tech Stack:** Python 3.12, `uv`, `pytest`. Depends on `chapter-segmentation` v0.1.0+ (Part 1 of this plan).

**Prerequisite:** Part 1 (`docs/superpowers/plans/2026-08-06-chapter-segmentation-new-repo.md`) must be complete through its Task 16 (the `v0.1.0` tag pushed and verified installable) before starting here.

**Design spec:** `docs/superpowers/specs/2026-08-06-chapter-segmentation-extraction-design.md`.

---

## Before you start: the mock-patching subtlety this plan resolves

`backend/tests/test_chapter_segmentation.py`'s `TestRun` class patches five names by their string path, e.g. `unittest.mock.patch("backend.services.chapter_segmentation.load_cached_analysis", ...)`. After the split, whether that string still needs to point at `backend.services.chapter_segmentation` depends on **whether the orchestrator's own `run()` calls that name directly**, versus the name being called *inside* a function that itself now lives entirely in the external package:

| Patched name | Called directly by `run()`? | New patch target |
|---|---|---|
| `load_cached_analysis` | Yes (`run()` calls it itself) | unchanged: `backend.services.chapter_segmentation.load_cached_analysis` |
| `load_cached_ocr` | Yes | unchanged: `backend.services.chapter_segmentation.load_cached_ocr` |
| `save_analysis_cache` | Yes | unchanged: `backend.services.chapter_segmentation.save_analysis_cache` |
| `extract_page_texts_from_pdf_bytes` | No — called *inside* `extract_page_texts_for_analysis`, which now lives entirely in the package | `chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes` |
| `analyze_attachment_with_llm_fallback` | No — called *inside* `analyze_attachment_with_strategies`, which now lives entirely in the package | `chapter_segmentation.segmentation.analyze_attachment_with_llm_fallback` |

Task 6 applies exactly these two patch-target changes and leaves the other three untouched.

---

### Task 1: Add the dependency and remove now-transitive direct dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: Edit `[project.dependencies]`**

In `pyproject.toml`, remove these two lines (now pulled in transitively via `chapter-segmentation`):

```toml
    "rapidfuzz>=3.10.0",
    "langdetect>=1.0.9",
```

Add this line in their place (keep the list alphabetized/grouped the way the surrounding entries already are — insert near `pypdf` since it's related extraction tooling):

```toml
    "chapter-segmentation[kreuzberg] @ git+https://github.com/cboulanger/chapter-segmentation.git@v0.1.0",
```

`spacy` and `pypdf` stay in `[project.dependencies]` unchanged — `backend/services/chunking.py` and the RAG-indexing PDF-splitting path use them independently of chapter segmentation.

- [ ] **Step 2: Add `uv.toml` to `.gitignore`** (for the *optional* local-editable override described in Task 2 — never commit that override to the shared `pyproject.toml`, since a hardcoded sibling-directory path would break `uv sync` in CI and on every other developer's machine)

```bash
echo "uv.toml" >> .gitignore
```

- [ ] **Step 3: Resolve and lock**

```bash
uv lock
uv sync
```

Expected: resolution succeeds and installs `chapter-segmentation` from the tagged git commit. If it fails to resolve, re-run Part 1 Task 16 Step 4's standalone verification first to isolate whether the problem is the package itself or something zotero-rag-specific.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock .gitignore
git commit -m "build: depend on chapter-segmentation v0.1.0, drop rapidfuzz/langdetect as direct deps"
```

---

### Task 2 (optional, uncommitted): set up local editable iteration against a sibling checkout

Skip this task entirely if you don't have a local checkout of `chapter-segmentation` to iterate against — the git dependency from Task 1 is sufficient on its own.

- [ ] **Step 1: Clone the new repo as a sibling of zotero-rag** (if not already present from Part 1's work)

```bash
ls ../chapter-segmentation || git clone https://github.com/cboulanger/chapter-segmentation.git ../chapter-segmentation
```

- [ ] **Step 2: Create an uncommitted `uv.toml`** in the zotero-rag repo root

```toml
[tool.uv.sources]
chapter-segmentation = { path = "../chapter-segmentation", editable = true }
```

- [ ] **Step 3: Re-sync and verify the editable install**

```bash
uv sync
uv run python -c "import chapter_segmentation.segmentation as m; print(m.__file__)"
```

Expected: the printed path points into `../chapter-segmentation/src/chapter_segmentation/segmentation.py`, not a site-packages cache directory — confirming edits to the sibling checkout take effect immediately without re-locking.

- [ ] **Step 4: Do not commit `uv.toml`**

```bash
git status
```

Confirm `uv.toml` shows as untracked-but-ignored (Task 1 Step 2 already added it to `.gitignore`), not staged.

---

### Task 3: Rewrite `backend/services/chapter_segmentation.py` as a thin orchestrator

**Files:**
- Modify: `backend/services/chapter_segmentation.py` (full rewrite — shrinks from ~1590 lines to ~150)

- [ ] **Step 1: Replace the entire file contents**

```python
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
```

The only two behavioral differences from the pre-split file, both intentional: (1) `llm_client=llm_service` at the `analyze_attachment_with_strategies` call site (the package renamed its own parameter; `run()`'s own external signature keeps `llm_service` unchanged since `backend/api/chapter_linking.py` calls it by that keyword), and (2) every pure detection function now comes from the `chapter_segmentation` package instead of being defined in this file.

- [ ] **Step 2: Verify it imports cleanly**

```bash
uv run python -c "from backend.services.chapter_segmentation import run; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add backend/services/chapter_segmentation.py
git commit -m "refactor: chapter_segmentation.py becomes a thin orchestrator over the chapter_segmentation package"
```

(Don't push yet — Task 4-7 need to land before the test suite is green again; push once per this plan's final task, or after each task if you prefer more granular history. Either is fine as long as you don't leave the branch in a broken state on a shared remote for long.)

---

### Task 4: Rewrite `backend/services/chapter_ocr.py` as a thin orchestrator

**Files:**
- Modify: `backend/services/chapter_ocr.py` (full rewrite — shrinks from ~192 lines to ~65)

- [ ] **Step 1: Replace the entire file contents**

```python
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

        # NOTE: get_attachment_file's URL is /items/{key}/file — it needs the
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
```

The parameter formerly named `extractor` (a `DocumentExtractor`) is renamed `ocr_backend` (an `OcrBackend`) — Task 5 updates its two callers in `chapter_linking.py` to match.

- [ ] **Step 2: Verify it imports cleanly**

```bash
uv run python -c "from backend.services.chapter_ocr import run; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add backend/services/chapter_ocr.py
git commit -m "refactor: chapter_ocr.py becomes a thin orchestrator over chapter_segmentation.ocr, extractor param renamed to ocr_backend"
```

---

### Task 5: Update `backend/api/chapter_linking.py`'s two OCR call sites

**Files:**
- Modify: `backend/api/chapter_linking.py:20-31` (imports), `:158-183` (`start_ocr`), `:361-373` (`_apply_entry`'s ocr branch)

- [ ] **Step 1: Swap the extraction-factory import for the Kreuzberg OCR backend import**

Replace this line:

```python
from backend.services.extraction import create_document_extractor
```

with:

```python
from chapter_segmentation.ocr_backends.kreuzberg import KreuzbergOcrBackend
```

- [ ] **Step 2: Fix `start_ocr`**

Replace:

```python
    extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
```

with:

```python
    ocr_backend = KreuzbergOcrBackend(kreuzberg_url=get_settings().kreuzberg_url)
```

and, a few lines below, replace the `ocr_run(...)` call's `extractor=extractor,` keyword argument with `ocr_backend=ocr_backend,`.

- [ ] **Step 3: Fix `_apply_entry`'s `ocr` branch**

Same two changes inside the `if entry["type"] == "ocr":` block: replace `extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)` with `ocr_backend = KreuzbergOcrBackend(kreuzberg_url=get_settings().kreuzberg_url)`, and the `ocr_run(...)` call's `extractor=extractor,` with `ocr_backend=ocr_backend,`.

- [ ] **Step 4: Verify no remaining reference to `create_document_extractor` or a bare `extractor=` in this file**

```bash
grep -n "create_document_extractor\|extractor=" backend/api/chapter_linking.py
```

Expected: no output.

- [ ] **Step 5: Verify the module still imports**

```bash
uv run python -c "import backend.api.chapter_linking; print('OK')"
```

- [ ] **Step 6: Commit**

```bash
git add backend/api/chapter_linking.py
git commit -m "fix: chapter_linking.py's OCR endpoints construct a KreuzbergOcrBackend instead of a DocumentExtractor"
```

---

### Task 6: Fix `backend/services/chapter_retrofit.py`'s import

**Files:**
- Modify: `backend/services/chapter_retrofit.py:13`

- [ ] **Step 1: Edit the import**

Replace:

```python
from backend.services.chapter_common import year_from_date
```

with:

```python
from chapter_segmentation.common import year_from_date
```

- [ ] **Step 2: Verify**

```bash
uv run python -c "import backend.services.chapter_retrofit; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add backend/services/chapter_retrofit.py
git commit -m "fix: chapter_retrofit.py imports year_from_date from the chapter_segmentation package"
```

(`backend/services/chapter_upload.py` and `backend/services/chapter_link_store.py` need no changes — neither imports anything that moved, confirmed while researching this plan.)

---

### Task 7: Rewrite `backend/tests/test_chapter_segmentation.py` down to `TestRun` only

**Files:**
- Modify: `backend/tests/test_chapter_segmentation.py` (full rewrite — shrinks from ~1554 lines to ~460; every other test class in the original file tested a pure function that moved to the package, where it already has a home per Part 1 Task 10)

- [ ] **Step 1: Replace the entire file contents**

```python
"""Unit tests for backend.services.chapter_segmentation (the Zotero-specific
orchestrator -- the actual chapter-boundary detection engine now lives in
the standalone chapter_segmentation package; its own tests live there)."""

import asyncio
import io as _io
import tempfile
import unittest
import unittest.mock
from pathlib import Path as _TestPath
from unittest.mock import AsyncMock, MagicMock

from pypdf import PdfWriter as _PdfWriter

from backend.config.settings import get_settings, reset_settings
from backend.services.chapter_segmentation import run as analyze_run
from backend.services.review_queue_store import get_entry


def _blank_pdf(num_pages: int) -> bytes:
    writer = _PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_with_outline(num_pages: int, entries: list[tuple[str, int]]) -> bytes:
    writer = _PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in entries:
        writer.add_outline_item(title, page_number)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# Repeated (not a single short line) so this filler page clears
# pages_need_ocr's per-page "substantial" (>500 chars) and "not degenerate"
# (>=3 newlines) thresholds -- a single short line reads as OCR-shaped input
# and short-circuits run() into the needs_ocr branch before any chapter
# segmentation strategy runs.
_FILLER = "Unrelated body filler text, nothing chapter-related in this passage at all.\n" * 8

_TWO_CHAPTER_PAGES = [
    _FILLER,  # 0
    _FILLER,  # 1
    _FILLER,  # 2
    _FILLER,  # 3
    _FILLER,  # 4
    "Introduction\nJane Author\n\nBody text opening the chapter.\n\n1",  # 5
    "...continued introduction text with real body content here.\n\n2",  # 6
    "...more continued introduction text with real body content.\n\n3",  # 7
    "...final continued introduction text with real body content.\n\n4",  # 8
    _FILLER,  # 9
    _FILLER,  # 10
    _FILLER,  # 11
    "Comparing Citation Styles\n\nJohn Smith\n\nBody text opening this chapter.\n\n5",  # 12
    "...continued citation styles text with real body content here.\n\n6",  # 13
    "...more continued citation styles text with real body content.\n\n7",  # 14
    "...final continued citation styles text with real body content.\n\n8",  # 15
    _FILLER,  # 16
    _FILLER,  # 17
    _FILLER,  # 18
    _FILLER,  # 19
]

_SUBSTANTIAL_FILLER_PAGE = "Just filler prose, no TOC pattern here at all.\n" * 12


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"
        get_settings().zotero_cache_path = _TestPath(self.tmp.name) / "zotero_cache"

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_skips_already_linked_book(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0001", "version": 1, "data": {"key": "BOOK0001", "itemType": "book", "extra": "X-Contains: groups/1:CH01"}},
        ]
        progress_calls = []
        result = asyncio.run(analyze_run(
            zotero_client=zotero_client,
            library_id="1",
            library_type="group",
            slug="groups/1",
            item_keys=None,
            max_items=None,
            relink=False,
            progress_callback=lambda p, m: progress_calls.append((p, m)),
        ))
        self.assertEqual(result["attachments"], [])
        zotero_client.get_item_children.assert_not_called()

    def test_processes_unlinked_book(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0002", "version": 1, "data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0001", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[_SUBSTANTIAL_FILLER_PAGE],
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        self.assertEqual(len(result["attachments"]), 1)
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0002")
        self.assertTrue(result["attachments"][0]["has_text_layer"])
        zotero_client.get_attachment_file.assert_called_once_with("1", "ATT0001", library_type="group")

    def test_needs_ocr_attachment_is_upserted_into_review_queue(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK9", "version": 1, "data": {"key": "BOOK9", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT9", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 no text layer"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[""],
        ):
            asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))

        entry = get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT9")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "ocr")
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["payload"], {"book_key": "BOOK9", "attachment_key": "ATT9"})

    def test_uses_llm_fallback_when_llm_service_provided(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0003", "version": 1, "data": {"key": "BOOK0003", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0002", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"
        fake_llm = MagicMock()

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[_SUBSTANTIAL_FILLER_PAGE],
        ), unittest.mock.patch(
            "chapter_segmentation.segmentation.analyze_attachment_with_llm_fallback",
            new_callable=AsyncMock,
            return_value={"total_pdf_pages": 1, "segmentation_confidence": "low", "chapters": [], "diagnostics": {}},
        ) as mock_fallback:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                llm_service=fake_llm,
            ))
        mock_fallback.assert_called_once()
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0003")

    def test_reads_ocr_cache_when_no_text_layer_and_cache_hit(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0004", "version": 1, "data": {"key": "BOOK0004", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0003", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["", ""],  # no text layer -- scanned PDF
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_ocr",
            return_value={"detected_language": "eng", "pages": [_SUBSTANTIAL_FILLER_PAGE]},
        ) as mock_load_cache, unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis",
            return_value=None,
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.save_analysis_cache",
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                ocr_cache_dir=unittest.mock.ANY,
            ))
        mock_load_cache.assert_called_once()
        self.assertTrue(result["attachments"][0]["has_text_layer"])
        self.assertFalse(result["attachments"][0]["needs_ocr"])

    def test_still_reports_needs_ocr_when_no_cache_dir_given(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0005", "version": 1, "data": {"key": "BOOK0005", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0004", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["", ""],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_ocr",
        ) as mock_load_cache:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        mock_load_cache.assert_not_called()
        self.assertFalse(result["attachments"][0]["has_text_layer"])
        self.assertTrue(result["attachments"][0]["needs_ocr"])

    def test_analysis_cache_hit_skips_download(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0006", "version": 1, "data": {"key": "BOOK0006", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0005", "itemType": "attachment", "contentType": "application/pdf", "version": 7}},
        ]
        cached_entry = {
            "item_key": "BOOK0006", "attachment_key": "ATT0005", "has_text_layer": True,
            "needs_ocr": False, "total_pdf_pages": 3, "segmentation_confidence": "high",
            "chapters": [], "diagnostics": {},
        }

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis",
            return_value=cached_entry,
        ) as mock_load:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                ocr_cache_dir=unittest.mock.ANY,
            ))
        mock_load.assert_called_once_with(unittest.mock.ANY, "BOOK0006", "ATT0005", 7, "strategies")
        zotero_client.get_attachment_file.assert_not_called()
        self.assertEqual(result["attachments"], [cached_entry])

    def test_analysis_cache_miss_saves_result(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0007", "version": 1, "data": {"key": "BOOK0007", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0006", "itemType": "attachment", "contentType": "application/pdf", "version": 3}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[_SUBSTANTIAL_FILLER_PAGE],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis", return_value=None,
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.save_analysis_cache",
        ) as mock_save:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                ocr_cache_dir=unittest.mock.ANY,
            ))
        mock_save.assert_called_once()
        saved_args = mock_save.call_args.args
        self.assertEqual(saved_args[1:4], ("BOOK0007", "ATT0006", 3))
        self.assertEqual(saved_args[4], "strategies")
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0007")

    def test_no_cache_dir_never_touches_analysis_cache(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0008", "version": 1, "data": {"key": "BOOK0008", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0007", "itemType": "attachment", "contentType": "application/pdf", "version": 1}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis",
        ) as mock_load, unittest.mock.patch(
            "backend.services.chapter_segmentation.save_analysis_cache",
        ) as mock_save:
            asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        mock_load.assert_not_called()
        mock_save.assert_not_called()

    def test_uses_outline_strategy_when_present(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK3", "version": 1, "data": {"key": "BOOK3", "itemType": "book", "extra": "", "title": "Some Book"}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT3", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _pdf_with_outline(20, [("Introduction", 5), ("Comparing Citation Styles", 12)])
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=_TWO_CHAPTER_PAGES,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        chapters = result["attachments"][0]["chapters"]
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["source"], "outline")

    def test_builds_zotero_catalog_index_from_unlinked_book_sections(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK4", "version": 1, "data": {"key": "BOOK4", "itemType": "book", "extra": "", "title": "Some Book"}},
            {"key": "CH1", "version": 1, "data": {
                "key": "CH1", "itemType": "bookSection", "extra": "",
                "title": "Introduction", "bookTitle": "Some Book", "pages": "1-20",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Author"}],
            }},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT4", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(8)  # no outline -- forces content-search localization
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=_TWO_CHAPTER_PAGES,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        chapters = result["attachments"][0]["chapters"]
        titles = {c["title"]: c for c in chapters}
        self.assertIn("Introduction", titles)
        self.assertEqual(titles["Introduction"]["source"], "zotero_catalog")

    def test_already_linked_book_section_is_not_offered_as_candidate(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK5", "version": 1, "data": {"key": "BOOK5", "itemType": "book", "extra": "", "title": "Some Book"}},
            {"key": "CH2", "version": 1, "data": {
                "key": "CH2", "itemType": "bookSection",
                "extra": "X-Contained-By: groups/1:OTHERBOOK",
                "title": "Introduction", "bookTitle": "Some Book", "pages": "1-20",
                "creators": [],
            }},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT5", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(3)
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all.\n" * 12] * 3,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        self.assertEqual(result["attachments"][0]["diagnostics"]["strategies_used"], [])

    def test_enable_crossref_false_skips_crossref_entirely(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK6", "version": 1, "data": {"key": "BOOK6", "itemType": "book", "extra": "", "title": "Some Book", "ISBN": "9783031466373"}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT6", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(3)
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all.\n" * 12] * 3,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        self.assertIsNone(result["attachments"][0]["diagnostics"]["crossref_isbn_used"])


if __name__ == "__main__":
    unittest.main()
```

Note the two patch-target changes applied per this plan's "Before you start" table: `extract_page_texts_from_pdf_bytes` and `analyze_attachment_with_llm_fallback` now target `chapter_segmentation.segmentation.*`; `load_cached_ocr`, `load_cached_analysis`, and `save_analysis_cache` are unchanged at `backend.services.chapter_segmentation.*`. Each `import asyncio` that used to appear inline at the top of every test method was hoisted to a single module-level import — the original file's per-method `import asyncio` was redundant repetition, not a meaningful pattern worth preserving.

- [ ] **Step 2: Run it**

```bash
uv run pytest backend/tests/test_chapter_segmentation.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_chapter_segmentation.py
git commit -m "test: trim test_chapter_segmentation.py to TestRun only, fix patch targets for the moved analysis functions"
```

---

### Task 8: Rewrite `backend/tests/test_chapter_ocr.py` for the `ocr_backend` parameter

**Files:**
- Modify: `backend/tests/test_chapter_ocr.py` (full rewrite — shrinks from 214 lines to ~115; `TestDetectLanguage`/`TestOcrCache`/`TestOcrPdfPages` moved to the package per Part 1 Task 9, where they already have a home)

- [ ] **Step 1: Replace the entire file contents**

```python
"""Unit tests for backend.services.chapter_ocr (the Zotero-specific batch
OCR orchestrator -- the OCR engine itself now lives in the standalone
chapter_segmentation package; its own tests live there)."""

import asyncio
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from chapter_segmentation.ocr import save_ocr_cache

from backend.services.chapter_ocr import run as ocr_run


class TestOcrRun(unittest.TestCase):
    def test_ocrs_each_attachment_and_caches_result(self):
        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 one page fake"
        zotero_client.get_item.return_value = {"data": {"title": "Einführung in die Zitierweise", "language": ""}}

        ocr_backend = AsyncMock()
        ocr_backend.ocr_pdf_pages.return_value = ["OCR'd page text"]

        with TemporaryDirectory() as tmp:
            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                ocr_backend=ocr_backend,
                library_id="1",
                library_type="group",
                attachment_specs=[{"item_key": "BOOK1", "attachment_key": "ATT1"}],
                max_items=None,
                cache_dir=Path(tmp),
                progress_callback=lambda p, m: None,
            ))
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["ocr_succeeded"])
        self.assertEqual(result["results"][0]["detected_language"], "deu")
        # Regression guard: get_attachment_file must be called with the
        # attachment's OWN key (ATT1), not the book item's key (BOOK1).
        zotero_client.get_attachment_file.assert_called_once_with("1", "ATT1", library_type="group")
        ocr_backend.ocr_pdf_pages.assert_awaited_once_with(b"%PDF-1.4 one page fake", language="deu")

    def test_cache_hit_short_circuits_before_fetching_item(self):
        fixture_bytes = b"%PDF-1.4 one page fake"
        content_hash = hashlib.sha256(fixture_bytes).hexdigest()

        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.return_value = fixture_bytes

        ocr_backend = AsyncMock()

        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_ocr_cache(cache_dir, content_hash, detected_language="fra", pages=["cached page one"])

            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                ocr_backend=ocr_backend,
                library_id="1",
                library_type="group",
                attachment_specs=[{"item_key": "BOOK1", "attachment_key": "ATT1"}],
                max_items=None,
                cache_dir=cache_dir,
                progress_callback=lambda p, m: None,
            ))

        self.assertEqual(len(result["results"]), 1)
        entry = result["results"][0]
        self.assertTrue(entry["ocr_succeeded"])
        self.assertEqual(entry["detected_language"], "fra")
        self.assertEqual(entry["char_count"], len("cached page one"))
        # Cache hit must short-circuit before ever fetching the item or OCR-ing.
        zotero_client.get_item.assert_not_called()
        ocr_backend.ocr_pdf_pages.assert_not_called()

    def test_one_attachment_failure_does_not_abort_the_batch(self):
        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.side_effect = [
            b"%PDF-1.4 fake book one",
            b"%PDF-1.4 fake book two",
        ]
        zotero_client.get_item.return_value = {"data": {"title": "", "language": "en"}}

        ocr_backend = AsyncMock()
        ocr_backend.ocr_pdf_pages.side_effect = [
            RuntimeError("Kreuzberg sidecar timeout"),
            ["OCR'd page text"],
        ]

        with TemporaryDirectory() as tmp:
            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                ocr_backend=ocr_backend,
                library_id="1",
                library_type="group",
                attachment_specs=[
                    {"item_key": "BOOK1", "attachment_key": "ATT1"},
                    {"item_key": "BOOK2", "attachment_key": "ATT2"},
                ],
                max_items=None,
                cache_dir=Path(tmp),
                progress_callback=lambda p, m: None,
            ))

        self.assertEqual(len(result["results"]), 2)
        first, second = result["results"]
        self.assertFalse(first["ocr_succeeded"])
        self.assertIn("error", first)
        self.assertTrue(second["ocr_succeeded"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

```bash
uv run pytest backend/tests/test_chapter_ocr.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_chapter_ocr.py
git commit -m "test: rewrite test_chapter_ocr.py for the ocr_backend parameter, drop tests that moved to the package"
```

---

### Task 9: Delete the moved files from zotero-rag

**Files:** deletions only.

- [ ] **Step 1: Remove the moved source, evaluation, script, and test files**

```bash
git rm -r backend/services/chapter_common.py
git rm -r backend/services/chapter_evidence/
git rm -r backend/evaluation/
git rm -r scripts/evaluation_redaction/
git rm scripts/ocr_evaluation_pdfs.py
git rm scripts/generate_public_evaluation_cache.py
git rm scripts/fetch_evaluation_pdfs.py
git rm scripts/ground_truth_helper.py
git rm scripts/evaluate_chapter_segmentation_strategies.py
git rm scripts/evaluate_chapter_segmentation_llm_fallback.py
git rm backend/tests/test_chapter_common.py
git rm backend/tests/test_chapter_evidence_types.py
git rm backend/tests/test_chapter_evidence_fusion.py
git rm backend/tests/test_chapter_evidence_outline.py
git rm backend/tests/test_chapter_evidence_crossref.py
git rm backend/tests/test_chapter_evidence_zotero_catalog.py
git rm backend/tests/test_chapter_segmentation_accuracy.py
git rm backend/tests/test_chapter_segmentation_strategies.py
git rm backend/tests/test_evaluation_harness.py
git rm backend/tests/test_evaluation_redaction.py
git rm backend/tests/test_public_evaluation_cache_parity.py
```

- [ ] **Step 2: Verify nothing still references the deleted paths**

```bash
grep -rln "backend\.evaluation\|backend\.services\.chapter_common\|backend\.services\.chapter_evidence\|scripts\.evaluation_redaction" backend/ scripts/ plugin/ 2>/dev/null
```

Expected: no output. (This must be empty — if anything shows up, it's a caller Tasks 1-8 missed; fix it before proceeding rather than deleting out from under a live reference.)

- [ ] **Step 3: Check for now-dangling references in root-level docs**

```bash
grep -rln "backend/evaluation/book-segmentation\|backend/services/chapter_common\|backend/services/chapter_evidence\|scripts/evaluation_redaction\|scripts/ocr_evaluation_pdfs\|scripts/generate_public_evaluation_cache\|scripts/fetch_evaluation_pdfs\|scripts/ground_truth_helper\|scripts/evaluate_chapter_segmentation" \
  docs/ CLAUDE.md 2>/dev/null | grep -v docs/history | grep -v docs/superpowers/specs | grep -v docs/superpowers/plans
```

For each hit outside `docs/history/`, `docs/superpowers/specs/`, and `docs/superpowers/plans/` (which are historical/planning records and explicitly exempt from this kind of edit per this project's own `CLAUDE.md` documentation rules), update the reference to point at `github.com/cboulanger/chapter-segmentation` instead, or remove it if it's describing a workflow that no longer exists in this repo at all.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: delete chapter-segmentation engine/evaluation files, now maintained in cboulanger/chapter-segmentation"
```

---

### Task 10: Run the full test suite and the container/startup smoke tests

**Files:** none — verification only.

- [ ] **Step 1: Run the default test suite**

```bash
uv sync
uv run pytest -v
```

Expected: all pass. Pay particular attention to `backend/tests/test_chapter_linking_api.py`, `test_chapter_linking_e2e.py`, `test_chapter_retrofit.py`, `test_chapter_upload.py`, `test_chapter_link_store.py`, `test_admin_pages.py` — none of these files' own source needed edits in this plan, but they exercise code paths (`chapter_linking.py`, `chapter_retrofit.py`) that did change; a regression here would mean Task 5 or 6 introduced a behavior change, not just an import rename.

- [ ] **Step 2: Run the chapter-segmentation-adjacent integration tests explicitly**

```bash
uv run pytest backend/tests/test_chapter_segmentation.py backend/tests/test_chapter_ocr.py backend/tests/test_chapter_linking_api.py backend/tests/test_chapter_linking_e2e.py backend/tests/test_chapter_retrofit.py backend/tests/test_chapter_upload.py -v
```

Expected: all pass.

- [ ] **Step 3: Run the container smoke test and startup sequence test**

Per this project's `CLAUDE.md`, any change touching dependency resolution or the backend's import graph warrants re-running these:

```bash
uv run pytest -m container -v -s
uv run python scripts/test_startup_sequence.py
```

Expected: both pass. The container smoke test in particular confirms the built image can actually resolve and install the `chapter-segmentation` git dependency from inside a fresh container build (network access to GitHub required during the image build step) — a failure here that doesn't reproduce in Step 1 usually means a `podman build` layer isn't set up to reach `github.com`, not a code bug.

- [ ] **Step 4: Manually verify the CLI scripts that were never touched still work**

```bash
uv run python scripts/analyze_book_chapters.py --help
uv run python scripts/ocr_attachments.py --help
uv run python scripts/retrofit_chapter_links.py --help
uv run python scripts/upload_chapters.py --help
```

Expected: each prints its normal `--help` output with no import error — confirming `chapter_segmentation.run`, `chapter_ocr.run`, `chapter_retrofit.run`, `chapter_upload.run` are all still importable at their original paths with their original signatures, exactly as this plan's architecture section promised.

- [ ] **Step 5: Push**

```bash
git push
```

---

**Both parts of this extraction are now complete.** The chapter-segmentation engine and evaluation harness live in `github.com/cboulanger/chapter-segmentation`, versioned and consumed as a `uv` git dependency; zotero-rag retains only the Zotero-specific persistence, review-queue, and API/plugin code, plus two thin orchestrators. The remaining in-flight feature-branch work (chapter linking, review UI, retrofit, upload) continues on top of this new foundation.
