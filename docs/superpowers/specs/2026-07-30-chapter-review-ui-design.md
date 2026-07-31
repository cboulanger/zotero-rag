# Chapter Segmentation/Retrofit Review UI — Design Spec

## 1. Goal

The chapter-segmentation pipeline (`docs/chapter-segmentation.md`) already produces three kinds of "uncertain, needs a human" output, but has no way for an admin to act on them other than eyeballing a JSON file and re-running scripts with different flags:

1. **Low-confidence chapter boundaries** — `analyze_book_chapters.py`'s detected chapters below `upload_chapters.py`'s `--confidence-threshold` (default `0.90`), currently just skipped (`skipped_low_confidence`) rather than uploaded.
2. **Ambiguous retrofit matches** — `retrofit_chapter_links.py`'s `ambiguous` bucket: a chapter with multiple similarly-plausible candidate books, left unlinked rather than guessed.
3. **`needs_ocr` attachments** — `analyze_book_chapters.py` items with no extractable text layer at all, blocking segmentation until OCR'd.

This project adds a **review queue** that every dry-run/analyze invocation of these scripts automatically populates, and a small **admin-only web page** where a human approves, edits, or rejects each pending item — with the approved action (create the chapter item, write the link, run OCR) executed immediately through the same logic the scripts already use for a whole batch, just scoped to one item.

The same store and page also cover the flip side: the **confident** chapters and matches a dry run would already commit on its own (above the confidence threshold; unambiguous matches). Rather than trusting a threshold blindly, the admin gets a **commit queue** view listing exactly what a real `--commit` run would do, with a batch "Execute Selected" action and a per-item "Send to Review" escape hatch for anything that looks wrong despite passing the threshold — no in-place editing there, since an item that needs editing belongs in the review queue instead.

Out of scope for this spec (see §7): a plugin-side UI reimplementing this logic against the local Zotero database.

## 2. Architecture

```text
analyze/retrofit/upload (CLI or API, dry-run) --upsert--> review_queue.json
                                                     (bucket: review | commit)
                                                                |
                                                                v
                                        GET /api/chapter-linking/review/pending
                                                                |
                                                                v
                                              /admin/review (Jinja2 page)
                                        "Needs Review" view    |    "Ready to Commit" view
                                   approve/edit or reject      |    execute selected or send-to-review
                                                                v
                             existing per-item commit logic (commit_links / upload / OCR run)
                                                                |
                                                                v
                                                             Zotero
```

The queue is a side effect of the existing dry-run paths — nothing about their current `--output` JSON files or return shapes changes. No script pauses to wait for a human; review always happens later, against whatever has accumulated in the queue. The two page views ("Needs Review" and "Ready to Commit") are just different filters over the same store — an item's `bucket` determines which view it shows up in, and "Send to Review" is nothing more than flipping that field.

## 3. Data model & storage

**New module** `backend/services/review_queue_store.py`, mirroring `chapter_link_store.py`'s role as the shared module other services import from. Backing file: `data/system/review_queue.json` — unencrypted (unlike `autoindex_keys.json`; nothing stored here is a credential), structured as:

```json
{
  "groups/6297749": {
    "chapter:BOOK1:45-68": {
      "type": "chapter",
      "bucket": "review",
      "status": "pending",
      "created_at": "2026-07-30T12:00:00Z",
      "updated_at": "2026-07-30T12:00:00Z",
      "payload": { "book_key": "BOOK1", "title": "...", "authors": [...],
                   "pdf_start_index": 45, "pdf_end_index": 68,
                   "confidence": 0.72, "attachment_key": "ATT1" }
    },
    "chapter:BOOK5:10-30": {
      "type": "chapter",
      "bucket": "commit",
      "status": "pending",
      "payload": { "book_key": "BOOK5", "title": "...", "authors": [...],
                   "pdf_start_index": 10, "pdf_end_index": 30,
                   "confidence": 0.96, "attachment_key": "ATT5" }
    },
    "match:CHAP7": {
      "type": "match",
      "bucket": "review",
      "status": "pending",
      "payload": { "chapter_key": "CHAP7",
                   "candidates": [{"key": "BOOK2", "title": "...", "year": "2019"},
                                  {"key": "BOOK3", "title": "...", "year": "2019"}] }
    },
    "match:CHAP8": {
      "type": "match",
      "bucket": "commit",
      "status": "pending",
      "payload": { "chapter_key": "CHAP8", "book_key": "BOOK6", "score": 0.94 }
    },
    "ocr:ATT9": {
      "type": "ocr",
      "bucket": "review",
      "status": "pending",
      "payload": { "book_key": "BOOK4", "attachment_key": "ATT9" }
    }
  }
}
```

