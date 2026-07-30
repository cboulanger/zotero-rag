# Chapter Segmentation/Retrofit Review UI — Design Spec

## 1. Goal

The chapter-segmentation pipeline (`docs/chapter-segmentation.md`) already produces three kinds of "uncertain, needs a human" output, but has no way for an admin to act on them other than eyeballing a JSON file and re-running scripts with different flags:

1. **Low-confidence chapter boundaries** — `analyze_book_chapters.py`'s detected chapters below `upload_chapters.py`'s `--confidence-threshold` (default `0.90`), currently just skipped (`skipped_low_confidence`) rather than uploaded.
2. **Ambiguous retrofit matches** — `retrofit_chapter_links.py`'s `ambiguous` bucket: a chapter with multiple similarly-plausible candidate books, left unlinked rather than guessed.
3. **`needs_ocr` attachments** — `analyze_book_chapters.py` items with no extractable text layer at all, blocking segmentation until OCR'd.

This project adds a **review queue** that every dry-run/analyze invocation of these scripts automatically populates, and a small **admin-only web page** where a human approves, edits, or rejects each pending item — with the approved action (create the chapter item, write the link, run OCR) executed immediately through the same logic the scripts already use for a whole batch, just scoped to one item.

Out of scope for this spec (see §7): a plugin-side UI reimplementing this logic against the local Zotero database.

## 2. Architecture

```text
analyze/retrofit/upload (CLI or API, dry-run) --upsert--> review_queue.json
                                                                |
                                                                v
                                        GET /api/chapter-linking/review/pending
                                                                |
                                                                v
                                              /admin/review (Jinja2 page)
                                                                |
                                          approve/edit (POST) or reject (POST)
                                                                |
                                                                v
                             existing per-item commit logic (commit_links / upload / OCR run)
                                                                |
                                                                v
                                                             Zotero
```

The queue is a side effect of the existing dry-run paths — nothing about their current `--output` JSON files or return shapes changes. No script pauses to wait for a human; review always happens later, against whatever has accumulated in the queue.

## 3. Data model & storage

**New module** `backend/services/review_queue_store.py`, mirroring `chapter_link_store.py`'s role as the shared module other services import from. Backing file: `data/system/review_queue.json` — unencrypted (unlike `autoindex_keys.json`; nothing stored here is a credential), structured as:

```json
{
  "groups/6297749": {
    "chapter:BOOK1:45-68": {
      "type": "chapter",
      "status": "pending",
      "created_at": "2026-07-30T12:00:00Z",
      "updated_at": "2026-07-30T12:00:00Z",
      "payload": { "book_key": "BOOK1", "title": "...", "authors": [...],
                   "pdf_start_index": 45, "pdf_end_index": 68,
                   "confidence": 0.72, "attachment_key": "ATT1" }
    },
    "match:CHAP7": {
      "type": "match",
      "status": "pending",
      "payload": { "chapter_key": "CHAP7",
                   "candidates": [{"key": "BOOK2", "title": "...", "year": "2019"},
                                  {"key": "BOOK3", "title": "...", "year": "2019"}] }
    },
    "ocr:ATT9": {
      "type": "ocr",
      "status": "pending",
      "payload": { "book_key": "BOOK4", "attachment_key": "ATT9" }
    }
  }
}
```

Keyed first by library slug, then by a deterministic `queue_id` (`chapter:<book_key>:<pdf_start-pdf_end>`, `match:<chapter_key>`, `ocr:<attachment_key>`) so repeat analyze/retrofit runs are idempotent: an unchanged pending item is upserted in place (refreshed `payload`/`updated_at`, `status` untouched), and an item already `approved`/`rejected` is left alone rather than reset to `pending` — a re-run must not resurrect a decision the admin already made. `review_queue_store` exposes `upsert_many(slug, entries)`, `list_pending(slug, type=None)`, `set_status(slug, queue_id, status)`, matching the read/modify/write-whole-file pattern already used by `chapter_link_store`'s callers (no concurrent-writer concerns beyond what that pattern already tolerates — this is an admin tool, not a high-throughput path).

## 4. API endpoints

New routes on the existing `backend/api/chapter_linking.py` router, gated by the same admin/owner-of-`AUTHORIZED_GROUP_ID` check already used by `/api/autoindex/scheduler/*` (an `X-Zotero-API-Key` header):

