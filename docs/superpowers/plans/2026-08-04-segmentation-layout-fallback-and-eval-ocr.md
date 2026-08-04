# Segmentation Layout-Mode Fallback + Evaluation OCR Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the chapter-segmentation pipeline recover the 10 new evaluation books that currently score 0.00/0.00, by (a) falling back to pypdf layout-mode extraction when default-mode text hides a real printed TOC, and (b) giving the evaluation harness the same OCR route production has, with content-hash caching for fast re-runs.

**Architecture:** Three small additions to existing modules — a layout-aware extraction entry point and a `pages_need_ocr` predicate in `backend/services/chapter_segmentation.py`, a reusable cached `ocr_pdf_pages` helper in `backend/services/chapter_ocr.py` — plus a new shared evaluation-loading module `backend/evaluation/harness.py` used by the pytest harness and both evaluation scripts, and a new `scripts/ocr_evaluation_pdfs.py` that populates a gitignored OCR cache via the Kreuzberg sidecar (mirroring production's script-1/script-2 split: analysis reads the OCR cache, a separate step fills it).

**Tech Stack:** Python 3.12 via `uv`, pypdf, unittest/pytest, Kreuzberg sidecar (podman container on `http://localhost:8100`), no new dependencies.

---

## Background: investigation findings (already verified — do not re-derive)

The 10 books in `backend/evaluation/book-segmentation/manifest.local.json` all score 0.00 precision / 0.00 recall today. The committed README currently claims they have "genuinely no signal". That claim is **wrong** for 6 of the 10. The verified root causes:

1. **Extraction-mode failure (6 native-text books).** All six have a real printed TOC page with a text layer, but pypdf's *default* extraction mode scrambles the two-column TOC layout (page numbers on separate lines from titles, or glued: `'7Vorwort'`, `'123III. Recht zwischen den Professionen'`). The TOC detector `find_toc_candidates` requires `<title> …dots/spaces… <number>` on ONE physical line, so it finds nothing. Re-extracting with `page.extract_text(extraction_mode="layout")` restores classic dot-leader lines (`'Wie funktioniert die Interpretation des Rechts in der Praxis?       41'`). Verified end-to-end with zero heuristic changes: `9783848736829` goes 0.00→**1.00/1.00** (23/23 exact), `9783789016202` →0.46/0.50, `9783492021234` →0.27/0.29, `9783899718188` →0.27/0.30.
2. **Degenerate text encoding (2 of the 6 "native" books: `9783789057366`, `9780367439712`).** Their pages extract as ONE giant line per page — in default AND layout mode (pypdf warns "Rotated text discovered"). No line-oriented parsing can ever work; these must be routed to OCR despite technically having a text layer.
3. **The evaluation path skips OCR entirely (4 scans: `9783465016878`, `9781409403906`, `9783848704316`, `dnb-36942798X`).** They have no text layer (0–2 pages with text). Production (`chapter_segmentation.run()` at `backend/services/chapter_segmentation.py:1446-1462`) detects this and reads `chapter_ocr.py`'s content-hash-keyed OCR cache — but the pytest harness and both eval scripts feed raw pypdf text straight in. The old 7-book set's scan (`9783322969828`) only worked because it shipped with an embedded OCR text layer.

Calibration data for the degenerate-page predicate, measured over all 17 evaluation books ("longish" = pages with > 500 stripped chars; "degen" = longish pages with < 3 newlines):

| Book | pages | longish | degen | degen fraction |
| --- | --- | --- | --- | --- |
| 13 healthy books (all committed 7 + 4 recoverable natives + 2 others) | 181–503 | ≥ 172 each | 0 | 0.00 |
| `9783789057366.pdf` | 739 | 682 | 672 | **0.99** |
| `9780367439712.pdf` | 285 | 268 | 260 | **0.97** |
| `9783465016878.pdf` / `9781409403906.pdf` / `9783848704316.pdf` | 322 / 372 / 343 | 0 | 0 | (no text at all) |
| `dnb-36942798X.pdf` | 411 | 1 | 1 | (2,366 chars total — NOTE: passes the current `> 100 chars` has-text-layer check!) |

So two clean thresholds separate healthy from broken with a wide margin: "fewer than 10% of pages are longish" (catches all 4 scans including dnb) and "more than 50% of longish pages are degenerate" (catches the 2 one-giant-line books, 0.97/0.99 vs 0.00 for everything healthy).

**Environment facts:**
- The Kreuzberg sidecar is already running: `podman ps` shows `zotero-rag-kreuzberg` on `127.0.0.1:8100`, `curl http://localhost:8100/health` returns 200. `Settings.kreuzberg_url` (in `backend/config/settings.py`) holds the URL; `get_settings()` returns the settings object.
- The repo uses **implicit namespace packages** — there is no `backend/__init__.py`, so do NOT create `__init__.py` files; `backend/evaluation/harness.py` is importable as `backend.evaluation.harness` as-is.
- Current branch is `feature/chapter-segmentation-linking`. The working tree already has uncommitted changes (10 new `*.expected.json` files, README/CLAUDE.md edits, `test_chapter_segmentation_accuracy.py` edits) — they are part of this same feature; include them in the commits of the tasks that touch those files (Task 6 and Task 9), and do not revert them.
- All Python commands run as `uv run …` from the repo root `/Users/cboulanger/Code/zotero-rag`.

## File structure

- Modify: `backend/services/chapter_segmentation.py` — add `layout` kwarg to `extract_page_texts_from_pdf_bytes`; add `pages_need_ocr()`; add `extract_page_texts_for_analysis()`; rewire `run()`.
- Modify: `backend/services/chapter_ocr.py` — add `ocr_pdf_pages()`; refactor `run()`'s inner loop to use it.
- Create: `backend/evaluation/harness.py` — shared manifest/pages loading for the evaluation set.
- Create: `scripts/ocr_evaluation_pdfs.py` — populates the eval OCR cache via Kreuzberg.
- Modify: `backend/tests/test_chapter_segmentation.py` — new unit tests (this file already holds the segmentation unit tests).
- Modify: `backend/tests/test_chapter_ocr.py` — unit tests for `ocr_pdf_pages`.
- Create: `backend/tests/test_evaluation_harness.py` — unit tests for the shared harness module.
- Modify: `backend/tests/test_chapter_segmentation_accuracy.py`, `scripts/evaluate_chapter_segmentation_strategies.py`, `scripts/evaluate_chapter_segmentation_llm_fallback.py` — use the shared harness.
- Modify: `backend/evaluation/book-segmentation/.gitignore` — ignore `.ocr-cache/`.
- Modify: `backend/evaluation/book-segmentation/manifest.local.json`, `README.md`, `CLAUDE.md` — corrected flags and documentation (Task 9, after real numbers exist).

---

### Task 1: `pages_need_ocr` predicate

**Files:**
- Modify: `backend/services/chapter_segmentation.py` (add after `extract_page_texts_from_pdf_bytes`, currently ending at line 221)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_chapter_segmentation.py` (append a new test class at the end of the file, before any `if __name__ == "__main__":` block if present; match the file's existing import of the module under test — add `pages_need_ocr` to the existing `from backend.services.chapter_segmentation import (...)` list):

```python
class TestPagesNeedOcr(unittest.TestCase):
    """pages_need_ocr must catch three real failure shapes found in the
    evaluation set (see backend/evaluation/book-segmentation/README.md):
    scans with no text layer, scans with a trivial amount of stray text,
    and PDFs whose text layer extracts as one giant line per page."""

    def _healthy_page(self) -> str:
        # ~35 lines of ~40 chars: realistic body page, plenty of newlines.
        return ("Dies ist eine gewoehnliche Textzeile ohne Nummer\n" * 35)

    def test_empty_page_list_needs_ocr(self):
        self.assertTrue(pages_need_ocr([]))

    def test_scan_without_text_layer_needs_ocr(self):
        self.assertTrue(pages_need_ocr([""] * 300))

    def test_scan_with_trivial_stray_text_needs_ocr(self):
        # Mirrors dnb-36942798X.pdf: 411 pages, only 2 carry any text
        # (2,366 chars total) -- more than the old >100-chars check allowed,
        # but obviously still an un-OCR'd scan.
        pages = [""] * 409 + [self._healthy_page()] * 2
        self.assertTrue(pages_need_ocr(pages))

    def test_normal_book_does_not_need_ocr(self):
        self.assertFalse(pages_need_ocr([self._healthy_page()] * 40))

    def test_one_giant_line_pages_need_ocr(self):
        # Mirrors 9783789057366.pdf / 9780367439712.pdf: pages have plenty
        # of text but essentially no newlines (one absolutely-positioned
        # run per page), so no line-oriented parsing can work.
        giant = "Wort " * 400  # ~2000 chars, zero newlines
        self.assertTrue(pages_need_ocr([giant] * 40))

    def test_few_degenerate_pages_among_healthy_ones_is_fine(self):
        # A handful of single-line pages (e.g. a part-divider printed
        # sideways) must not condemn a healthy book to OCR.
        pages = [self._healthy_page()] * 36 + ["Wort " * 400] * 4
        self.assertFalse(pages_need_ocr(pages))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -k PagesNeedOcr -v`
Expected: FAIL/ERROR with `ImportError: cannot import name 'pages_need_ocr'`

- [ ] **Step 3: Implement `pages_need_ocr`**

In `backend/services/chapter_segmentation.py`, directly after the `extract_page_texts_from_pdf_bytes` function (after current line 221), add:

```python
# Calibrated against the 17-book evaluation set (see backend/evaluation/
# book-segmentation/README.md): healthy books have >=90% "longish" pages
# (>500 stripped chars) and 0% of them degenerate (<3 newlines); un-OCR'd
# scans have ~0% longish pages; the two known degenerate-text-layer books
# (whole page extracted as one absolutely-positioned line) sit at 97-99%
# degenerate. Both thresholds have a wide margin on real data.
_OCR_MIN_SUBSTANTIAL_PAGE_CHARS = 500
_OCR_MIN_SUBSTANTIAL_PAGE_FRACTION = 0.1
_OCR_DEGENERATE_MAX_NEWLINES = 3
_OCR_DEGENERATE_MAX_FRACTION = 0.5


def pages_need_ocr(pages: list[str]) -> bool:
    """True when this page text cannot be analyzed and the PDF should go
    through the OCR route instead: either there is (almost) no text layer
    at all, or the text layer is degenerate -- each page extracted as one
    giant line with no line structure (seen with absolutely-positioned /
    rotated text runs), which defeats every line-oriented heuristic in this
    module. Replaces the old `total chars > 100` has-text-layer check,
    which a 411-page scan with two stray text pages slipped past.
    """
    if not pages:
        return True
    substantial = [p.strip() for p in pages if len(p.strip()) > _OCR_MIN_SUBSTANTIAL_PAGE_CHARS]
    if len(substantial) < _OCR_MIN_SUBSTANTIAL_PAGE_FRACTION * len(pages):
        return True
    degenerate = [p for p in substantial if p.count("\n") < _OCR_DEGENERATE_MAX_NEWLINES]
    return len(degenerate) > _OCR_DEGENERATE_MAX_FRACTION * len(substantial)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -k PagesNeedOcr -v`
Expected: 6 PASS

- [ ] **Step 5: Run the whole default suite**

Run: `uv run pytest -q`
Expected: all green (this task adds a new function; nothing calls it yet)

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add pages_need_ocr predicate catching absent and degenerate text layers"
```

---

### Task 2: layout-mode extraction fallback

**Files:**
- Modify: `backend/services/chapter_segmentation.py:215-221` (`extract_page_texts_from_pdf_bytes`) and add `extract_page_texts_for_analysis` after `pages_need_ocr`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_chapter_segmentation.py` (add `extract_page_texts_for_analysis` to the module-under-test imports; also add `from unittest.mock import patch` to the imports at the top of the file if not already present):

```python
# Fake page sets for extract_page_texts_for_analysis. 10 pages total, so
# find_toc_candidates' front window (15%) is exactly page 0 and printed page
# numbers up to 2*10=20 are plausible. The "default mode" pages are healthy
# multi-line text with NO TOC-shaped lines; the "layout mode" pages carry a
# classic 3-entry dot-leader TOC on page 0.
_BODY_PAGE = "Dies ist eine gewoehnliche Textzeile ohne Nummer\n" * 35
_DEFAULT_MODE_PAGES = [_BODY_PAGE] * 10
_LAYOUT_MODE_PAGES = [
    "Inhalt\n"
    "Erstes Kapitel .......... 5\n"
    "Zweites Kapitel .......... 9\n"
    "Drittes Kapitel .......... 15\n"
] + [_BODY_PAGE] * 9
_TOC_DEFAULT_PAGES = [_LAYOUT_MODE_PAGES[0]] + [_BODY_PAGE] * 9


class TestExtractPageTextsForAnalysis(unittest.TestCase):
    """The layout fallback must only fire when default-mode extraction hides
    the TOC: default-found TOC -> default pages untouched (protects the
    existing 7-book baseline); no TOC either way -> default pages; OCR-shaped
    input -> default pages without even attempting the (slow) layout pass."""

    def test_keeps_default_pages_when_default_mode_finds_toc(self):
        def fake_extract(content, layout=False):
            if layout:
                raise AssertionError("layout extraction must not run when default mode already finds a TOC")
            return _TOC_DEFAULT_PAGES

        with patch("backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes", side_effect=fake_extract):
            pages, layout_used = extract_page_texts_for_analysis(b"%PDF-fake")
        self.assertEqual(pages, _TOC_DEFAULT_PAGES)
        self.assertFalse(layout_used)

    def test_falls_back_to_layout_pages_when_only_layout_mode_finds_toc(self):
        def fake_extract(content, layout=False):
            return _LAYOUT_MODE_PAGES if layout else _DEFAULT_MODE_PAGES

        with patch("backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes", side_effect=fake_extract):
            pages, layout_used = extract_page_texts_for_analysis(b"%PDF-fake")
        self.assertEqual(pages, _LAYOUT_MODE_PAGES)
        self.assertTrue(layout_used)

    def test_keeps_default_pages_when_neither_mode_finds_toc(self):
        def fake_extract(content, layout=False):
            return _DEFAULT_MODE_PAGES

        with patch("backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes", side_effect=fake_extract):
            pages, layout_used = extract_page_texts_for_analysis(b"%PDF-fake")
        self.assertEqual(pages, _DEFAULT_MODE_PAGES)
        self.assertFalse(layout_used)

    def test_skips_layout_attempt_entirely_for_ocr_shaped_input(self):
        def fake_extract(content, layout=False):
            if layout:
                raise AssertionError("layout extraction must not run for pages that need OCR")
            return [""] * 300

        with patch("backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes", side_effect=fake_extract):
            pages, layout_used = extract_page_texts_for_analysis(b"%PDF-fake")
        self.assertEqual(pages, [""] * 300)
        self.assertFalse(layout_used)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -k ExtractPageTextsForAnalysis -v`
Expected: FAIL/ERROR with `ImportError: cannot import name 'extract_page_texts_for_analysis'`

- [ ] **Step 3: Implement**

Replace the whole `extract_page_texts_from_pdf_bytes` function (`backend/services/chapter_segmentation.py:215-221`) with:

```python
def extract_page_texts_from_pdf_bytes(content: bytes, layout: bool = False) -> list[str]:
    """Return one text string per physical PDF page, in index order (index 0
    = first page). Uses pypdf directly rather than Kreuzberg's chunking,
    which does not guarantee a clean 1:1 page<->chunk mapping.

    With `layout=True`, uses pypdf's layout extraction mode, which preserves
    horizontal whitespace -- on TOC pages typeset as a title column plus a
    page-number column, this keeps each entry on one physical line where the
    default mode scrambles them (numbers on their own lines, or glued onto
    the next title). Slower, and it can throw on individual pages (e.g.
    rotated text), so per-page failures degrade to an empty string.
    """
    reader = PdfReader(io.BytesIO(content))
    if not layout:
        return [page.extract_text() or "" for page in reader.pages]
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            texts.append("")
    return texts
```

Then, directly after the `pages_need_ocr` function added in Task 1, add:

```python
def extract_page_texts_for_analysis(content: bytes) -> tuple[list[str], bool]:
    """Page texts to feed the segmentation heuristics, plus whether the
    layout-mode fallback was used.

    Default-mode pypdf extraction is kept whenever it already yields a
    detectable TOC (so books that work today are byte-for-byte unaffected).
    When it doesn't, the pages are re-extracted in layout mode and adopted
    if a TOC becomes detectable that way -- verified on real evaluation
    books whose two-column TOC the default mode scrambles beyond
    recognition (see backend/evaluation/book-segmentation/README.md).
    OCR-shaped input (see pages_need_ocr) skips the layout attempt: a book
    with no usable text layer cannot be rescued by a different text
    extraction mode, only by actual OCR.
    """
    pages = extract_page_texts_from_pdf_bytes(content)
    if pages_need_ocr(pages):
        return pages, False
    if find_toc_candidates(pages):
        return pages, False
    layout_pages = extract_page_texts_from_pdf_bytes(content, layout=True)
    if find_toc_candidates(layout_pages):
        return layout_pages, True
    return pages, False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -k ExtractPageTextsForAnalysis -v`
Expected: 4 PASS

- [ ] **Step 5: Run the whole default suite**

Run: `uv run pytest -q`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: fall back to pypdf layout-mode extraction when default mode hides the printed TOC"
```

---

### Task 3: reusable cached `ocr_pdf_pages` in chapter_ocr.py

**Files:**
- Modify: `backend/services/chapter_ocr.py` (add `ocr_pdf_pages` after `slice_single_page_pdf`, i.e. after line 78; refactor `run()`'s inner loop)
- Test: `backend/tests/test_chapter_ocr.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_chapter_ocr.py` (reuse the file's existing imports — it already has `unittest`, `AsyncMock`, `MagicMock`; additionally ensure `import io`, `import tempfile`, `from pathlib import Path`, and `from pypdf import PdfWriter` are imported, and add `ocr_pdf_pages` to the imports from `backend.services.chapter_ocr`):

```python
def _two_page_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestOcrPdfPages(unittest.IsolatedAsyncioTestCase):
    async def test_ocrs_each_page_individually_and_caches_by_content_hash(self):
        pdf_bytes = _two_page_pdf_bytes()
        extractor = AsyncMock()
        extractor.extract_and_chunk.side_effect = [
            [MagicMock(text="page one text")],
            [MagicMock(text="page two text")],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            pages = await ocr_pdf_pages(
                pdf_bytes, extractor=extractor, cache_dir=cache_dir, language="deu",
            )
            self.assertEqual(pages, ["page one text", "page two text"])
            self.assertEqual(extractor.extract_and_chunk.await_count, 2)
            # one page-sliced PDF per call, never the whole book at once
            for call in extractor.extract_and_chunk.await_args_list:
                self.assertEqual(call.kwargs.get("ocr_language"), "deu")
            self.assertEqual(len(list(cache_dir.glob("*.json"))), 1)

            # Second call with identical bytes: served from cache, extractor untouched.
            pages_again = await ocr_pdf_pages(
                pdf_bytes, extractor=extractor, cache_dir=cache_dir, language="deu",
            )
            self.assertEqual(pages_again, ["page one text", "page two text"])
            self.assertEqual(extractor.extract_and_chunk.await_count, 2)

    async def test_reports_per_page_progress(self):
        pdf_bytes = _two_page_pdf_bytes()
        extractor = AsyncMock()
        extractor.extract_and_chunk.return_value = [MagicMock(text="x")]
        seen: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            await ocr_pdf_pages(
                pdf_bytes, extractor=extractor, cache_dir=Path(tmp), language="eng",
                on_page=lambda done, total: seen.append((done, total)),
            )
        self.assertEqual(seen, [(1, 2), (2, 2)])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_ocr.py -k OcrPdfPages -v`
Expected: FAIL/ERROR with `ImportError: cannot import name 'ocr_pdf_pages'`

- [ ] **Step 3: Implement `ocr_pdf_pages`**

In `backend/services/chapter_ocr.py`, after `slice_single_page_pdf` (line 78), add:

```python
async def ocr_pdf_pages(
    content: bytes,
    *,
    extractor,
    cache_dir: Path,
    language: str,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> list[str]:
    """OCR every page of `content` via the Kreuzberg sidecar, returning one
    text string per physical page (index 0 = first page, matching pypdf's
    indexing used throughout chapter_segmentation.py). Results are cached in
    `cache_dir` keyed by the PDF's own content hash -- a later call with the
    same bytes returns the cached pages without touching the extractor.
    `on_page(pages_done, total_pages)` is called after each page for
    progress reporting on long books.
    """
    content_hash = hashlib.sha256(content).hexdigest()
    cached = load_cached_ocr(cache_dir, content_hash)
    if cached is not None:
        return cached["pages"]

    reader = PdfReader(io.BytesIO(content))
    total_pages = len(reader.pages)
    page_texts: list[str] = []
    for page_index in range(total_pages):
        single_page_bytes = slice_single_page_pdf(content, page_index)
        chunks = await extractor.extract_and_chunk(
            single_page_bytes, "application/pdf", ocr_language=language
        )
        page_texts.append(" ".join(c.text for c in chunks))
        if on_page is not None:
            on_page(page_index + 1, total_pages)

    save_ocr_cache(cache_dir, content_hash, detected_language=language, pages=page_texts)
    return page_texts
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_ocr.py -k OcrPdfPages -v`
Expected: 2 PASS

- [ ] **Step 5: Refactor `run()` to use it**

In `backend/services/chapter_ocr.py`'s `run()`, replace the per-page OCR loop and cache save (currently lines 129-138):

```python
            reader = PdfReader(io.BytesIO(file_bytes))
            page_texts: list[str] = []
            for page_index in range(len(reader.pages)):
                single_page_bytes = slice_single_page_pdf(file_bytes, page_index)
                chunks = await extractor.extract_and_chunk(
                    single_page_bytes, "application/pdf", ocr_language=language
                )
                page_texts.append(" ".join(c.text for c in chunks))

            cache_path = save_ocr_cache(cache_dir, content_hash, detected_language=language, pages=page_texts)
```

with:

```python
            page_texts = await ocr_pdf_pages(
                file_bytes, extractor=extractor, cache_dir=cache_dir, language=language,
            )
            cache_path = _cache_path(cache_dir, content_hash)
```

(`run()`'s own earlier cache check already handled the hit case before this point, so `ocr_pdf_pages`'s internal cache check is redundant there but harmless — and it is what makes the function safe to call directly from scripts.)

- [ ] **Step 6: Run the full chapter_ocr test file and the default suite**

Run: `uv run pytest backend/tests/test_chapter_ocr.py -v && uv run pytest -q`
Expected: all green — the pre-existing `run()` tests must not change behavior

- [ ] **Step 7: Commit**

```bash
git add backend/services/chapter_ocr.py backend/tests/test_chapter_ocr.py
git commit -m "feat: extract reusable content-hash-cached ocr_pdf_pages from chapter_ocr.run"
```

---

### Task 4: wire production `run()` to the new extraction + OCR detection

**Files:**
- Modify: `backend/services/chapter_segmentation.py` — inside `run()`, currently lines 1445-1478

- [ ] **Step 1: Rewire the extraction block**

In `backend/services/chapter_segmentation.py`'s `run()`, replace this block (currently lines 1445-1462):

```python
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
```

with:

```python
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
```

- [ ] **Step 2: Record the fallback in the result entry**

Still in `run()`, in the `result_entry = {...}` dict a few lines below (currently lines 1469-1475), add one key after `"needs_ocr": False,`:

```python
                "layout_mode_used": layout_mode_used,
```

- [ ] **Step 3: Run the default suite**

Run: `uv run pytest -q`
Expected: all green. If a test asserts on `run()`'s output dict shape, the only changes are the new `layout_mode_used` key and OCR routing for degenerate-text books — fix the *test expectation* only if it hardcodes the full key set; the production behavior change here is intended.

- [ ] **Step 4: Commit**

```bash
git add backend/services/chapter_segmentation.py
git commit -m "feat: use layout-fallback extraction and pages_need_ocr in the analysis pipeline"
```

---

### Task 5: shared evaluation harness module

**Files:**
- Create: `backend/evaluation/harness.py`
- Modify: `backend/evaluation/book-segmentation/.gitignore`
- Test: `backend/tests/test_evaluation_harness.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_evaluation_harness.py`:

```python
"""Unit tests for backend/evaluation/harness.py -- the pages-loading logic
shared by the pytest accuracy harness and the evaluation scripts. The
extraction and cache primitives are patched; what's under test is the
routing: healthy pages pass through, OCR-shaped pages come from the eval
OCR cache, and a cache miss returns None (caller skips the book)."""

import unittest
from unittest.mock import patch

from backend.evaluation.harness import analysis_pages_for

_HEALTHY_PAGES = ["Zeile\n" * 200] * 40
_OCR_PAGES = ["ocr text\n" * 100] * 40


class TestAnalysisPagesFor(unittest.TestCase):
    def test_returns_extracted_pages_when_text_layer_is_usable(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=(_HEALTHY_PAGES, False)), \
             patch("backend.evaluation.harness.load_cached_ocr") as mock_cache:
            pages = analysis_pages_for(b"%PDF-fake")
        self.assertEqual(pages, _HEALTHY_PAGES)
        mock_cache.assert_not_called()

    def test_returns_cached_ocr_pages_for_ocr_shaped_input(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=([""] * 300, False)), \
             patch("backend.evaluation.harness.load_cached_ocr", return_value={"detected_language": "deu", "pages": _OCR_PAGES}):
            pages = analysis_pages_for(b"%PDF-fake")
        self.assertEqual(pages, _OCR_PAGES)

    def test_returns_none_on_ocr_cache_miss(self):
        with patch("backend.evaluation.harness.extract_page_texts_for_analysis", return_value=([""] * 300, False)), \
             patch("backend.evaluation.harness.load_cached_ocr", return_value=None):
            self.assertIsNone(analysis_pages_for(b"%PDF-fake"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_evaluation_harness.py -v`
Expected: ERROR with `ModuleNotFoundError: No module named 'backend.evaluation.harness'`

- [ ] **Step 3: Create `backend/evaluation/harness.py`**

(No `__init__.py` — the repo uses implicit namespace packages.)

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_evaluation_harness.py -v`
Expected: 3 PASS

- [ ] **Step 5: Gitignore the OCR cache**

`backend/evaluation/book-segmentation/.gitignore` currently contains (NOTE: no trailing newline after the last line — replace the whole file rather than appending, to avoid corrupting the last entry):

```text
*.pdf
manifest.local.json
.ocr-cache/
```

- [ ] **Step 6: Run the default suite and commit**

Run: `uv run pytest -q`
Expected: all green

```bash
git add backend/evaluation/harness.py backend/tests/test_evaluation_harness.py backend/evaluation/book-segmentation/.gitignore
git commit -m "feat: shared evaluation harness module with production-mirroring page loading"
```

---

### Task 6: point the pytest accuracy harness at the shared module

**Files:**
- Modify: `backend/tests/test_chapter_segmentation_accuracy.py`

(This file already has uncommitted `heuristic_expected_zero` edits in the working tree — build on them, don't revert.)

- [ ] **Step 1: Rewrite the harness to use `backend.evaluation.harness`**

Replace the entire contents of `backend/tests/test_chapter_segmentation_accuracy.py` with:

```python
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
                    # route (see book-segmentation/README.md) -- so zero is
                    # the expected outcome here, not a regression.
                    continue
                # Reported, not gated (design spec §12: probabilistic, not pass/fail) —
                # this assertion only catches a total regression to zero detection.
                self.assertGreater(recall, 0.0, f"{pdf_path.name}: detected zero of {len(expected_ranges)} known chapters")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the harness (integration) to see it work end-to-end**

Run: `uv run pytest backend/tests/test_chapter_segmentation_accuracy.py -q -s`
Expected at this point (OCR cache not yet populated):
- The original 7 committed books print **the same numbers as the README's "Current results" table** (their default-mode extraction already finds a TOC, so the fallback never fires for them — any change here is a regression, stop and investigate).
- `9783848736829.pdf` prints precision=1.00 recall=1.00; `9783492021234.pdf`, `9783789016202.pdf`, `9783899718188.pdf` print nonzero partial scores.
- The 4 scans and the 2 degenerate books print `SKIPPED (needs OCR ...)`.
- Books still flagged `heuristic_expected_zero: true` don't fail the recall assertion; overall exit is green.

- [ ] **Step 3: Run the default suite and commit**

Run: `uv run pytest -q`
Expected: all green (the accuracy file is integration-marked, so the default run only collects it, never executes it)

```bash
git add backend/tests/test_chapter_segmentation_accuracy.py
git commit -m "test: load evaluation pages production-style (layout fallback + OCR cache) in accuracy harness"
```

---

### Task 7: update both evaluation scripts

**Files:**
- Modify: `scripts/evaluate_chapter_segmentation_strategies.py`
- Modify: `scripts/evaluate_chapter_segmentation_llm_fallback.py`

- [ ] **Step 1: Update the strategies script**

In `scripts/evaluate_chapter_segmentation_strategies.py`:

1. Delete the local `_load_manifest_books` and `_available_books` functions and the `_EVAL_DIR` constant (lines 39-60).
2. Replace the import block from `backend.services.chapter_segmentation` (lines 34-37) with:

```python
from backend.evaluation.harness import analysis_pages_for, available_books
from backend.services.chapter_segmentation import analyze_attachment_with_strategies
```

3. In `_main`, replace `triples = _available_books()` with `triples = available_books()`, and replace the two lines

```python
            file_bytes = pdf_path.read_bytes()
            pages = extract_page_texts_from_pdf_bytes(file_bytes)
```

with:

```python
            file_bytes = pdf_path.read_bytes()
            pages = analysis_pages_for(file_bytes)
            if pages is None:
                print(f"{pdf_path.name}: SKIPPED (needs OCR — populate the cache with: "
                      f"uv run python scripts/ocr_evaluation_pdfs.py)")
                continue
```

(The loop variable already unpacks `(pdf_path, expected_path, book)`, matching `available_books()`'s return shape.)

- [ ] **Step 2: Update the LLM-fallback script the same way**

In `scripts/evaluate_chapter_segmentation_llm_fallback.py`:

1. Delete `_load_manifest_books`, `_available_books`, and `_EVAL_DIR` (lines 37-59).
2. Replace the `backend.services.chapter_segmentation` import block (lines 32-35) with:

```python
from backend.evaluation.harness import analysis_pages_for, available_books
from backend.services.chapter_segmentation import analyze_attachment_with_llm_fallback
```

3. In `_main`, replace `pairs = _available_books()` with `pairs = available_books()`, change the loop header from `for pdf_path, expected_path in pairs:` to `for pdf_path, expected_path, _book in pairs:`, and replace

```python
        pages = extract_page_texts_from_pdf_bytes(pdf_path.read_bytes())
```

with:

```python
        pages = analysis_pages_for(pdf_path.read_bytes())
        if pages is None:
            print(f"{pdf_path.name}: SKIPPED (needs OCR — populate the cache with: "
                  f"uv run python scripts/ocr_evaluation_pdfs.py)")
            continue
```

- [ ] **Step 3: Smoke-run the strategies script**

Run: `uv run python scripts/evaluate_chapter_segmentation_strategies.py --no-crossref`
Expected: prints one line per book; OCR-needing books print SKIPPED; no tracebacks. (Do not run the LLM-fallback script here — it costs paid API calls; its update is structurally identical and Task 9 verifies imports.)

- [ ] **Step 4: Verify the LLM script at least imports cleanly**

Run: `uv run python scripts/evaluate_chapter_segmentation_llm_fallback.py --help`
Expected: the `--help` text prints without import errors (module-level imports run before argparse, so this catches a broken import without costing any LLM call)

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluate_chapter_segmentation_strategies.py scripts/evaluate_chapter_segmentation_llm_fallback.py
git commit -m "refactor: evaluation scripts load pages via shared harness (layout fallback + OCR cache)"
```

---

### Task 8: `scripts/ocr_evaluation_pdfs.py`

**Files:**
- Create: `scripts/ocr_evaluation_pdfs.py`

- [ ] **Step 1: Create the script**

```python
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
```

- [ ] **Step 2: Verify it runs and correctly skips healthy books**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8100/health` — expected `200`. If not, the Kreuzberg sidecar isn't up; stop and report rather than debugging podman here.

Then run: `uv run python scripts/ocr_evaluation_pdfs.py 2>&1 | tee /tmp/ocr_eval_run.log`
Expected: the 11 healthy books print `text layer usable, no OCR needed` within the first minutes; the 6 OCR-needing books (`9783789057366`, `9780367439712`, `9783465016878`, `9781409403906`, `9783848704316`, `dnb-36942798X`) start printing page progress. **This full run takes on the order of 1–3 hours** (~2,500 pages of per-page HTTP OCR) — run it in the background and continue only after it completes. Cached results make every later run near-instant.

- [ ] **Step 3: Commit (script only — the cache is gitignored)**

```bash
git add scripts/ocr_evaluation_pdfs.py
git commit -m "feat: script to OCR evaluation books into the content-hash eval cache"
```

---

### Task 9: re-run evaluations, correct flags and documentation

**Files:**
- Modify: `backend/evaluation/book-segmentation/manifest.local.json`
- Modify: `backend/evaluation/book-segmentation/README.md`
- Modify: `backend/evaluation/book-segmentation/CLAUDE.md`

(README.md and CLAUDE.md already carry uncommitted edits in the working tree — build on them.)

- [ ] **Step 1: Run the accuracy harness over everything**

Precondition: Task 8's OCR run finished (all 6 books report `done` / a later `uv run python scripts/ocr_evaluation_pdfs.py` re-run prints only instant lines).

Run: `uv run pytest backend/tests/test_chapter_segmentation_accuracy.py -q -s 2>&1 | tee /tmp/accuracy_run.log`
Record every per-book precision/recall line. Hard requirements:
- The original 7 committed books match the README's "Current results" table exactly (no regression).
- `9783848736829.pdf` reports 1.00/1.00.
- No SKIPPED lines remain.

- [ ] **Step 2: Run the strategies evaluation**

Run: `uv run python scripts/evaluate_chapter_segmentation_strategies.py 2>&1 | tee /tmp/strategies_run.log`
Record every line (incl. `strategies_used`).

- [ ] **Step 3: Correct `heuristic_expected_zero` flags**

In `backend/evaluation/book-segmentation/manifest.local.json`, for each of the 10 entries: set `"heuristic_expected_zero": false` if Step 1 showed recall > 0 for that book; leave `true` only for books still at exactly 0.00 recall. (Expected from the investigation: `9783848736829`, `9783492021234`, `9783789016202`, `9783899718188` flip to `false`; the OCR'd books depend on actual OCR quality — set them from the measured numbers, not from guesses.) Then re-run Step 1's command once more — it must still exit green (books with recall > 0 now also pass the `recall > 0` assertion; books left at `true` are still exempted).

- [ ] **Step 4: Update the README**

In `backend/evaluation/book-segmentation/README.md`:

1. Regenerate the "Current results" table and its aggregate line from Step 1's actual output (now covering all books that produce numbers, not just the committed 7).
2. Regenerate the "Strategy-pipeline status" table and aggregate from Step 2's output.
3. Rewrite the "Diverse real-library evaluation set" section's failure analysis: replace the "none of the three strategies have any signal / genuinely no regex-detectable TOC" narrative with the verified root causes, and describe the current mechanism, covering these facts:
   - 6 of the 10 books always had a real printed TOC page; pypdf's *default* extraction mode scrambled its two-column layout so `find_toc_candidates` saw nothing. `extract_page_texts_for_analysis` now falls back to layout-mode extraction when the default-mode text yields no TOC, which recovers 4 of them outright (list the measured per-book numbers).
   - 2 books (`9783789057366`, `9780367439712`) have a degenerate text layer (every page extracts as one giant line, default and layout mode alike) and are routed to OCR by `pages_need_ocr`, alongside the 4 true scans with no text layer at all.
   - OCR-routed books are analyzed from the gitignored `.ocr-cache/` (content-hash keyed, same format as production `chapter_ocr.py`), populated once via `uv run python scripts/ocr_evaluation_pdfs.py` (needs the Kreuzberg sidecar); the harness and both evaluation scripts skip a book with a pointer to that script when its cache entry is missing.
   - Report the measured post-OCR numbers for those 6 books honestly, including any that remain at zero (those keep `heuristic_expected_zero: true` and stay documented as known limitations, with a one-line reason from inspecting the diagnostics — e.g. OCR quality, still no locatable TOC lines).
4. Keep the document status-quo-only: describe the current mechanism and current numbers; the old snapshot tables/claims are replaced, not annotated with "previously".

- [ ] **Step 5: Update the eval-dir CLAUDE.md**

In `backend/evaluation/book-segmentation/CLAUDE.md`, update the `heuristic_expected_zero` explanation (in "Step 0") to match the new meaning: the flag now means "zero recall even after the layout-mode fallback and the OCR route" (checked via `backend.evaluation.harness.analysis_pages_for` + `analyze_attachment`), not merely "`find_toc_candidates` returns empty on default-mode text". Also add one sentence to the workflow: OCR-needing books require `uv run python scripts/ocr_evaluation_pdfs.py` (Kreuzberg sidecar up) before the harness can score them.

- [ ] **Step 6: Final full-suite check**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add backend/evaluation/book-segmentation/manifest.local.json \
        backend/evaluation/book-segmentation/README.md \
        backend/evaluation/book-segmentation/CLAUDE.md \
        backend/evaluation/book-segmentation/*.expected.json \
        backend/tests/test_chapter_segmentation_accuracy.py
git commit -m "docs: correct evaluation-set failure analysis and flags after layout-fallback + OCR route"
```

(This commit intentionally sweeps in the pre-existing uncommitted `*.expected.json` ground-truth files and any remaining eval-dir edits from the earlier session — they are part of this same feature branch.)

---

## Verification checklist (run after all tasks)

- [ ] `uv run pytest -q` — default suite green.
- [ ] `uv run pytest backend/tests/test_chapter_segmentation_accuracy.py -q -s` — 7 committed books unchanged vs. the pre-change README table; `9783848736829` at 1.00/1.00; no SKIPPED lines; exit green.
- [ ] `uv run python scripts/evaluate_chapter_segmentation_strategies.py` — runs all 17 books without a traceback.
- [ ] Second run of `uv run python scripts/ocr_evaluation_pdfs.py` completes in seconds (all cache hits) — proves the caching works.
- [ ] `git status` — no stray uncommitted files except gitignored ones (`*.pdf`, `manifest.local.json`, `.ocr-cache/`).

## Known risks / notes for the implementer

- **Do not "fix" the 7-book baseline if it shifts.** If any of the 7 committed books' numbers change in Task 6 Step 2, the layout fallback fired where it shouldn't have (or extraction changed) — that's a bug in your change, not a new baseline. Stop and investigate `extract_page_texts_for_analysis`'s decision path for that book.
- **OCR output quality for the 6 OCR-routed books is untested territory.** The plan's success criterion for them is honest measurement, not any particular score. `ocr_pdf_pages` joins each page's Kreuzberg chunks with spaces (existing production behavior) — if OCR'd TOC pages end up line-mangled the same way default pypdf extraction was, those books legitimately keep `heuristic_expected_zero: true` and get documented as such.
- **Layout-mode extraction is slow** (roughly doubles extraction time for every book that reaches the fallback). It only runs when the default mode finds no TOC, and OCR-shaped books skip it entirely — do not add further short-circuits without measurements.
- Pytest's default 30s per-test timeout (pyproject.toml) is overridden to 900s only on the accuracy method; if collection-time work ever grows, keep that in mind.
- The `Rotated text discovered. Output will be incomplete.` warning pypdf prints during layout-mode extraction of some books is expected and harmless — do not suppress it globally.
