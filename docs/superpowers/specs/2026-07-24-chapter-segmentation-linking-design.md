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
- Each chapter's **PDF page-index range within the book's file** is stored as a separate key, `X-Chapter-Pdf-Range`, e.g. `X-Chapter-Pdf-Range: groups/6297749:WXYZ5678:52-74`. This is deliberately **not** the same thing as the chapter's native Zotero `pages` field: `pages` holds the printed/bibliographic page numbers for citation display (e.g. `"45-67"`), while `X-Chapter-Pdf-Range` holds physical page *indices* into the book's PDF file, which is what §9's suppression logic and any future re-slicing actually need. The two numbers routinely differ once there's unnumbered or roman-numeral front matter before the book's arabic pagination starts, and `pages` is human-editable (so it must never be relied on as a machine input) while `X-Chapter-Pdf-Range` is only ever written by these scripts. See §5, §7, §8, §9 for how each script produces/consumes it.

**New shared module** `backend/services/chapter_link_store.py`:

- `parse_links(extra: str) -> ChapterLinks` — extracts `contained_by: str | None`, `contains: list[str]`, and `pdf_ranges: dict[str, tuple[int, int]]` (chapter ID → `(start_index, end_index)`, from `X-Chapter-Pdf-Range`) from an item's `Extra` text; ignores unrelated lines.
- `write_links(extra: str, *, contained_by=None, contains=None, pdf_ranges=None) -> str` — replaces only the `X-Contained-By:`/`X-Contains:`/`X-Chapter-Pdf-Range:` lines (if present) or appends them (if absent), leaving all other `Extra` content untouched. Idempotent: calling twice with the same value is a no-op change. A chapter ID with no entry in `pdf_ranges` simply has no `X-Chapter-Pdf-Range` data — this is the normal, safe state when the range isn't confidently known (§7, §9).

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
- **`--max-items <N>` (CLI) / `max_items` (API request field)**: caps how many items each script processes in a single run, on all four scripts (script 1's book scan, script 2's OCR list, script 3's unlinked-bookSection scan, script 4's chapter list). For testing/debugging against a real, large library without waiting for or committing a full run. Applied as a simple truncation of the work list before the main loop starts; omitted or `None` means no limit (the existing default, unchanged behavior).

## 5. Script 1 — Analyze (`analyze_book_chapters.py`)

**Input:** library slug + read-only key; optional explicit item-key list (else scans all `book`-type items in the library); optional `--ocr-results <path>` to consume script 2's output for items that needed OCR.

**Per book attachment:**

1. Skip if `X-Contains`/`X-Contained-By` already present (via `chapter_link_store.parse_links`), unless `--relink`.
2. Check text-layer presence (a Kreuzberg call without `force_ocr`, or reuse cached OCR text from script 2). If absent: emit `needs_ocr: true` and stop — this item is a candidate for script 2.
3. If present, attempt segmentation. **Critical safeguard: PDF physical page index and printed/citation page number are never assumed to be equal** — front matter (title page, copyright page, TOC, often roman-numeral-paginated or unpaginated) routinely offsets them, and the offset can't be guessed, only located.
   - Scan the first ~15% and last ~5% of pages for TOC-like structure (repeated `<title> ... <printed page number>` line patterns). The printed page number found here is a *target to search for*, not an index to jump to.
   - For each TOC entry, locate its actual **PDF page index** by searching the book's full extracted text for that entry's title/author appearing at the start of a page (fuzzy/exact content match scanned across the whole document) — this is a content lookup, never an assumption that `pdf_index == printed_number`. The matching PDF index is `pdf_start_index` for that chapter.
   - Independently, attempt to read the **printed page number** actually shown on that same physical page (a short header/footer line matching an arabic- or roman-numeral-only pattern) to populate `citation_pages` for later use in the Zotero `pages` field. If this can't be read confidently, or printed numbers don't increase monotonically across the confirmed chapter-start pages, mark `page_mapping_confidence: "unmappable"` and leave `citation_pages: null` — segmentation and slicing still proceed normally, since neither depends on this number.
   - Run spaCy NER (`en_core_web_sm`, already a project dependency — see `docs/zotero-plugin-dev.md`/CLAUDE.md for the model) on the lines around each confirmed chapter-start page to extract candidate author names.
   - Cluster confirmed chapter-start PDF indices into contiguous `[pdf_start_index, pdf_end_index]` ranges.