Keyed first by library slug, then by a deterministic `queue_id` (`chapter:<book_key>:<pdf_start-pdf_end>`, `match:<chapter_key>`, `ocr:<attachment_key>`) so repeat analyze/retrofit runs are idempotent: an unchanged pending item is upserted in place (refreshed `payload`/`updated_at`, `status` untouched), and an item already non-`pending` is left alone rather than reset — a re-run must not resurrect a decision the admin already made. `bucket` (`"review"` | `"commit"`) determines which page view an entry shows up in (§6); only `chapter` and `match` entries ever have `bucket: "commit"` — an `ocr` entry is inherently a review decision (there's no "confident enough to auto-run OCR" concept) and is always `bucket: "review"`. `review_queue_store` exposes `upsert_many(slug, entries)`, `list_pending(slug, bucket=None, type=None)`, `set_status(slug, queue_id, status)`, `set_bucket(slug, queue_id, bucket)` (the "Send to Review" action), matching the read/modify/write-whole-file pattern already used by `chapter_link_store`'s callers (no concurrent-writer concerns beyond what that pattern already tolerates — this is an admin tool, not a high-throughput path).

## 4. API endpoints

New routes on the existing `backend/api/chapter_linking.py` router, gated by the same admin/owner-of-`AUTHORIZED_GROUP_ID` check already used by `/api/autoindex/scheduler/*` (an `X-Zotero-API-Key` header):

- `GET /api/chapter-linking/review/pending?library_slug=...&bucket=review|commit` — pending entries for one library, grouped by `type`; `bucket` filters to one page view (omit for both).
- `POST /api/chapter-linking/review/{queue_id}/approve` — body: `library_slug`, plus optional edits (`pdf_start_index`/`pdf_end_index`/`title`/`authors` for a `chapter` entry, `book_key` — the chosen candidate — for a `match` entry). Used by the "Needs Review" view's ✓/✎ actions on a single `bucket: "review"` entry. Dispatches by `type`:
  - `chapter` → slice the PDF and create the `bookSection` for this one chapter, reusing `upload_chapters.py`'s existing per-chapter logic (confidence threshold does not apply here — the human already decided).
  - `match` → `chapter_retrofit.commit_links()` with a single-entry `would_link` list built from the approved `book_key`.
  - `ocr` → starts an OCR job for that attachment (reusing `ocr_attachments.py`'s per-item logic) and, on completion, re-runs analysis for that book so it either resolves (chapters detected, queue entry cleared) or is re-upserted with fresh data (still `needs_ocr`, or now a `chapter` entry).
  - On success, marks the entry `approved` (kept in the store for audit, excluded from `list_pending`).
- `POST /api/chapter-linking/review/{queue_id}/reject?library_slug=...` — marks `rejected`, no Zotero write. Valid for either bucket (a "Ready to Commit" item can be rejected outright, same as "Send to Review" but without a second review step).
- `POST /api/chapter-linking/review/execute` — body: `library_slug`, `queue_ids: [...]`. Used by the "Ready to Commit" view's "Execute Selected" button (§6). Runs the same per-`type` dispatch as `approve` above (no edits — `bucket: "commit"` entries are never edited, per §1) for each `queue_id` in the list, isolating failures per-item exactly like `chapter_retrofit.commit_links()` already does internally, marking each successfully-written entry `approved` (same terminal status `approve` uses — a commit-queue item and a review-queue item both end up "approved" once written, they just took different paths to get there), and returns `{"executed": [...], "failed": [{"queue_id", "error"}]}`. Only valid for entries with `bucket: "commit"` and `status: "pending"`; a mismatched entry is reported as a failure rather than silently skipped.
- `POST /api/chapter-linking/review/{queue_id}/send-to-review?library_slug=...` — flips `bucket` from `"commit"` to `"review"`, `status` stays `"pending"`. The one-item escape hatch on the "Ready to Commit" view — no edits, just a move (an item that needs editing belongs in "Needs Review" from there on, using the existing ✎ action).

## 5. Script integration

`analyze_book_chapters.py`, `retrofit_chapter_links.py` (dry-run path), and `upload_chapters.py` (dry-run path) — both their CLI entry points and API job equivalents — each get one added call at the end of their existing run, upserting into `review_queue_store`:

- **Uncertain → `bucket: "review"`**: `needs_ocr` + low-confidence chapters (from analyze), `ambiguous` matches (from retrofit), `skipped_low_confidence` (from upload).
- **Confident → `bucket: "commit"`**: chapters upload's dry run would create (above `--confidence-threshold`, i.e. everything in its dry-run output *not* in `skipped_low_confidence`), and retrofit's `would_link` matches.

Purely additive; no existing flag, output shape, or default behavior changes. Note the confident-chapter bucket is sourced from **upload's** dry run, not analyze's raw output — analyze has no concept of a confidence threshold on its own (that's `upload_chapters.py`'s `--confidence-threshold` flag), so it only ever contributes to the review bucket (`needs_ocr` / low-confidence).

## 6. Web UI

Server-rendered with Jinja2, extending the existing `backend/templates/` precedent (currently only used by the public-query form), served at `/admin/review`:

- A library-slug picker at the top (the admin key's accessible libraries), and a top-level switch between the **Needs Review** and **Ready to Commit** views.
- **Needs Review** view — tabs **Chapters** / **Ambiguous Matches** / **Needs OCR**, each a table of that type's `bucket: "review"` pending entries, per the approved mockup layout:
  - **Chapters tab**: title, authors, page range, confidence, a short extracted-text snippet around the boundary (already available from analysis — no new rendering pipeline), and a link to the Zotero web reader for the attachment being sliced (`https://www.zotero.org/<library>/items/<parent-item-key>/attachment/<attachment-item-key>/reader`, deep-linked to the boundary page if the reader's URL scheme supports a page anchor — confirm exact query param during implementation) so the admin can inspect the actual PDF before deciding. Row actions: ✓ approve as-is, ✎ edit page range/title inline then approve, ✕ reject.
  - **Ambiguous Matches tab**: the chapter's title plus each candidate book (title/year) with a pick-one control, and a link to each candidate's plain Zotero item page for inspection. Row actions: approve with the picked candidate, or reject (no match).
  - **Needs OCR tab**: book title and attachment, a "Run OCR" action (kicks the OCR job described in §4), and the same reader link for a quick look at whether the scan is even legible before spending OCR time on it.
- **Ready to Commit** view — tabs **Chapters** / **Matches** (no Needs OCR tab — §3), each a table of that type's `bucket: "commit"` pending entries: same descriptive columns as the corresponding "Needs Review" tab (title/pages/confidence, or chapter↔book match/score) plus the same reader/item links for a final look, but **no ✎ edit control** — just a per-row checkbox (default checked) and a "Send to Review" action. A single "Execute Selected" button above the table posts the checked `queue_ids` to `POST .../review/execute` and reports per-item success/failure.
- Actions are plain form posts/fetch calls to §4's endpoints; a successful approve/reject/execute/send-to-review removes the affected row(s) from view without a full page reload (exact mechanism — redirect vs. fetch+DOM update — left to the implementation plan).

## 7. Future extensions (not built now)

Because review logic lives behind API endpoints rather than inside page-rendering code, a future plugin-side UI — e.g. a "right-click a book item in Zotero to segment it" feature, reimplementing detection client-side against the local Zotero database for speed — can drive the same `review_queue`/approve/reject endpoints instead of this web page, or push directly into the same `review_queue.json` shape via a local call. This keeps one review surface regardless of whether the triggering run happened server-side or client-side. Left as its own future design, not specified further here.

## 8. Testing

Standard `backend/tests/` unit coverage for `review_queue_store.py` (upsert idempotency, status transitions, bucket transitions, per-library isolation) and the new API endpoints (auth gating, dispatch-by-type, edit-then-approve payload handling, `execute`'s per-item failure isolation, `send-to-review`'s bucket flip and its rejection of an already non-`pending` entry), following the existing patterns in `test_chapter_link_store.py` / `test_chapter_linking_api.py`. The web page itself is exercised manually (no existing precedent for Jinja2-page testing in this codebase — the public-query form has none either); a `container`-marked smoke test is not warranted for a page addition alone.
