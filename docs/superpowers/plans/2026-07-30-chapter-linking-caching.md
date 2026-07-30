# Chapter-Linking Scripts Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate redundant Zotero fetches and PDF downloads across the four chapter-segmentation/linking scripts (`docs/chapter-segmentation.md`), most importantly letting a retrofit-link `--commit` run reuse a prior dry run's matches instead of re-fetching and re-matching the entire library (today ~11 minutes for a small library, dominated by the full-library `everything()` call).

**Architecture:** Three independent, additive changes, each following a caching pattern the codebase already uses elsewhere (`chapter_ocr.py`'s content-hash cache is the reference implementation):
1. Script 4 (`chapter_upload.py`): stop re-downloading the same book PDF once per chapter — download once per book.
2. Script 1 (`chapter_segmentation.py`): cache each book's analysis result keyed by the PDF attachment's own Zotero `version`, so an unchanged attachment skips download + analysis entirely on a re-run.
3. Script 3 (`chapter_retrofit.py`): split the matching logic (`find_matches`) from the writing logic (`commit_links`), and let a `--commit` run accept a prior dry run's `would_link` list (via `--input`, mirroring the CLI convention scripts 1→4 already use) to skip the expensive full-library fetch entirely.

Each task produces working, independently-testable software — no task depends on a later one being complete to pass its own tests.

**Tech Stack:** Python 3.12, `uv run pytest` (unittest-style test classes), `pyzotero`, FastAPI (Pydantic request models).

---

## File Structure

- Modify: `backend/services/chapter_upload.py` — hoist the per-chapter PDF download to per-book.
- Modify: `backend/services/chapter_segmentation.py` — add `load_cached_analysis`/`save_analysis_cache`, wire into `run()`.
- Modify: `backend/services/chapter_retrofit.py` — extract `find_matches()` and `commit_links()` out of `run()`; add `would_link` param to `run()`.
- Modify: `scripts/retrofit_chapter_links.py` — add `--input` flag.
- Modify: `backend/api/chapter_linking.py` — add `would_link` field to `RetrofitLinkRequest`.
- Modify: `docs/chapter-segmentation.md` — document the new `--input` replay flow.
- Test: `backend/tests/test_chapter_upload.py`, `backend/tests/test_chapter_segmentation.py`, `backend/tests/test_chapter_retrofit.py`, `backend/tests/test_chapter_linking_api.py`, `backend/tests/test_chapter_linking_e2e.py`.

---

### Task 1: Script 4 — download the book PDF once per book, not once per chapter

**Files:**
- Modify: `backend/services/chapter_upload.py:162-190`
- Test: `backend/tests/test_chapter_upload.py`

Today, `run()`'s per-chapter loop calls `zotero_read_client.get_attachment_file(...)` inside the loop body — a book with N confident chapters downloads its own (often large) PDF N times.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_chapter_upload.py`, inside `class TestUploadRun`:

```python
    def test_commit_downloads_book_pdf_once_for_multiple_chapters(self):
        book_item, analysis = self._book_and_analysis()
        analysis["chapters"].append({
            "title": "Second Chapter", "authors": ["Jane Doe"], "pdf_start_index": 5,
            "pdf_end_index": 7, "citation_pages": "70-90", "confidence": 0.95, "page_mapping_confidence": "high",
        })
        zot = MagicMock()
        zot.item.return_value = book_item

        def item_template_side_effect(item_type, **kwargs):
            if item_type == "attachment":
                return {"itemType": "attachment", "linkMode": kwargs.get("linkmode", ""), "title": "",
                        "filename": "", "contentType": "", "parentItem": ""}
            return {"itemType": "bookSection", "title": "", "bookTitle": "",
                    "publisher": "", "place": "", "date": "", "ISBN": "", "language": "",
                    "pages": "", "creators": []}

        zot.item_template.side_effect = item_template_side_effect
        zot.create_items.side_effect = [
            {"successful": {"0": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}}},
            {"successful": {"0": {"key": "ATT1", "data": {"key": "ATT1"}}}},
            {"successful": {"0": {"key": "CHAP2", "data": {"key": "CHAP2", "extra": ""}}}},
            {"successful": {"0": {"key": "ATT2", "data": {"key": "ATT2"}}}},
        ]
        zot.upload_attachments.return_value = {"success": [{"key": "ATT1"}], "failure": [], "unchanged": []}
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []
        read_client = MagicMock()
        read_client.get_attachment_file = unittest.mock.AsyncMock(return_value=b"%PDF-1.4 fake")

        with patch("backend.services.chapter_upload.slice_pdf_range", return_value=b"sliced bytes"):
            result = asyncio.run(upload_run(
                zotero_write_client=zot, zotero_read_client=read_client,
                slug="groups/1", analyses=[analysis], commit=True, confidence_threshold=0.8,
                target_collection="Book Chapters", max_items=None,
            ))

        self.assertEqual(len(result["created"]), 2)
        read_client.get_attachment_file.assert_called_once_with("1", "ATT1", library_type="group")
```

`backend/tests/test_chapter_upload.py` already imports `unittest.mock` implicitly via `from unittest.mock import MagicMock, patch` — this test additionally needs `unittest.mock.AsyncMock`, which is available via the already-imported `unittest` module (used the same way in the existing `test_commit_creates_and_links` test above it).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_upload.py::TestUploadRun::test_commit_downloads_book_pdf_once_for_multiple_chapters -v`
Expected: FAIL — `read_client.get_attachment_file` was called twice (once per chapter), not once.

- [ ] **Step 3: Hoist the download above the per-chapter loop**

In `backend/services/chapter_upload.py`, replace lines 162-174 (the start of the per-chapter loop through the `sliced = ...` line):

```python
        new_chapter_ids: list[str] = []
        pdf_ranges: dict[str, tuple[int, int]] = {}
        for chapter in confident_chapters:
            # Isolate per-chapter failures so one corrupt PDF / API error does
            # not abort the whole batch (matches chapter_retrofit.run()).
            try:
                # Fetch the BOOK's PDF ATTACHMENT (not the book item itself) —
                # analysis["attachment_key"] is the attachment key detected by
                # script 1.
                file_bytes = await zotero_read_client.get_attachment_file(
                    library_id, attachment_key, library_type=library_type
                ) if hasattr(zotero_read_client, "get_attachment_file") else None
                sliced = slice_pdf_range(file_bytes or b"", chapter["pdf_start_index"], chapter["pdf_end_index"])
```

with:

```python
        new_chapter_ids: list[str] = []
        pdf_ranges: dict[str, tuple[int, int]] = {}

        # Download the book's PDF attachment ONCE per book, not once per
        # chapter -- this used to sit inside the per-chapter loop below and
        # re-downloaded the same (often large) file for every confident
        # chapter detected in the same book. A download failure is deferred
        # and raised inside the loop so it's still reported per-chapter,
        # isolated the same way any other per-chapter failure already is.
        book_file_bytes: bytes | None = None
        book_download_error: str | None = None
        if confident_chapters:
            try:
                book_file_bytes = await zotero_read_client.get_attachment_file(
                    library_id, attachment_key, library_type=library_type
                ) if hasattr(zotero_read_client, "get_attachment_file") else None
            except Exception as exc:  # noqa: BLE001 - reported per chapter below
                book_download_error = str(exc)

        for chapter in confident_chapters:
            # Isolate per-chapter failures so one corrupt PDF / API error does
            # not abort the whole batch (matches chapter_retrofit.run()).
            try:
                if book_download_error is not None:
                    raise RuntimeError(book_download_error)
                sliced = slice_pdf_range(book_file_bytes or b"", chapter["pdf_start_index"], chapter["pdf_end_index"])
```

The rest of the per-chapter `try` block (template building, `create_items`, attachment upload, etc.) is unchanged — it never referenced the old `file_bytes` variable outside the `sliced = ...` line.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_chapter_upload.py -v`
Expected: PASS — all tests in the file, including the new one and the pre-existing `test_commit_creates_and_links`.

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "perf: download book PDF once per book in chapter-upload, not once per chapter"
```

---

### Task 2: Script 1 — analysis cache functions

**Files:**
- Modify: `backend/services/chapter_segmentation.py` (add `import json`; add cache functions near the top, after `extract_page_texts_from_pdf_bytes`)
- Test: `backend/tests/test_chapter_segmentation.py`

Adds a version-keyed cache for a book's full analysis result (heuristic or LLM-fallback chapters), distinct from `chapter_ocr.py`'s content-hash OCR cache: keyed on the PDF attachment's own Zotero `version` field, so a cache hit never requires downloading the PDF at all (a content-hash cache can only ever short-circuit *after* download, since the hash needs the bytes).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_chapter_segmentation.py`. First, extend the imports at the top of the file:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
```

Then add a new test class (place it near `TestRun`, e.g. just before it):

```python
class TestAnalysisCache(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            entry = {"item_key": "B1", "attachment_key": "A1", "chapters": []}
            save_analysis_cache(cache_dir, "B1", "A1", 5, "heuristic", entry)
            result = load_cached_analysis(cache_dir, "B1", "A1", 5, "heuristic")
            self.assertEqual(result, entry)

    def test_returns_none_when_not_cached(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(load_cached_analysis(Path(tmp), "B1", "A1", 5, "heuristic"))

    def test_different_version_is_a_cache_miss(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_analysis_cache(cache_dir, "B1", "A1", 5, "heuristic", {"chapters": []})
            self.assertIsNone(load_cached_analysis(cache_dir, "B1", "A1", 6, "heuristic"))

    def test_different_mode_is_a_cache_miss(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_analysis_cache(cache_dir, "B1", "A1", 5, "heuristic", {"chapters": []})
            self.assertIsNone(load_cached_analysis(cache_dir, "B1", "A1", 5, "llm_fallback"))
```

Also add `load_cached_analysis` and `save_analysis_cache` to the existing `from backend.services.chapter_segmentation import (...)` import block at the top of the file (the one that currently imports `TocEntry, extract_page_texts_from_pdf_bytes, find_toc_candidates, llm_extract_toc_entries, _toc_scan_indices, analyze_attachment_with_llm_fallback`):

```python
from backend.services.chapter_segmentation import (
    TocEntry,
    extract_page_texts_from_pdf_bytes,
    find_toc_candidates,
    llm_extract_toc_entries,
    load_cached_analysis,
    save_analysis_cache,
    _toc_scan_indices,
    analyze_attachment_with_llm_fallback,
)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestAnalysisCache -v`
Expected: FAIL — `ImportError: cannot import name 'load_cached_analysis'`.

- [ ] **Step 3: Implement the cache functions**

In `backend/services/chapter_segmentation.py`, add `import json` to the top import block (alongside the existing `import hashlib`):

```python
import hashlib
import io
import json
import logging
```

Then add the following functions right after `extract_page_texts_from_pdf_bytes` (i.e. after its closing `return [page.extract_text() or "" for page in reader.pages]` line, before `def find_toc_candidates(...)`):

```python
def _analysis_cache_path(cache_dir: Path, item_key: str, attachment_key: str, version: int, mode: str) -> Path:
    return cache_dir / f"{item_key}-{attachment_key}-v{version}-{mode}.analysis.json"


def load_cached_analysis(cache_dir: Path, item_key: str, attachment_key: str, version: int, mode: str) -> Optional[dict]:
    """Cached run()-entry (has_text_layer/needs_ocr/chapters/diagnostics)
    for this exact attachment VERSION and analysis `mode` ("heuristic" or
    "llm_fallback"), or None on a cache miss. Keyed by the attachment's own
    Zotero version (not a content hash) so a hit never requires downloading
    the PDF at all -- unlike chapter_ocr.py's content-hash cache, which can
    only short-circuit AFTER the download since the hash needs the bytes.
    `mode` is part of the key so a heuristic-only cache entry is never
    served back when the caller now wants the LLM fallback applied (a
    heuristic pass that found zero chapters must not silently shadow a
    later --llm-fallback run against the same unchanged attachment).
    """
    path = _analysis_cache_path(cache_dir, item_key, attachment_key, version, mode)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_analysis_cache(cache_dir: Path, item_key: str, attachment_key: str, version: int, mode: str, entry: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _analysis_cache_path(cache_dir, item_key, attachment_key, version, mode)
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestAnalysisCache -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add version-keyed analysis cache for chapter-segmentation"
```

---

### Task 3: Script 1 — wire the analysis cache into `run()`

**Files:**
- Modify: `backend/services/chapter_segmentation.py:1174-1220` (the `run()` body)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestRun` in `backend/tests/test_chapter_segmentation.py`:

```python
    def test_analysis_cache_hit_skips_download(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0006", "itemType": "book", "extra": ""}},
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
        mock_load.assert_called_once_with(unittest.mock.ANY, "BOOK0006", "ATT0005", 7, "heuristic")
        zotero_client.get_attachment_file.assert_not_called()
        self.assertEqual(result["attachments"], [cached_entry])

    def test_analysis_cache_miss_saves_result(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0007", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0006", "itemType": "attachment", "contentType": "application/pdf", "version": 3}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
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
        self.assertEqual(saved_args[4], "heuristic")
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0007")

    def test_no_cache_dir_never_touches_analysis_cache(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0008", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0007", "itemType": "attachment", "contentType": "application/pdf", "version": 1}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestRun -v -k analysis_cache`
Expected: FAIL — `load_cached_analysis`/`save_analysis_cache` are never called by `run()` yet, so `mock_load.assert_called_once_with(...)` (first test) fails, and `zotero_client.get_attachment_file.assert_not_called()` fails since the real download path always runs today.

- [ ] **Step 3: Wire the cache into `run()`**

In `backend/services/chapter_segmentation.py`, replace the `run()` body from `attachments_out: list[dict] = []` through the final `return {"slug": slug, "attachments": attachments_out}` (originally lines 1174-1222) with:

```python
    attachments_out: list[dict] = []
    total = len(books) or 1
    analysis_mode = "llm_fallback" if llm_service is not None else "heuristic"
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

        pages = extract_page_texts_from_pdf_bytes(file_bytes)
        has_text_layer = sum(len(p.strip()) for p in pages) > 100

        if not has_text_layer and ocr_cache_dir is not None:
            content_hash = hashlib.sha256(file_bytes).hexdigest()
            cached = load_cached_ocr(ocr_cache_dir, content_hash)
            if cached is not None:
                pages = cached["pages"]
                has_text_layer = sum(len(p.strip()) for p in pages) > 100

        if not has_text_layer:
            attachments_out.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "has_text_layer": False,
                "needs_ocr": True,
            })
            continue

        if llm_service is not None:
            analysis = await analyze_attachment_with_llm_fallback(pages, llm_service)
        else:
            analysis = analyze_attachment(pages)
        result_entry = {
            "item_key": item_key,
            "attachment_key": attachment_key,
            "has_text_layer": True,
            "needs_ocr": False,
            **analysis,
        }
        if ocr_cache_dir is not None:
            save_analysis_cache(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode, result_entry)
        attachments_out.append(result_entry)

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
```

Only the `has_text_layer: True` branch is cached — a `needs_ocr: True` result is cheap to re-derive (no heuristic/LLM work happened), and script 2's own OCR cache already covers the expensive part of that path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS — every test in the file, including the 3 new ones and all pre-existing `TestRun` tests (`test_processes_unlinked_book`, `test_uses_llm_fallback_when_llm_service_provided`, `test_reads_ocr_cache_when_no_text_layer_and_cache_hit`, `test_still_reports_needs_ocr_when_no_cache_dir_given` — none of these pass `ocr_cache_dir`, or exercise the `needs_ocr` branch, so the new caching code path never triggers for them).

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "perf: skip re-downloading/re-analyzing unchanged book PDFs via version-keyed cache"
```

---

### Task 4: Script 3 — extract `find_matches()` (pure matching logic)

**Files:**
- Modify: `backend/services/chapter_retrofit.py:110-160` (the fetch + match portion of `run()`)
- Test: `backend/tests/test_chapter_retrofit.py`

This is a pure refactor: extract the matching logic (given an already-fetched item list) into its own function, so it can later be reused without the whole-library fetch. `run()` itself is not yet rewired — that's Task 6, once `commit_links` also exists (Task 5).

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_chapter_retrofit.py`. First, extend the import line at the top:

```python
from backend.services.chapter_retrofit import find_best_book_match, find_matches, locate_chapter_pdf_range
from backend.services.chapter_retrofit import run as retrofit_run
```

Then add a new test class, e.g. right after `TestLocateChapterPdfRange`:

```python
class TestFindMatches(unittest.TestCase):
    def test_matches_without_touching_zotero_client(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        result = find_matches(all_items, item_keys=None, max_items=None)
        self.assertEqual(len(result["would_link"]), 1)
        self.assertEqual(result["would_link"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["would_link"][0]["book_key"], "BOOK1")
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["no_match"], [])

    def test_already_linked_chapter_is_excluded(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": "X-Contained-By: groups/1:BOOK1"}},
        ]
        result = find_matches(all_items, item_keys=None, max_items=None)
        self.assertEqual(result["would_link"], [])

    def test_item_keys_restricts_which_chapters_are_considered(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP2", "data": {"key": "CHAP2", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        result = find_matches(all_items, item_keys=["CHAP2"], max_items=None)
        self.assertEqual({e["chapter_key"] for e in result["would_link"]}, {"CHAP2"})

    def test_no_book_title_is_no_match(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "", "date": "2019", "extra": ""}},
        ]
        result = find_matches(all_items, item_keys=None, max_items=None)
        self.assertEqual(result["no_match"], ["CHAP1"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py::TestFindMatches -v`
Expected: FAIL — `ImportError: cannot import name 'find_matches'`.

- [ ] **Step 3: Extract `find_matches()`**

In `backend/services/chapter_retrofit.py`, insert the following function directly above `def run(`:

```python
def find_matches(all_items: list[dict], item_keys: list[str] | None, max_items: int | None) -> dict:
    """Pure matching logic: given an already-fetched full-library item list
    (see run()'s zotero_write_client.everything(...) call — the caller's
    job, not this function's), finds book/chapter matches. Returns
    {"would_link": [...], "ambiguous": [...], "no_match": [...]} — the same
    three buckets run()'s dry-run output already has (run() adds the
    "linked"/"failed" keys around this). Never touches Zotero and never
    writes; see commit_links for that half of script 3 (design spec §7).
    """
    books = [i for i in all_items if i["data"].get("itemType") == "book"]
    chapters = [i for i in all_items if i["data"].get("itemType") == "bookSection"]

    unlinked_chapters = [c for c in chapters if not parse_links(c["data"].get("extra", "")).contained_by]
    if item_keys is not None:
        wanted = set(item_keys)
        unlinked_chapters = [c for c in unlinked_chapters if c["data"]["key"] in wanted]
    if max_items is not None:
        unlinked_chapters = unlinked_chapters[:max_items]

    book_candidates = [
        {"key": b["data"]["key"], "title": b["data"].get("title", ""), "year": _year_from_date(b["data"].get("date"))}
        for b in books
    ]

    would_link: list[dict] = []
    ambiguous: list[dict] = []
    no_match: list[str] = []

    for chapter in unlinked_chapters:
        chapter_key = chapter["data"]["key"]
        book_title = chapter["data"].get("bookTitle", "")
        year = _year_from_date(chapter["data"].get("date"))

        if not book_title or not book_candidates:
            no_match.append(chapter_key)
            continue

        match = find_best_book_match(book_title, year, book_candidates)
        if match is None:
            ambiguous.append({"chapter_key": chapter_key, "candidates": book_candidates})
            continue

        would_link.append({"chapter_key": chapter_key, "book_key": match.book_key, "score": match.score})

    return {"would_link": would_link, "ambiguous": ambiguous, "no_match": no_match}
```

Do not remove anything from `run()` yet — it still has its own inline copy of this logic. `run()` is rewired in Task 6, after `commit_links()` also exists (Task 5), so both extractions land together in one clean rewrite.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v`
Expected: PASS — all tests, including the 4 new `TestFindMatches` tests and every pre-existing test (nothing in `run()` changed yet).

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "refactor: extract find_matches() from chapter_retrofit.run()"
```

---

### Task 5: Script 3 — extract `commit_links()` (write-only logic)

**Files:**
- Modify: `backend/services/chapter_retrofit.py` (add `commit_links` above `run()`)
- Test: `backend/tests/test_chapter_retrofit.py`

`commit_links` takes an already-computed `would_link` list (e.g. `find_matches()`'s output, or a prior dry run's saved JSON) and writes the links — re-fetching only the two specific items per link (`zot.item(book_key)`, `zot.item(chapter_key)`), never the whole library.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_chapter_retrofit.py`, extend the import line again:

```python
from backend.services.chapter_retrofit import find_best_book_match, find_matches, commit_links, locate_chapter_pdf_range
from backend.services.chapter_retrofit import run as retrofit_run
```

Add a new test class, e.g. right after `TestFindMatches`:

```python
class TestCommitLinks(unittest.TestCase):
    def test_writes_links_without_full_fetch(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        result = commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        zot.everything.assert_not_called()
        self.assertEqual(result["linked"], [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])
        self.assertEqual(zot.update_item.call_count, 2)
        first_call_arg = zot.update_item.call_args_list[0].args[0]
        self.assertEqual(first_call_arg["data"]["key"], "BOOK1")

    def test_write_failure_is_isolated_per_entry(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.update_item.side_effect = [None, Exception("boom")]

        result = commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["chapter_key"], "CHAP1")
        self.assertIn("boom", result["failed"][0]["error"])

    def test_multiple_entries_each_processed_independently(self):
        zot = MagicMock()
        items = {
            "BOOK1": {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}},
            "CHAP1": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}},
            "BOOK2": {"key": "BOOK2", "data": {"key": "BOOK2", "extra": ""}},
            "CHAP2": {"key": "CHAP2", "data": {"key": "CHAP2", "extra": ""}},
        }
        zot.item.side_effect = lambda key: items[key]

        result = commit_links(zot, "groups/1", [
            {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0},
            {"chapter_key": "CHAP2", "book_key": "BOOK2", "score": 0.95},
        ])

        self.assertEqual(len(result["linked"]), 2)
        self.assertEqual({e["chapter_key"] for e in result["linked"]}, {"CHAP1", "CHAP2"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py::TestCommitLinks -v`
Expected: FAIL — `ImportError: cannot import name 'commit_links'`.

- [ ] **Step 3: Extract `commit_links()`**

In `backend/services/chapter_retrofit.py`, insert this function directly above `def run(` (after `find_matches`, added in Task 4):

```python
def commit_links(zotero_write_client, slug: str, would_link: list[dict]) -> dict:
    """Writes X-Contains/X-Contained-By links for an already-computed
    would_link list (find_matches()'s output, or a prior dry run's saved
    JSON replayed via the CLI's --input / the API's would_link field).
    Re-fetches only the two specific items involved in EACH link -- never
    the whole library -- which is what makes replaying a prior dry run's
    matches fast (design spec §7's write ordering/self-healing behavior is
    unchanged: book side written first, so a chapter-write failure after a
    successful book write just re-writes a no-op X-Contains on retry).
    """
    linked: list[dict] = []
    failed: list[dict] = []

    for entry in would_link:
        chapter_key = entry["chapter_key"]
        book_key = entry["book_key"]
        score = entry["score"]

        try:
            book_item = zotero_write_client.item(book_key)
            chapter_item = zotero_write_client.item(chapter_key)

            existing_links = parse_links(book_item["data"].get("extra", ""))
            chapter_id = format_chapter_id(slug, chapter_key)
            book_id = format_chapter_id(slug, book_key)
            new_contains = list(dict.fromkeys([*existing_links.contains, chapter_id]))
            book_item["data"]["extra"] = write_links(book_item["data"].get("extra", ""), contains=new_contains)
            zotero_write_client.update_item(book_item)

            chapter_item["data"]["extra"] = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
            zotero_write_client.update_item(chapter_item)
        except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
            failed.append({"chapter_key": chapter_key, "book_key": book_key, "error": str(exc)})
            continue

        linked.append({"chapter_key": chapter_key, "book_key": book_key, "score": score})

    return {"linked": linked, "failed": failed}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v`
Expected: PASS — all tests, including the 3 new `TestCommitLinks` tests.

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "refactor: extract commit_links() from chapter_retrofit.run()"
```

---

### Task 6: Script 3 — rewire `run()` to use `find_matches`/`commit_links`, add `would_link` replay

**Files:**
- Modify: `backend/services/chapter_retrofit.py:110-192` (replace `run()`'s body)
- Test: `backend/tests/test_chapter_retrofit.py`

This is the task that actually delivers the speedup: `run(commit=True, would_link=<prior dry run's list>)` skips the full-library fetch entirely.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_chapter_retrofit.py`, inside `class TestRetrofitRun` (after its existing three tests):

```python
    def test_commit_with_would_link_skips_full_fetch(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
            commit=True,
            would_link=[{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
        )

        zot.everything.assert_not_called()
        self.assertEqual(len(result["linked"]), 1)
        self.assertEqual(result["linked"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["no_match"], [])
        self.assertEqual(result["would_link"], [])

    def test_commit_without_would_link_still_does_full_fetch(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None, commit=True)

        zot.everything.assert_called_once()
        self.assertEqual(len(result["linked"]), 1)

    def test_dry_run_ignores_would_link_and_still_matches_fresh(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(
            zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None,
            commit=False, would_link=[{"chapter_key": "IGNORED", "book_key": "IGNORED", "score": 1.0}],
        )

        zot.update_item.assert_not_called()
        self.assertEqual(result["would_link"][0]["chapter_key"], "CHAP1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py::TestRetrofitRun -v -k would_link`
Expected: FAIL — `run()` does not yet accept a `would_link` keyword argument (`TypeError: run() got an unexpected keyword argument 'would_link'`).

- [ ] **Step 3: Rewire `run()`**

In `backend/services/chapter_retrofit.py`, replace the entire `run()` function (originally lines 110-192) with:

```python
def run(
    *,
    zotero_write_client,
    slug: str,
    item_keys: list[str] | None,
    max_items: int | None,
    commit: bool = False,
    would_link: list[dict] | None = None,
) -> dict:
    """Core logic for script 3 (retrofit_chapter_links). Synchronous --
    pyzotero's client is itself synchronous. Defaults to dry-run -- `commit`
    must be explicitly True to write the `X-Contains`/`X-Contained-By`
    links to Zotero (mirrors chapter_upload.py's script 4 convention). See
    design spec §7.

    If `would_link` is given (e.g. a prior dry run's output, replayed via
    the CLI's --input flag or the API's `would_link` request field) AND
    commit=True, this skips the full-library fetch and matching pass
    entirely and goes straight to commit_links() -- this is what makes a
    commit run after a dry run fast: the full-library fetch is what
    dominates a fresh run's cost, not the fuzzy matching itself.
    `would_link` is ignored when commit=False; a dry run always matches
    fresh (there is nothing to preview if it just replayed a prior
    preview).
    """
    if commit and would_link is not None:
        result = commit_links(zotero_write_client, slug, would_link)
        return {**result, "would_link": [], "ambiguous": [], "no_match": []}

    all_items = zotero_write_client.everything(zotero_write_client.items())
    matches = find_matches(all_items, item_keys, max_items)

    if not commit:
        return {
            "linked": [], "would_link": matches["would_link"],
            "ambiguous": matches["ambiguous"], "no_match": matches["no_match"], "failed": [],
        }

    result = commit_links(zotero_write_client, slug, matches["would_link"])
    return {**result, "would_link": [], "ambiguous": matches["ambiguous"], "no_match": matches["no_match"]}
```

This removes the old inline fetch/match/write logic entirely — `find_matches` (Task 4) and `commit_links` (Task 5) now own it. `_year_from_date` stays where it is (still used by `find_matches`); no other function in the file changes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v`
Expected: PASS — all tests, including the 3 new ones and the pre-existing `test_links_confident_match_and_skips_ambiguous`, `test_book_write_succeeds_but_chapter_write_fails_reports_failed`, `test_defaults_to_dry_run_and_writes_nothing` (all three call `run()` without `would_link`, exercising the unchanged fresh-fetch path).

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `uv run pytest`
Expected: PASS — no other module imports `chapter_retrofit`'s removed internals directly (only `run` is imported elsewhere, per `backend/api/chapter_linking.py`).

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: let chapter_retrofit.run() replay a prior dry run's matches, skipping the full-library fetch"
```

---

### Task 7: CLI — add `--input` to `scripts/retrofit_chapter_links.py`

**Files:**
- Modify: `scripts/retrofit_chapter_links.py`

No test file exists for the CLI scripts themselves (they're thin argparse wrappers around the already-tested service functions — consistent with `scripts/analyze_book_chapters.py`, `scripts/ocr_attachments.py`, `scripts/upload_chapters.py`, none of which have dedicated unit tests either). Correctness is covered by Task 6's `run()` tests plus Task 9's E2E test below.

- [ ] **Step 1: Add the `--input` flag and wire it through**

Replace the full contents of `scripts/retrofit_chapter_links.py` with:

```python
#!/usr/bin/env python3
"""CLI for script 3: retrofit-link existing, separately-catalogued book and
bookSection items. Defaults to dry-run -- pass --commit to actually write.

Usage:
    uv run python scripts/retrofit_chapter_links.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --output .local/retrofit.json --commit

    # Replay a prior dry run's matches instead of re-fetching/re-matching
    # the whole library (much faster for a large library):
    uv run python scripts/retrofit_chapter_links.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --input .local/retrofit.json --commit
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyzotero import zotero

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_retrofit import run as retrofit_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrofit-link existing book/bookSection pairs.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Write-scoped Zotero API key")
    parser.add_argument("--item-keys", default=None, help="Comma-separated bookSection item keys to restrict to")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--commit", action="store_true", help="Actually write to Zotero (default: dry-run preview)")
    parser.add_argument(
        "--input", default=None,
        help="A prior dry run's --output JSON. Replays its would_link matches into the "
             "commit pass instead of re-fetching and re-matching the whole library. Only "
             "valid together with --commit.",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.input and not args.commit:
        parser.error("--input requires --commit (it replays a prior dry run's matches into a commit pass)")

    library_type, numeric_id, _library_id = parse_library_slug(args.library_slug)
    zot = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None
    would_link = json.loads(Path(args.input).read_text(encoding="utf-8")).get("would_link") if args.input else None

    result = retrofit_run(
        zotero_write_client=zot,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
        commit=args.commit,
        would_link=would_link,
    )

    if not args.commit:
        print(f"DRY RUN: would link {len(result['would_link'])} chapter(s). Pass --commit to apply.")
    else:
        print(f"Wrote {len(result['linked'])} link(s).")
    print(f"{len(result['ambiguous'])} ambiguous, {len(result['no_match'])} unmatched.")

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Wrote full result to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test the argument validation locally (no network)**

Run: `uv run python scripts/retrofit_chapter_links.py --library-slug groups/1 --api-key fake --input /tmp/does-not-matter.json`
Expected: exits with the argparse error `--input requires --commit (...)` and a non-zero exit code — confirms the validation fires before any Zotero client is constructed.

- [ ] **Step 3: Commit**

```bash
git add scripts/retrofit_chapter_links.py
git commit -m "feat: add --input to retrofit_chapter_links.py to replay a prior dry run's matches"
```

---

### Task 8: API — add `would_link` to `RetrofitLinkRequest`

**Files:**
- Modify: `backend/api/chapter_linking.py:178-212`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing test**

Add to `class TestRetrofitEndpoint` in `backend/tests/test_chapter_linking_api.py`:

```python
    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_passes_would_link_through(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        would_link = [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}]
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key", "committed": True, "would_link": would_link},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_run.call_args.kwargs["would_link"], would_link)

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_would_link_defaults_to_none(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(mock_run.call_args.kwargs["would_link"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py::TestRetrofitEndpoint -v -k would_link`
Expected: FAIL — `KeyError: 'would_link'` (the endpoint doesn't pass this kwarg to `retrofit_run` yet).

- [ ] **Step 3: Add the field and thread it through**

In `backend/api/chapter_linking.py`, replace the `RetrofitLinkRequest` model (lines 178-183):

```python
class RetrofitLinkRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    max_items: int | None = None
    committed: bool = False
```

with:

```python
class RetrofitLinkRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    max_items: int | None = None
    committed: bool = False
    # A prior dry run's `would_link` list (this endpoint's own response
    # shape, see JobStatusResponse.result). When given together with
    # committed=True, chapter_retrofit.run() replays it instead of
    # re-fetching and re-matching the whole library -- see run()'s
    # docstring in backend/services/chapter_retrofit.py.
    would_link: list[dict] | None = None
```

Then update the `_task()` closure inside `start_retrofit_link` (lines 198-205):

```python
            result = await asyncio.to_thread(
                retrofit_run,
                zotero_write_client=zot,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                commit=request.committed,
            )
```

to:

```python
            result = await asyncio.to_thread(
                retrofit_run,
                zotero_write_client=zot,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                commit=request.committed,
                would_link=request.would_link,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 2 new ones and the pre-existing `TestRetrofitEndpoint` tests.

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: accept would_link in the retrofit-link API endpoint"
```

---

### Task 9: E2E test — replay a dry run's matches via `--input`

**Files:**
- Modify: `backend/tests/test_chapter_linking_e2e.py`

Extends the existing live retrofit-link test with the new replay path, so the CLI wiring from Task 7 is exercised against a real Zotero library, not just mocked.

- [ ] **Step 1: Add the E2E test**

Add a new test function in `backend/tests/test_chapter_linking_e2e.py`, right after `test_retrofit_link_dummy_entries` (which ends at line 398 with `assert "X-Contained-By" not in unmatched_item["data"].get("extra", "")`):

```python
@pytest.mark.timeout(90)  # overrides pyproject.toml's global 30s -- real network + subprocess calls
def test_retrofit_link_replays_dry_run_via_input(zot, cleanup, tmp_path):
    """--input replays a prior dry run's would_link matches into --commit,
    without this script re-deriving them itself -- Task 6/7's optimization,
    exercised through the real CLI + a real Zotero library."""
    item_keys, _collection_keys = cleanup

    book_title = _unique("Dummy Replay Book")
    book_template = zot.item_template("book")
    book_template["title"] = book_title
    book_template["date"] = "2022"
    book_template["tags"] = [{"tag": _TEST_TAG}]
    book_resp = zot.create_items([book_template])
    book_key = list(book_resp["successful"].values())[0]["key"]
    item_keys.append(book_key)

    chapter_title = _unique("Dummy Replay Chapter")
    chapter_template = zot.item_template("bookSection")
    chapter_template["title"] = chapter_title
    chapter_template["bookTitle"] = book_title
    chapter_template["date"] = "2022"
    chapter_template["tags"] = [{"tag": _TEST_TAG}]
    chapter_resp = zot.create_items([chapter_template])
    chapter_key = list(chapter_resp["successful"].values())[0]["key"]
    item_keys.append(chapter_key)

    dryrun_output = tmp_path / "retrofit_replay_dry.json"
    _run_script(
        "retrofit_chapter_links.py",
        ["--item-keys", chapter_key, "--output", str(dryrun_output)],
    )
    dry_result = json.loads(dryrun_output.read_text())
    assert chapter_key in {e["chapter_key"] for e in dry_result["would_link"]}

    commit_output = tmp_path / "retrofit_replay_commit.json"
    _run_script(
        "retrofit_chapter_links.py",
        ["--input", str(dryrun_output), "--commit", "--output", str(commit_output)],
    )
    commit_result = json.loads(commit_output.read_text())
    assert not commit_result["failed"], commit_result["failed"]
    assert chapter_key in {e["chapter_key"] for e in commit_result["linked"]}

    chapter_item = zot.item(chapter_key)
    assert f"{LIBRARY_SLUG}:{book_key}" in chapter_item["data"]["extra"]

    book_item = zot.item(book_key)
    assert f"{LIBRARY_SLUG}:{chapter_key}" in book_item["data"]["extra"]
```

- [ ] **Step 2: Run the test against the real test library**

Run: `uv run pytest backend/tests/test_chapter_linking_e2e.py::test_retrofit_link_replays_dry_run_via_input -v -s`
Expected: PASS — requires a write-scoped `ZOTERO_API_KEY` in `.env` for the `test-rag-plugin` group (or `CHAPTER_LINKING_TEST_LIBRARY_SLUG` pointed at a library you control), per the file's own module docstring. If no such key is configured, this test errors at fixture setup rather than failing on real logic — confirm the error is about missing credentials, not about `--input`/`--commit` wiring, before treating it as blocking.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_chapter_linking_e2e.py
git commit -m "test: add E2E coverage for retrofit-link --input replay"
```

---

### Task 10: Documentation — update `docs/chapter-segmentation.md`

**Files:**
- Modify: `docs/chapter-segmentation.md:118-148` (script 3's section) and its "Running via the API" section (lines 199-212)

- [ ] **Step 1: Document `--input` in script 3's section**

In `docs/chapter-segmentation.md`, immediately after the existing script-3 code block (which ends with the `--commit` example, right before the paragraph starting "The output reports four buckets"), insert:

````markdown
A `--commit` run always re-fetches and re-matches the whole library from
scratch, same as a dry run — this is what a large library's `everything()`
call can make slow (the fuzzy matching itself is fast; the full-library
fetch dominates). To skip straight to writing a dry run's already-reviewed
matches instead, pass `--input` pointing at that dry run's `--output` file:

```bash
uv run python scripts/retrofit_chapter_links.py \
  --library-slug groups/6297749 \
  --api-key <write-scoped-zotero-key> \
  --input .local/retrofit.json \
  --commit
```

`--input` re-fetches only the two specific items involved in each link (to
get their current version before writing), never the whole library, and is
only valid together with `--commit` — a dry run always matches fresh.
````

- [ ] **Step 2: Document `would_link` in the API section**

In the same file's "Running via the API" section, the sentence about the retrofit-link endpoint's body currently reads:

```markdown
...the retrofit-link endpoint's body
takes a `committed` flag, mirroring the CLI's `--commit` and defaulting to
the same dry-run behavior) — useful for driving this from an external
scheduler or admin tool instead of a shell.
```

Replace it with:

```markdown
...the retrofit-link endpoint's body
takes a `committed` flag, mirroring the CLI's `--commit` and defaulting to
the same dry-run behavior, plus an optional `would_link` field mirroring
the CLI's `--input`: pass a prior dry run's `would_link` response array
alongside `committed: true` to skip straight to writing those matches
instead of re-fetching and re-matching the whole library) — useful for
driving this from an external scheduler or admin tool instead of a shell.
```

- [ ] **Step 3: Commit**

```bash
git add docs/chapter-segmentation.md
git commit -m "docs: document retrofit-link --input/would_link replay"
```

---

### Task 11: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full default test suite**

Run: `uv run pytest`
Expected: PASS — every test file under `backend/tests/`, excluding `integration`/`api`/`container`-marked tests per `pyproject.toml`'s `addopts`.

- [ ] **Step 2: Run the Node test suites (unaffected by this plan, but confirm no accidental breakage)**

Run: `npm run test:node && npm run test:plugin`
Expected: PASS — this plan touches no `bin/` or `plugin/` files, so this is a smoke check, not expected to catch anything.

- [ ] **Step 3: Confirm the live retrofit-link `--input` flow actually saves time**

Run (using the same `users/39226` library and `ZOTERO_API_KEY` from `.env` used earlier in this session):

```bash
set -a; source .env; set +a
time uv run python scripts/retrofit_chapter_links.py \
  --library-slug users/39226 --api-key "$ZOTERO_API_KEY" --max-items 10 \
  --output .local/retrofit_dryrun_39226.json
time uv run python scripts/retrofit_chapter_links.py \
  --library-slug users/39226 --api-key "$ZOTERO_API_KEY" \
  --input .local/retrofit_dryrun_39226.json --commit \
  --output .local/retrofit_commit_39226.json
```

Expected: the first (`dry-run`) call takes roughly the ~11 minutes observed earlier in this session (unchanged — it still does the full fetch); the second (`--input --commit`) call completes in a few seconds, since it only re-fetches the handful of specific book/chapter items involved in the 6 `would_link` matches instead of the whole library. **This actually writes `X-Contains`/`X-Contained-By` links to the real `users/39226` library** — confirm with the user before running this step, since it is a genuine (if intentional) mutation, not a dry run.