**Output JSON** (also the direct input to script 4):

```json
{
  "item_key": "ABCD1234",
  "attachment_key": "EFGH5678",
  "has_text_layer": true,
  "needs_ocr": false,
  "total_pdf_pages": 412,
  "segmentation_confidence": "high",
  "chapters": [
    {
      "title": "...",
      "authors": ["..."],
      "pdf_start_index": 52,
      "pdf_end_index": 74,
      "citation_pages": "45-67",
      "confidence": 0.93,
      "page_mapping_confidence": "high"
    }
  ],
  "diagnostics": {"toc_pages_scanned": [1, 2, 3], "toc_matches_found": 9, "notes": "..."}
}
```

`pdf_start_index`/`pdf_end_index` (mechanical — physical position in the file, used for slicing in §8 and suppression in §9) and `citation_pages` (bibliographic — printed number, used only for the Zotero `pages` field) are independent fields with independent confidence: a chapter can have high boundary-detection `confidence` but `page_mapping_confidence: "unmappable"` (citation pages simply left blank for manual entry), and that never blocks slicing or suppression, which only need the PDF-index pair.

**Design bias — precision over recall.** Born-digital academic edited-volume PDFs (Springer/Routledge/Palgrave-style) very often carry machine-generated, dotted-leader TOCs that heuristics can parse reliably. The expected failure modes are scanned/older volumes (OCR noise degrades TOC matching), running-header-only books with no real TOC, and non-Latin scripts. Given this is heuristics-only for v1 (no ML/NLP classifier), segmentation is only ever claimed when TOC cross-referencing strongly confirms it; everything else is reported as `segmentation_confidence: "low"` for manual review rather than guessed — a wrong auto-link would corrupt real bibliographic metadata, which is worse than a missed one. The output schema is intentionally decoupled from the detection method (plain page ranges + confidence scores) so a future ML-based detector could replace the heuristics internally without changing what scripts 3/4 consume.

**Ground-truth testing:** `backend/evaluation/book-segmentation/` holds seven real, hand-verified books (English/French/German, various publishers) — `manifest.json` is the single source of truth for the set (language, DOI, OA status, download URL), not a README table. The six OA entries are fetched on demand via `scripts/fetch_evaluation_pdfs.py`; the seventh (a 1976 scanned/OCR'd yearbook, `oa: false`) can never be legally auto-downloaded, so that script instead prints its DOI and the exact save path for manual acquisition through institutional access. Every entry (including the non-OA one) is paired with a hand-annotated `<name>.expected.json` (title/author/PDF-index-range/citation-pages per chapter — see that directory's README for the exact schema and how to add more books). A scoring harness (`tests/test_chapter_segmentation_accuracy.py`) reports precision/recall on chapter-boundary detection and author-attribution accuracy — probabilistic, not a hard pass/fail gate.

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

**PDF-range localization (new sub-step, needed for §9 suppression to work on retrofitted pairs).** Unlike script 4, which knows a chapter's PDF range by construction (it just sliced it), script 3 links two *pre-existing*, separately-catalogued items with no page relationship given — the chapter's own attachment might even be a different scan of the same content, not a byte-identical excerpt. So for each successful auto-link, script 3 additionally:

