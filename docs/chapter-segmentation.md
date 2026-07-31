# Chapter Segmentation & Book/Chapter Linking

Link edited-book (`book`) items to their individual chapters
(`bookSection` items) in a Zotero library, so retrieval and citations
prefer the specific chapter over the whole book. This is a one-time (or
occasional) maintenance operation an administrator runs against a
library — not something end users trigger from the plugin.

An edited-volume library often has a `book` item whose attachment indexes
the whole PDF, plus separate `bookSection` items for individual chapters
that overlap the same text. Without a link between them, both compete as
independent "documents" in search results and citations. This feature
**detects** chapter boundaries inside a book's PDF, **links** existing
book/chapter pairs, and **creates** new `bookSection` items from a
detected chapter range — nothing runs automatically, it's a deliberate,
admin-triggered pass using the CLI commands and web UI below.

## Prerequisites

- Backend dependencies installed (`uv sync`) and, for scanned/OCR'd
  books, the Kreuzberg sidecar available (same requirement as the app's
  normal document extraction — see [docs/architecture.md](architecture.md)).
- A Zotero API key for the library you're processing
  (<https://www.zotero.org/settings/keys>) — **write-scoped**, so you
  don't have to swap keys partway through the workflow below (a
  read-only key also works for the read-only steps, see the
  [Reference](#reference) for exactly which command needs which).
- The library's **slug**: `groups/<numeric-id>` or `users/<numeric-id>`
  (the same form used elsewhere in this project — e.g. the group ID shown
  in a Zotero group's URL).

## Quick start

Four commands populate two review queues; you then resolve everything
from a web page instead of hand-editing JSON files or tuning thresholds
blindly. Replace `groups/6297749` and `<api-key>` throughout.

```bash
# 1. Detect chapter boundaries in every book's PDF.
uv run chapter-analyze \
  --library-slug groups/6297749 --api-key <api-key> \
  --output .local/analysis.json

# 2. Preview what would be created from the detected chapters --
#    queues low-confidence chapters for review, confident ones for commit.
uv run chapter-segment-upload \
  --library-slug groups/6297749 --api-key <api-key> \
  --input .local/analysis.json

# 3. Preview matches for any chapters already catalogued separately from
#    their book -- queues ambiguous matches for review, confident ones
#    for commit.
uv run chapter-retrofit-link \
  --library-slug groups/6297749 --api-key <api-key>

# 4. If step 1 reported any needs_ocr books, OCR them, re-run analyze
#    for just those books to pick up the extracted text (a separate
#    --output so it doesn't overwrite the full-library analysis.json),
#    then re-run step 2 for just those books to queue their chapters:
uv run chapter-ocr \
  --library-slug groups/6297749 --api-key <api-key> \
  --input .local/analysis.json --output .local/ocr_results.json
uv run chapter-analyze \
  --library-slug groups/6297749 --api-key <api-key> \
  --item-keys <book-item-keys-that-needed-ocr> \
  --output .local/analysis-ocr-followup.json
uv run chapter-segment-upload \
  --library-slug groups/6297749 --api-key <api-key> \
  --input .local/analysis-ocr-followup.json
```

Then open **`/admin/review`** (see [below](#reviewing-uncertain-and-confident-results))
and work through what steps 2–3 just queued — approve, edit, reject, or
commit each item — instead of re-running any command with `--commit`.

Each command has more flags than shown here (confidence thresholds,
target collections, LLM fallback, etc.) — see the [Reference](#reference)
below for the full set, or run any command with `--help`.

## Reviewing uncertain and confident results

Every dry-run/analyze invocation of the commands above (CLI or API)
automatically feeds two persistent, per-library queues: a **review
queue** for items that need a human decision (low-confidence chapter
boundaries, ambiguous retrofit matches, `needs_ocr` attachments) and a
**commit queue** for items a real `--commit` run would already write on
its own, for a final look before it happens.

An admin can act on both from `/admin/review`: enter a library slug and
an admin Zotero API key (must belong to an owner/admin of the server's
`AUTHORIZED_GROUP_ID`), then switch between the **Needs Review** view
(approve as-is, edit page range/title/candidate then approve, reject, or
trigger OCR) and the **Ready to Commit** view (select confident items and
"Execute Selected", or "Send to Review" to defer one instead). Approving
or executing an entry writes to Zotero immediately, using the exact same
logic the scripts themselves use — see
`docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md` for the
full design.

The same functionality is available via `GET/POST
/api/chapter-linking/review/*` for scripting or a future non-web client
(see [Running via the API](#running-via-the-api)).

**Known limitation:** triggering "Run OCR" on a `needs_ocr` entry runs
OCR and re-analyzes the book, but does not automatically re-populate the
chapter/commit queue from the result — that requires a separate
`chapter-segment-upload` dry-run (or `POST /chapter-linking/segment-upload`)
pass over the freshly-OCR'd book afterward. Closing this loop
automatically would require extending the queue entry's stored payload
with the `confidence_threshold`/`target_collection` the eventual upload
pass should use, which isn't implemented yet.

**A multi-chapter interaction to know about:** approving one chapter of a
multi-chapter book writes that book's `X-Contains` link immediately, and
[Analyze skips already-linked books by default](#1-analyze) — so a book
with some chapters approved via the queue and others still pending review
won't get those pending ones refreshed by a plain re-run of Analyze; pass
`--relink --item-keys <that book>` if you need fresh analyze data for its
remaining chapters.

---

## Reference

Full flag reference for each command, plus the REST API. Every command
also accepts `--max-items <N>` to cap how many items it processes in one
run — useful for a first trial against a large library before committing
to a full pass — and `--help` for the complete list.

### 1. Analyze

`uv run chapter-analyze` (`scripts/analyze_book_chapters.py`) — **read-only key is enough.**

Scans every `book`-type item in the library, downloads its PDF attachment,
and tries to detect chapter boundaries from the text (a table-of-contents
listing, cross-referenced against where each chapter's title/author
actually starts in the body — never assumed from a page-number offset
alone).

```bash
uv run chapter-analyze \
  --library-slug groups/6297749 \
  --api-key <read-only-zotero-key> \
  --output .local/analysis.json
```

Key flags:

| Flag | Purpose |
| --- | --- |
| `--relink` | Re-analyze books that already have an `X-Contains` link (normally skipped) |
| `--item-keys <a,b,c>` | Restrict to specific book item keys instead of scanning the whole library |
| `--llm-fallback` | Enable the LLM-based fallback for chapters the text heuristics find nothing or are ambiguous about (slower, calls a configured LLM API — see [Presets](presets.md)) |
| `--auto-select-model` | With `--llm-fallback`: retry across the active preset's currently-available models instead of a single fixed one, useful if the preset's default model is temporarily overloaded or unreachable |
| `--cache-dir` | Directory to check for already-OCR'd page text (default `data/ocr_cache`, same as [OCR](#2-ocr)'s `--cache-dir`) — lets a re-run after OCR-ing a scanned book pick up its text automatically instead of reporting `needs_ocr` again |
| `--max-items <N>` | Cap how many books are processed |

The output JSON (written to `--output`, or printed to stdout) has one
entry per book attachment, each with `chapters` (title, authors,
`pdf_start_index`/`pdf_end_index`, `citation_pages`, a confidence score,
and `source`: `"heuristic"` or `"llm"`) and a top-level `needs_ocr` flag
for attachments with no extractable text layer at all (a scanned book with
no OCR yet) — see [OCR](#2-ocr) for those. This file is the direct input
to [Segment & upload](#4-segment-and-upload).

A book already linked (`X-Contains` present) is skipped by default —
this makes repeat runs against a growing library fast and safe to re-run
regularly, only touching newly-added books unless you pass `--relink`.

### 2. OCR

`uv run chapter-ocr` (`scripts/ocr_attachments.py`) — **read-only key is enough.**

For attachments Analyze flagged `needs_ocr: true` (a scanned PDF with no
text layer), runs OCR page-by-page and caches the extracted text to disk
so repeat runs don't re-OCR the same file.

```bash
uv run chapter-ocr \
  --library-slug groups/6297749 \
  --api-key <read-only-zotero-key> \
  --input .local/analysis.json \
  --output .local/ocr_results.json
```

`--input` is Analyze's output — only its `needs_ocr: true` entries are
processed. `--cache-dir` (default `data/ocr_cache`) is where the OCR'd
per-page text is cached, keyed by the PDF's content hash. Re-running
[Analyze](#1-analyze) afterward (with the same `--cache-dir`, the default)
reads this cache back in automatically for any book still lacking a text
layer, so chapters detectable from the OCR'd text now show up without any
extra flag.

### 3. Retrofit-link

`uv run chapter-retrofit-link` (`scripts/retrofit_chapter_links.py`) — **write-scoped key required.**

For libraries that already have separately-catalogued `book` and
`bookSection` items (added by hand, or imported, before this feature
existed) — matches each unlinked chapter to its book by title/year
similarity and writes the `X-Contains`/`X-Contained-By` link. Deliberately
biased toward precision: an ambiguous or low-confidence match is reported
for manual review rather than linked. **Defaults to a dry run** — pass
`--commit` to actually write to Zotero.

```bash
# Preview first (no changes made)
uv run chapter-retrofit-link \
  --library-slug groups/6297749 \
  --api-key <write-scoped-zotero-key> \
  --output .local/retrofit.json

# Then actually write the links
uv run chapter-retrofit-link \
  --library-slug groups/6297749 \
  --api-key <write-scoped-zotero-key> \
  --output .local/retrofit.json \
  --commit
```

A `--commit` run always re-fetches and re-matches the whole library from
scratch, same as a dry run — this is what a large library's `everything()`
call can make slow (the fuzzy matching itself is fast; the full-library
fetch dominates). To skip straight to writing a dry run's already-reviewed
matches instead, pass `--input` pointing at that dry run's `--output` file:

```bash
uv run chapter-retrofit-link \
  --library-slug groups/6297749 \
  --api-key <write-scoped-zotero-key> \
  --input .local/retrofit.json \
  --commit
```

`--input` re-fetches only the two specific items involved in each link (to
get their current version before writing), never the whole library, and is
only valid together with `--commit` — a dry run always matches fresh.

Every written link also sets a native Zotero "Related" connection between
the two items (visible in the Zotero client's Related tab), independent of
the `Extra`-field convention above. `--target-collection <name>`
optionally also files the *chapter* side of each written link into a
`<name>/<Author (Year)>` subcollection (created if it doesn't exist yet,
reused otherwise) — the same scheme [Segment & upload](#4-segment-and-upload)
uses for its own newly created chapters, but off by default here, since
this command links items you've already organized yourself.

The output reports four buckets: `linked` (written this run, with a match
score — empty unless `--commit` was passed), `would_link` (what `--commit`
would write, populated only in dry-run mode), `ambiguous` (multiple
similarly-plausible book candidates — needs a human to pick), and
`no_match` (no book found at all). Only confident matches are ever
written; ambiguous and unmatched chapters are always left untouched.

### 4. Segment and upload

`uv run chapter-segment-upload` (`scripts/upload_chapters.py`) — **dry-run works with a read-only key; `--commit` needs write-scoped.**

Physically slices a book's PDF per Analyze's detected chapter ranges and
creates new `bookSection` items in Zotero, with metadata (publisher,
place, date, ISBN, language, and the book's own creators as `editor`)
inherited from the book. **Defaults to a dry run** — pass `--commit` to
actually write to Zotero.

```bash
# Preview first (no changes made)
uv run chapter-segment-upload \
  --library-slug groups/6297749 \
  --api-key <zotero-key> \
  --input .local/analysis.json

# Then actually create the items
uv run chapter-segment-upload \
  --library-slug groups/6297749 \
  --api-key <write-scoped-zotero-key> \
  --input .local/analysis.json \
  --commit
```

`--input` is Analyze's output. Chapters below `--confidence-threshold`
(default `0.90`, calibrated against a hand-verified evaluation set — see
[backend/evaluation/book-segmentation/README.md](../backend/evaluation/book-segmentation/README.md))
are skipped rather than uploaded with a guessed boundary — the dry-run
output lists them separately (`skipped_low_confidence`) so you can review
before deciding whether to lower the threshold, or resolve them
individually via [`/admin/review`](#reviewing-uncertain-and-confident-results)
instead. `--target-collection` (default `"Book Chapters"`) is where new
chapter items are filed.

### Running via the API

Each command above has a matching endpoint that starts an async job and
returns a `job_id` immediately: `POST /api/chapter-linking/analyze`,
`POST /api/chapter-linking/ocr`, `POST /api/chapter-linking/retrofit-link`,
`POST /api/chapter-linking/segment-upload`. Poll
`GET /api/chapter-linking/jobs/{job_id}` for progress and the final
result. This is the same request/response shape as the CLI's flags
(e.g. the analyze endpoint's JSON body takes `library_slug`, `api_key`,
`item_keys`, `relink`, `max_items`, `enable_llm_fallback`,
`auto_select_model`, and `ocr_cache_dir`; the retrofit-link endpoint's body
takes a `committed` flag, mirroring the CLI's `--commit` and defaulting to
the same dry-run behavior; an optional `would_link` field mirroring the
CLI's `--input` (pass a prior dry run's `would_link` response array
alongside `committed: true` to skip straight to writing those matches
instead of re-fetching and re-matching the whole library); and an optional
`target_collection` field mirroring the CLI's `--target-collection`) —
useful for driving this from an external scheduler or admin tool instead
of a shell.

The review queue itself is also driven entirely via `GET/POST
/api/chapter-linking/review/*` — see
[Reviewing uncertain and confident results](#reviewing-uncertain-and-confident-results)
above.
