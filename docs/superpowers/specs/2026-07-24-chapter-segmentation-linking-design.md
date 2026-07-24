# Chapter Segmentation & Book/Chapter Linking — Design Spec

## 1. Goal

Edited-book libraries currently produce duplicate, competing citations: a "book" item indexes the whole PDF, and any separately-catalogued "bookSection" (chapter) items index overlapping excerpts of the same text, but Zotero has no built-in way to relate a book to its chapters and the RAG retrieval/indexing pipeline has no mechanism to prefer one over the other (see prior investigation in this conversation: `backend/services/rag_engine.py:301-322` groups by `attachment_key`, so book and chapter chunks are always treated as two independent "documents").

This project adds four scripts plus one indexing-pipeline change to close that gap:

1. **Analyze** (`analyze_book_chapters.py`) — find book PDFs that can plausibly be segmented into chapters, or that need OCR first.
2. **OCR** (`ocr_attachments.py`) — run per-item, language-aware OCR on attachments lacking a text layer, feeding back into (1).
3. **Retrofit-link** (`retrofit_chapter_links.py`) — for already-separately-catalogued book/chapter item pairs, reconstruct the missing link between them.
4. **Segment & upload** (`upload_chapters.py`) — physically slice a book PDF per (1)'s detected chapter boundaries, create real Zotero `bookSection` items with inherited metadata, and link them.
5. **Indexing suppression** — once a book is linked to its chapters, stop indexing the book's own pages that are now covered by a linked chapter, so retrieval naturally surfaces the chapter's (correct) citation instead of the book's.

All four scripts share one linking convention, one write-safety model, and one CLI+API+progress harness, specified in §2–§4 before the per-script sections (§5–§8).

## 2. Linking scheme

Stored in the Zotero item's `Extra` field as dedicated lines, alongside whatever else already lives there (e.g. Better BibTeX citekeys):

```text
X-Contained-By: groups/6297749:ABCD1234
X-Contains: groups/6297749:WXYZ5678,groups/6297749:MNOP9012
```