1. Extracts the chapter item's own attachment text.
2. Searches for its best-matching contiguous span inside the book's PDF text (fuzzy/sliding-window text matching across the book's pages).
3. If a confident, unambiguous, contiguous match is found → writes `X-Chapter-Pdf-Range` for that pair, same as script 4.
4. If not (low text-overlap score, multiple equally-plausible spans, or the chapter's content doesn't appear as a contiguous span at all) → the identity link (`X-Contained-By`/`X-Contains`) is still written, since it's independently valuable for correct citation metadata, but `X-Chapter-Pdf-Range` is simply **not written** for that pair. §9 treats a missing range as "don't suppress this chapter" — this is a safe degradation, not an error: the retrofit still fixes the metadata problem even when it can't safely automate the retrieval-suppression problem for that specific pair.

**Output JSON:**

```json
{
  "linked": [{"chapter_key": "...", "book_key": "...", "score": 0.94, "pdf_range_localized": true}],
  "ambiguous": [{"chapter_key": "...", "candidates": [{"book_key": "...", "title": "...", "year": 2019, "score": 0.87}]}],
  "no_match": ["..."]
}
```

`pdf_range_localized: false` on a `linked` entry means the identity link was written but suppression won't apply for that chapter until its range can be established some other way (e.g. re-running with a cleaner chapter-attachment text layer, or manual entry).

## 8. Script 4 — Segment & upload (`upload_chapters.py`)

**Input:** library slug + write-scoped key, plus script 1's diagnostic JSON (or a single-item override). Only processes chapters at/above script 1's confidence threshold; lower-confidence detections are skipped and reported, never guessed into permanent metadata.

**Defaults to dry-run.** Running with no explicit flag only returns/prints what *would* be created (new item field values, page range, PDF-slice preview) without writing anything to Zotero. An explicit `--commit` (CLI) / `"committed": true` (API) is required to actually create items and upload files — this is a hard-to-reverse, externally-visible write into a real Zotero library, so it does not happen silently.

**Per detected chapter, on commit:**

1. Slice the book PDF to `[pdf_start_index, pdf_end_index]` into a standalone file (`pypdf.PdfWriter` — already a project dependency, `pyproject.toml:17`). This always uses the PDF-index pair, never `citation_pages`.
2. Create a new `bookSection` item via `pyzotero` (`item_template("bookSection")` → `create_items(...)`, following the existing precedent in `scripts/openalex_import.py:332-429`), populating it with metadata **inherited from the book** for correct citation quality: `bookTitle` = book's title, `editor` = the book's own creators, plus `publisher`/`place`/`date`/`ISBN`/`language` copied down — and the chapter's own detected `title`/`author` from script 1 (left blank for manual fill-in if author-detection confidence was low, rather than guessing a wrong name into permanent metadata).
3. Set the new item's native `pages` field from `citation_pages` **only if script 1 reported it** (`page_mapping_confidence` other than `"unmappable"`); otherwise leave `pages` blank for manual entry — never derived from `pdf_start_index`/`pdf_end_index`, which are a different number space and would silently print the wrong citation page range.
4. Upload the sliced PDF as a child attachment (`attachment_simple(...)`, same precedent as step 2).
5. Write the links (`chapter_link_store.write_links`): `X-Contained-By` on the new chapter, append to `X-Contains` on the book, **and** set `X-Chapter-Pdf-Range` for this chapter to `[pdf_start_index, pdf_end_index]` on the book — this, not the `pages` field just set in step 3, is what §9's suppression logic actually reads.

The book's own item/attachment is never modified or deleted — it keeps its full PDF; new chapter items are strictly additive.

**Collection organization** — new `--target-collection` option (default `"Book Chapters"`):