- `GET /api/chapter-linking/review/pending?library_slug=...` — pending entries for one library, grouped by `type`.
- `POST /api/chapter-linking/review/{queue_id}/approve` — body: `library_slug`, plus optional edits (`pdf_start_index`/`pdf_end_index`/`title`/`authors` for a `chapter` entry, `book_key` — the chosen candidate — for a `match` entry). Dispatches by `type`:
  - `chapter` → slice the PDF and create the `bookSection` for this one chapter, reusing `upload_chapters.py`'s existing per-chapter logic (confidence threshold does not apply here — the human already decided).
  - `match` → `chapter_retrofit.commit_links()` with a single-entry `would_link` list built from the approved `book_key`.
  - `ocr` → starts an OCR job for that attachment (reusing `ocr_attachments.py`'s per-item logic) and, on completion, re-runs analysis for that book so it either resolves (chapters detected, queue entry cleared) or is re-upserted with fresh data (still `needs_ocr`, or now a `chapter` entry).
  - On success, marks the entry `approved` (kept in the store for audit, excluded from `list_pending`).
- `POST /api/chapter-linking/review/{queue_id}/reject?library_slug=...` — marks `rejected`, no Zotero write.

## 5. Script integration

`analyze_book_chapters.py`, `retrofit_chapter_links.py` (dry-run path), and `upload_chapters.py` (dry-run path) — both their CLI entry points and API job equivalents — each get one added call at the end of their existing run, upserting their uncertain items (`needs_ocr` + low-confidence chapters; `ambiguous` matches; `skipped_low_confidence`) into `review_queue_store`. Purely additive; no existing flag, output shape, or default behavior changes.

## 6. Web UI

Server-rendered with Jinja2, extending the existing `backend/templates/` precedent (currently only used by the public-query form), served at `/admin/review`:

- A library-slug picker at the top (the admin key's accessible libraries).
- Tabs — **Chapters** / **Ambiguous Matches** / **Needs OCR** — each a table of that type's pending entries, per the approved mockup layout.
- **Chapters tab**: title, authors, page range, confidence, a short extracted-text snippet around the boundary (already available from analysis — no new rendering pipeline), and a link to the Zotero web reader for the attachment being sliced (`https://www.zotero.org/<library>/items/<parent-item-key>/attachment/<attachment-item-key>/reader`, deep-linked to the boundary page if the reader's URL scheme supports a page anchor — confirm exact query param during implementation) so the admin can inspect the actual PDF before deciding. Row actions: ✓ approve as-is, ✎ edit page range/title inline then approve, ✕ reject.
- **Ambiguous Matches tab**: the chapter's title plus each candidate book (title/year) with a pick-one control, and a link to each candidate's plain Zotero item page for inspection. Row actions: approve with the picked candidate, or reject (no match).
- **Needs OCR tab**: book title and attachment, a "Run OCR" action (kicks the OCR job described in §4), and the same reader link for a quick look at whether the scan is even legible before spending OCR time on it.
- Actions are plain form posts/fetch calls to §4's endpoints; a successful approve/reject removes the row from view without a full page reload (exact mechanism — redirect vs. fetch+DOM update — left to the implementation plan).

## 7. Future extensions (not built now)

Because review logic lives behind API endpoints rather than inside page-rendering code, a future plugin-side UI — e.g. a "right-click a book item in Zotero to segment it" feature, reimplementing detection client-side against the local Zotero database for speed — can drive the same `review_queue`/approve/reject endpoints instead of this web page, or push directly into the same `review_queue.json` shape via a local call. This keeps one review surface regardless of whether the triggering run happened server-side or client-side. Left as its own future design, not specified further here.

## 8. Testing

Standard `backend/tests/` unit coverage for `review_queue_store.py` (upsert idempotency, status transitions, per-library isolation) and the new API endpoints (auth gating, dispatch-by-type, edit-then-approve payload handling), following the existing patterns in `test_chapter_link_store.py` / `test_chapter_linking_api.py`. The web page itself is exercised manually (no existing precedent for Jinja2-page testing in this codebase — the public-query form has none either); a `container`-marked smoke test is not warranted for a page addition alone.