- On a **book** item: `X-Contains` is a comma-separated list of its chapters.
- On a **bookSection** item: `X-Contained-By` points at its book.
- IDs are `<slug>:<item_key>` (slug = Zotero's own `users/{id}` / `groups/{id}`) rather than a bare item key, so the link stays unambiguous even if the same book/chapter pair is ever indexed from two different libraries. Item keys alone are only unique per-library.
- Each chapter's **page range within the book** is *not* duplicated here — `bookSection` items already have a native Zotero `pages` field (e.g. `"45-67"`); §7's suppression logic reads that directly instead of maintaining a second source of truth.

**New shared module** `backend/services/chapter_link_store.py`:
- `parse_links(extra: str) -> ChapterLinks` — extracts `contained_by: str | None` and `contains: list[str]` from an item's `Extra` text; ignores unrelated lines.
- `write_links(extra: str, *, contained_by=None, contains=None) -> str` — replaces only the `X-Contained-By:`/`X-Contains:` lines (if present) or appends them (if absent), leaving all other `Extra` content untouched. Idempotent: calling twice with the same value is a no-op change.

## 3. Write-key handling

Only scripts (3) and (4) write to Zotero. Both take a **write-scoped Zotero API key per invocation** — CLI flag `--api-key` / env var, or an API request field — used in-memory for that run only, never persisted. This mirrors the existing precedent in `scripts/openalex_import.py:332-429` and deliberately does *not* extend `AutoIndexKeyStore` (which by design, per `backend/zotero/key_validator.py:37-71`, only ever accepts and stores read-only keys). Scripts (1) and (2) only need read access and may reuse a stored `AutoIndexKeyStore` key or take a read-only key directly.

## 4. Shared CLI + API + progress harness

**New module** `backend/services/job_tracker.py`:
```python
class JobTracker:
    """In-memory job registry, keyed by job_id. Mirrors backend/api/document_upload.py's
    _UploadTask pattern rather than the subprocess/cron_indexer pattern — these are
    on-demand, human-triggered runs, not unattended scheduled jobs, so PID files and
    stale-lock recovery are unneeded complexity here."""
    def create(self) -> str: ...                      # returns new job_id
    def update(self, job_id, *, progress: float, message: str, result=None, error=None): ...
    def get(self, job_id) -> JobStatus | None: ...
```

Each script's core logic is a plain async function `async def run(..., progress_callback: Callable[[float, str], None])`.

- **CLI**: thin `argparse` wrapper (matching `bin/index_libraries.py`'s shell) that calls `run()` directly in-process and renders a `tqdm` gauge from the same callback — following the existing precedent in `scripts/openalex_import.py:496-508` (the only current user of the already-declared `tqdm` dependency).
- **API**: one new router, `backend/api/chapter_linking.py`, registered in `backend/main.py` alongside the other feature routers. Each script gets a `POST /api/chapter-linking/{analyze,ocr,retrofit-link,segment-upload}` that creates a job via `JobTracker`, spawns `run()` as an `asyncio.create_task`, and returns `{"job_id": ...}` immediately. One shared `GET /api/chapter-linking/jobs/{job_id}` polls status for all four, matching `backend/api/document_upload.py:751-779`'s existing polling shape.
- A job's lifetime is tied to the API process — if the server restarts mid-run, the job is simply lost and must be re-triggered. Acceptable for on-demand, human-supervised operations; the recurring-cron-style crash recovery in `cron_indexer.py` is not needed here.

## 5. Script 1 — Analyze (`analyze_book_chapters.py`)

**Input:** library slug + read-only key; optional explicit item-key list (else scans all `book`-type items in the library); optional `--ocr-results <path>` to consume script 2's output for items that needed OCR.

**Per book attachment:**
1. Skip if `X-Contains`/`X-Contained-By` already present (via `chapter_link_store.parse_links`), unless `--relink`.
2. Check text-layer presence (a Kreuzberg call without `force_ocr`, or reuse cached OCR text from script 2). If absent: emit `needs_ocr: true` and stop — this item is a candidate for script 2.
3. If present, attempt segmentation:
   - Scan the first ~15% and last ~5% of pages for TOC-like structure (repeated `<title> ... <page number>` line patterns).
   - Cross-reference each TOC candidate line against the actual text found at that page number (exact/fuzzy substring match) to confirm it's a real chapter start rather than a false TOC hit.
   - Run spaCy NER (`en_core_web_sm`, already a project dependency — see `docs/zotero-plugin-dev.md`/CLAUDE.md for the model) on the lines around each confirmed chapter-start page to extract candidate author names.
   - Cluster confirmed chapter-start pages into contiguous `[start_page, end_page]` ranges.

**Output JSON** (also the direct input to script 4):
```json
{
  "item_key": "ABCD1234",
  "attachment_key": "EFGH5678",
  "has_text_layer": true,
  "needs_ocr": false,
  "segmentation_confidence": "high",
  "chapters": [
    {"title": "...", "authors": ["..."], "start_page": 12, "end_page": 34, "confidence": 0.93}
  ],
  "diagnostics": {"toc_pages_scanned": [1, 2, 3], "toc_matches_found": 9, "notes": "..."}
}
```

**Design bias — precision over recall.** Born-digital academic edited-volume PDFs (Springer/Routledge/Palgrave-style) very often carry machine-generated, dotted-leader TOCs that heuristics can parse reliably. The expected failure modes are scanned/older volumes (OCR noise degrades TOC matching), running-header-only books with no real TOC, and non-Latin scripts. Given this is heuristics-only for v1 (no ML/NLP classifier), segmentation is only ever claimed when TOC cross-referencing strongly confirms it; everything else is reported as `segmentation_confidence: "low"` for manual review rather than guessed — a wrong auto-link would corrupt real bibliographic metadata, which is worse than a missed one. The output schema is intentionally decoupled from the detection method (plain page ranges + confidence scores) so a future ML-based detector could replace the heuristics internally without changing what scripts 3/4 consume.

**Ground-truth testing:** `tests/fixtures/chapter_segmentation/` holds sample PDFs (to be supplied) plus hand-annotated expected output (title/author/page-range per chapter). A scoring harness (`tests/test_chapter_segmentation_accuracy.py`) reports precision/recall on chapter-boundary detection and author-attribution accuracy — probabilistic, not a hard pass/fail gate.

## 6. Script 2 — OCR (`ocr_attachments.py`)

**Input:** a list of attachment item keys (typically script 1's `needs_ocr: true` output, or a user-supplied list).

**Language detection:** prefer the Zotero item's own `language` field if set; otherwise run language detection (new dependency, `langdetect` — not currently in `pyproject.toml`) against the item title, mapped to the closest Tesseract pack already installed in the Kreuzberg sidecar (`config/kreuzberg.toml`: `eng`, `deu`, `fra`, `spa`); fall back to the sidecar's current combined default (`"eng+deu+fra+spa"`) when detection confidence is low.

**Required code change** (not just a new script): `backend/services/extraction/kreuzberg.py:70-76` currently builds its `_config` dict once in `__init__` and reuses it for every `/extract` call, with no `ocr`/`language` key at all. Kreuzberg's `/extract` API already accepts a per-request `config.ocr.language` override — it's simply not wired up in this codebase yet. This project adds an optional `ocr_language` parameter threaded through `KreuzbergExtractor.__init__` → `backend/services/extraction/__init__.py:49-54`'s factory call, with `_config`'s assembly moved from `__init__` into the per-call `extract_and_chunk` path so language can vary per document rather than being fixed per extractor instance.

**Caching:** OCR'd text is cached keyed by the existing whole-file `content_hash` (`data/ocr_cache/<content_hash>.json`) so script 1, script 4, and normal indexing don't re-run OCR on the same PDF.

**Output JSON:** `{"item_key": ..., "attachment_key": ..., "detected_language": "deu", "ocr_succeeded": true, "char_count": 48213, "cache_path": "data/ocr_cache/<hash>.json"}` — the list fed back into script 1.

## 7. Script 3 — Retrofit link (`retrofit_chapter_links.py`)

**Input:** library slug + write-scoped key; optional item-key restriction.

**Matching:** for each unlinked `bookSection` item, read its native `bookTitle` and `date`/year. Candidate `book` items are those whose `title` fuzzy-matches `bookTitle` (new dependency, `rapidfuzz` — not currently in `pyproject.toml`; token-based similarity, not currently duplicated anywhere in this codebase) **and** whose year matches exactly (or within a small tolerance, e.g. ±1, for edition variance).

**Decision rule:** auto-link only when the top candidate scores at/above a threshold (default 0.90) **with a clear margin** over the next-best candidate — guards against two similarly-titled anthologies both scoring high. Anything below threshold, or an ambiguous near-tie, goes to a manual-review list instead of being linked. If no candidate book item exists in the library at all, the chapter is reported as `no_match`; script 3 never creates new book items (that's script 4's territory, or a manual cataloguing step).

**Write, on auto-link:** `chapter_link_store.write_links(...)` on both the chapter (`X-Contained-By`) and the book (append to `X-Contains`).

**Output JSON:**
```json
{
  "linked": [{"chapter_key": "...", "book_key": "...", "score": 0.94}],
  "ambiguous": [{"chapter_key": "...", "candidates": [{"book_key": "...", "title": "...", "year": 2019, "score": 0.87}]}],
  "no_match": ["..."]
}
```

## 8. Script 4 — Segment & upload (`upload_chapters.py`)

**Input:** library slug + write-scoped key, plus script 1's diagnostic JSON (or a single-item override). Only processes chapters at/above script 1's confidence threshold; lower-confidence detections are skipped and reported, never guessed into permanent metadata.

**Defaults to dry-run.** Running with no explicit flag only returns/prints what *would* be created (new item field values, page range, PDF-slice preview) without writing anything to Zotero. An explicit `--commit` (CLI) / `"committed": true` (API) is required to actually create items and upload files — this is a hard-to-reverse, externally-visible write into a real Zotero library, so it does not happen silently.

**Per detected chapter, on commit:**
1. Slice the book PDF to `[start_page, end_page]` into a standalone file (`pypdf.PdfWriter` — already a project dependency, `pyproject.toml:17`).
2. Create a new `bookSection` item via `pyzotero` (`item_template("bookSection")` → `create_items(...)`, following the existing precedent in `scripts/openalex_import.py:332-429`), populating it with metadata **inherited from the book** for correct citation quality: `bookTitle` = book's title, `editor` = the book's own creators, plus `publisher`/`place`/`date`/`ISBN`/`language` copied down — and the chapter's own detected `title`/`author` from script 1 (left blank for manual fill-in if author-detection confidence was low, rather than guessing a wrong name into permanent metadata).
3. Set the new item's native `pages` field to `"<start_page>-<end_page>"` — this is what §9's suppression logic keys off of.
4. Upload the sliced PDF as a child attachment (`attachment_simple(...)`, same precedent as step 2).
5. Write the link (`chapter_link_store.write_links`): `X-Contained-By` on the new chapter, append to `X-Contains` on the book.

The book's own item/attachment is never modified or deleted — it keeps its full PDF; new chapter items are strictly additive.

**Collection organization** — new `--target-collection` option (default `"Book Chapters"`):
- A top-level Zotero collection with this name is created on-demand if it doesn't already exist.
- Under it, a per-book subcollection is created (or reused, if already present) named with an author-year short label — e.g. `"Miller (2023)"`, or `"Smith et al. (1999)"` for 3+ authors — derived from the book's own creators/date. `backend/db/vector_store.py:42` (`_extract_lastnames`) already extracts normalized last names from a Zotero author-string list for Qdrant filtering; the label-formatting logic here reuses that helper for the last-name extraction, adding only the "et al." / year suffix on top.
- Both the top-level collection and the per-book subcollection are looked up before creation (`pyzotero`'s `collections()`/`collections_sub()`), so re-running script 4 against an already-processed book reuses the existing subcollection instead of creating a duplicate.
- Each newly created chapter item is added to its book's subcollection, in addition to remaining a normal top-level library item.

## 9. Indexing-pipeline change: suppress superseded book chunks

This is what actually makes retrieval prefer the chapter citation, closing the loop this whole project started from.

When `document_processor.py` indexes a **book** item that has an `X-Contains` list, it resolves each linked chapter's native `pages` field and **excludes the book's own pages that fall inside any linked chapter's range** from chunking — the book's index entry then only covers its true residual content (front matter, table of contents, index, and any chapters not yet linked), while each linked chapter's own attachment (with correct title/author metadata) independently covers its own pages.

This requires **no changes** to `backend/db/vector_store.py` or `backend/services/rag_engine.py`, no new Qdrant payload field, and no schema-version bump (`CURRENT_SCHEMA_VERSION` stays at 6) — it's a targeted change to the page-filtering step already present in `document_processor.py`'s chunking path.

**Re-indexing trigger:** linking happens *after* a book may already be fully indexed with the old (unsuppressed) chunk set. No new trigger mechanism is needed for this, though: writing `X-Contains` to the book's `Extra` field via the Zotero API is itself a write to the item, which increments the item's Zotero version number like any other edit. The existing incremental-indexing version comparison (`item_version`/`zotero_modified` in `ChunkMetadata`, `backend/models/document.py:33-89`) already reprocesses any item whose Zotero version has moved past what's stored — so the book is naturally picked up and reprocessed with page suppression applied on the next normal indexing pass, exactly as if its metadata had changed for any other reason.

## 10. New files

```text
backend/services/
  job_tracker.py            # §4 — shared JobTracker
  chapter_link_store.py     # §2 — Extra-field read/write/parse
  chapter_segmentation.py   # §5 — TOC matching, NER, page clustering
  chapter_ocr.py            # §6 — language detection + Kreuzberg OCR invocation
  chapter_retrofit.py       # §7 — fuzzy matching/scoring
  chapter_upload.py         # §8 — PDF slicing, item creation, collection management
backend/api/
  chapter_linking.py        # §4 — POST .../{analyze,ocr,retrofit-link,segment-upload} + GET .../jobs/{id}
scripts/                  # matches existing convention: bin/ is for unattended cron/service
                           # scripts (index_libraries.py); scripts/ holds manual/one-off tools
                           # (openalex_import.py, debug_live_query.py) — these four are the latter
  analyze_book_chapters.py
  ocr_attachments.py
  retrofit_chapter_links.py
  upload_chapters.py
tests/fixtures/chapter_segmentation/   # ground-truth PDFs + annotations (§5)
```

## 11. New dependencies

Confirmed absent from `pyproject.toml` (checked directly, alongside `pyzotero`/`spacy`/`pypdf`/`tqdm` which are already present):
- `rapidfuzz` — fuzzy title matching, §7.
- `langdetect` — title-based language detection, §6.

## 12. Testing plan

- Unit tests per new service module: `chapter_link_store` parse/write round-tripping and idempotency; `chapter_segmentation`'s TOC-matching heuristics against fixtures; `chapter_retrofit`'s scoring/threshold logic; the page-range suppression logic added to `document_processor.py`.
- Ground-truth PDF fixtures (§5) drive precision/recall metrics for segmentation accuracy — reported, not a hard pass/fail gate, since heuristic accuracy is inherently probabilistic.
- No changes to `Dockerfile`/`docker-compose.yml`/the container startup path — the container smoke test (`uv run pytest -m container`) is unaffected and doesn't need to be run for this work.

## 13. Known limitations (accepted)

- **Heuristics-only segmentation will miss some real chapter boundaries.** Scanned/older volumes, non-standard layouts, and books without a machine-parseable TOC will often be reported as `segmentation_confidence: "low"` rather than segmented — by design (§5), since a wrong auto-link is worse than a missed one. A future ML-based detector is an intentional later option, not built now.
- **Retrofit matching (§7) depends on `bookTitle`/`title` string similarity and year**, and will not link a book/chapter pair if either field is missing, badly mistyped, or the year is off by more than the tolerance — these fall into `no_match`/`ambiguous` for manual handling rather than being silently skipped.
- **Suppression (§9) is page-range-based**, so it assumes chapter `pages` values are accurate; a wrong page range (from a bad script-1 detection or a manually-mistyped retrofit) would suppress the wrong pages from the book's own index. Since script 4 only auto-creates high-confidence chapters and script 3 only auto-links high-confidence matches, this risk is bounded but not eliminated.