- A top-level Zotero collection with this name is created on-demand if it doesn't already exist.
- Under it, a per-book subcollection is created (or reused, if already present) named with an author-year short label — e.g. `"Miller (2023)"`, or `"Smith et al. (1999)"` for 3+ authors — derived from the book's own creators/date. `backend/db/vector_store.py:42` (`_extract_lastnames`) already extracts normalized last names from a Zotero author-string list for Qdrant filtering; the label-formatting logic here reuses that helper for the last-name extraction, adding only the "et al." / year suffix on top.
- Both the top-level collection and the per-book subcollection are looked up before creation (`pyzotero`'s `collections()`/`collections_sub()`), so re-running script 4 against an already-processed book reuses the existing subcollection instead of creating a duplicate.
- Each newly created chapter item is added to its book's subcollection, in addition to remaining a normal top-level library item.

## 9. Indexing-pipeline change: suppress superseded book chunks

This is what actually makes retrieval prefer the chapter citation, closing the loop this whole project started from.

When `document_processor.py` indexes a **book** item that has an `X-Contains` list, it resolves each linked chapter's `X-Chapter-Pdf-Range` entry (via `chapter_link_store.parse_links`, §2) — **never** the chapter's Zotero `pages` field, which holds printed/citation numbers in a different, human-editable number space — and **excludes the book's own PDF pages whose index falls inside any linked chapter's range** from chunking. The book's index entry then only covers its true residual content (front matter, table of contents, index, and any chapters not yet linked or not yet range-localized), while each linked chapter's own attachment (with correct title/author metadata) independently covers its own pages.

A chapter present in `X-Contains` but with **no** `X-Chapter-Pdf-Range` entry (possible after retrofit-linking, §7) contributes no suppression at all — its pages remain part of the book's index until/unless the range is later established. This is the deliberate safe default: suppressing on a guessed range risks silently dropping unrelated book content from the index, which is worse than temporarily leaving one redundant chapter un-suppressed.

**Sanity checks before applying any range:** `pdf_end_index` must not exceed the book PDF's actual page count, and no two linked chapters' ranges may overlap — either violation is logged and that book's suppression is skipped entirely for the affected pass (safe default again: fall back to indexing the whole book rather than acting on a corrupted or manually-mistyped range) rather than applying a partially-nonsensical exclusion.

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
backend/evaluation/book-segmentation/  # ground-truth manifest.json + <name>.expected.json (§5); PDFs gitignored
tests/test_chapter_segmentation_accuracy.py  # scoring harness reading the above
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
- **Suppression (§9) depends on `X-Chapter-Pdf-Range` being both present and correct.** It is deliberately decoupled from the human-editable `pages` field to avoid the printed-number/PDF-index confusion described in §2/§5, but a wrong range written by script 4 (bad boundary detection) or script 3 (bad content-localization) would still suppress the wrong pages from the book's own index. Since script 4 only auto-creates high-confidence chapters, script 3 only writes a range when content-localization is confident (§7), and §9 applies bounds/overlap sanity checks before acting, this risk is bounded but not eliminated.
- **Retrofit-linked chapters without a successfully localized PDF range (§7) get no suppression at all** — the book keeps indexing those pages redundantly even after the identity link is established. This is an accepted, safe degradation rather than a bug: the alternative (guessing a range) risks suppressing unrelated content. Closing this fully would require either better OCR/text-extraction on the chapter's own attachment or a manual range-entry path — not built now.
- **Until scripts 1–4 are run, books stay unsegmented and unsuppressed.** Right now, applying this whole feature to a library is a manual, human-triggered act (run script 1, review, run script 4/3, wait for the next indexing pass). Folding it into the normal indexing pipeline itself — i.e. having a routine indexing run notice an unlinked book and auto-segment it inline, opt-in via a config flag — is intentionally **not** part of this design. It surfaced a real unresolved conflict: script 4 needs a write-scoped Zotero key at the exact moment of indexing to create chapter items, but unattended cron indexing only ever holds read-only keys (§3, `AutoIndexKeyStore`), by deliberate design. Deferred to a later iteration, once scripts 1–4 have proven reliable standalone — at that point the key-sourcing question needs an explicit answer (e.g. limiting auto-segment to on-demand runs that already carry a write key, versus accepting a new persistent write-key store as a scoped exception to the read-only-only rule), not just an inline code change.
