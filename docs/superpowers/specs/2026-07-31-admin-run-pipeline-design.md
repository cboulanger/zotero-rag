# Admin "Run Pipeline" Page — Design Spec

**Status:** Approved, ready for implementation plan.

## 1. Problem

The chapter-segmentation pipeline (analyze → OCR → segment-upload →
retrofit-link) is fully exposed over the API (`backend/api/chapter_linking.py`)
and reviewable via `/admin/review` (`docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md`),
but *running* the pipeline still requires either the CLI scripts
(`docs/chapter-segmentation.md`) or hand-crafted `curl`/`fetch` calls against
the four job-starting endpoints. There is no web UI to kick off a run.

This spec adds a second admin page, `/admin/run`, that lets an admin trigger
a full dry-run pass over a library from a browser with three inputs
(library slug, API key, optional max-items), watch progress, and land on
`/admin/review` with results waiting.

## 2. Why this isn't four independent buttons

The four scripts are not independent from the caller's point of view:

- `segment-upload`'s `analyses` parameter is literally `analyze`'s output
  list (`backend/services/chapter_upload.py::run()`'s docstring: "`analyses`
  is script 1's output list"). It cannot run first.
- `ocr`'s `attachment_specs` parameter is a list of `{item_key,
  attachment_key}` pairs — there is no "OCR everything" mode. The only
  place those pairs come from is `analyze`'s `needs_ocr: true` entries.
- A book flagged `needs_ocr` produces zero chapters from `segment-upload`
  (its `attachments_out` entry has no `chapters` key, and
  `chapter_upload.run()` does `analysis.get("chapters", [])`) unless it's
  OCR'd and re-analyzed first.

So the page encodes a real pipeline, not a button bar.

## 3. Flow

All stages run in dry-run mode (`committed`/`commit: false` on every call
that supports it) — this page only *populates* the review/commit queues.
Committing happens exclusively from `/admin/review`, unchanged.

```
1. POST /api/chapter-linking/analyze        {library_slug, api_key, max_items}
   → poll /api/chapter-linking/jobs/{id} until done
   → result.attachments: list[dict]

2. needs_ocr = [a for a in attachments if a.get("needs_ocr")]
   if needs_ocr:
     2a. POST /api/chapter-linking/ocr      {library_slug, api_key,
                                              attachment_specs: [{item_key, attachment_key} ...]}
         → poll to done
     2b. POST /api/chapter-linking/analyze  {library_slug, api_key,
                                              item_keys: [book keys from needs_ocr]}
         → poll to done
         → result2.attachments
     2c. merge result2.attachments into the stage-1 list, replacing entries
         by item_key (a book can still come back needs_ocr: true if OCR
         failed to extract usable text — left as-is, self-heals on a later
         run the same way the existing review-page "Run OCR" button does)

3. POST /api/chapter-linking/segment-upload {library_slug, api_key,
                                              analyses: <merged list from 1/2>,
                                              committed: false, max_items}
   → poll to done
   (populates the chapter review/commit queue — chapter_upload.run() defaults:
   confidence_threshold=0.90, target_collection="Book Chapters")

4. POST /api/chapter-linking/retrofit-link  {library_slug, api_key,
                                              committed: false, max_items}
   → poll to done
   (populates the match review/commit queue)

5. redirect to /admin/review?library_slug=<slug>
```

Stages 2a/2b only appear (as progress rows) when stage 1 actually returns a
`needs_ocr` entry. A stage that reports `status: "error"` halts the sequence:
its row shows the error message, later stages never start, and there is no
redirect.

`max_items` is passed through verbatim to `analyze` and `retrofit-link` (the
two calls that scan the library). The OCR and re-analyze calls use explicit
`item_keys`/`attachment_specs` instead, so they need no separate cap —
they're already scoped to exactly the flagged books. `segment-upload`
also accepts `max_items`, applied to the merged analyses list actually being
uploaded from — passed through unchanged as an extra safety cap, not because
a second value is needed.

## 4. Components

**`backend/api/admin_pages.py`** — add one route:

```python
@router.get("/run", response_class=HTMLResponse, summary="Chapter-linking pipeline runner (admin page)")
async def run_page(request: Request) -> HTMLResponse:
    return _templates.TemplateResponse("admin_run.html", {"request": request})
```

Same access-control note as `/admin/review`'s existing docstring: no
server-side auth on the page route itself (`/admin/*` isn't covered by
`api_key_middleware`, which only gates `/api/*`); the four pipeline
endpoints it calls already require a valid, gate-approved Zotero API key
(`resolve_zotero_identity`, enforced by that same middleware on every
`/api/*` call) — the same trust boundary the CLI scripts and `/admin/review`
already operate under. Unlike `/admin/review`'s `/review/*` endpoints, the
pipeline endpoints (`analyze`/`ocr`/`retrofit-link`/`segment-upload`) do
**not** additionally require group-admin status — this page doesn't change
that; it's the existing model for those four endpoints.

**`backend/templates/admin_run.html`** (new) — self-contained HTML/vanilla
JS, same style as `admin_review.html`: a credentials form (slug, password-
type API key field, max-items number field), a "Run pipeline" button, and a
list of stage rows. Each row shows a label, a spinner/status glyph, and the
latest `message`/`progress` from polling that stage's job. Polling reuses
the exact `apiFetch()`/job-status-poll pattern already in `admin_review.html`
(`GET /api/chapter-linking/jobs/{job_id}`, same `JobStatusResponse` shape:
`status`/`progress`/`message`/`result`/`error`).

No new backend endpoints, Pydantic models, or changes to
`backend/services/job_tracker.py` — every call this page makes already
exists and is already tested.

**Cross-links:** a one-line nav link is added in each template pointing to
the other (`/admin/run` ↔ `/admin/review`), so the pair is discoverable from
either entry point.

## 5. Error handling

- Any stage's job resolving to `status: "error"` stops the pipeline. The
  failing stage's row shows the `error` string; remaining stage rows stay
  in their "not started" state; no redirect occurs.
- A `fetch()` failure (network error, non-2xx on the *start* call itself,
  before a `job_id` even exists) is treated the same way — shown on the
  current stage's row, pipeline halted.
- Bad/missing credentials surface as whatever the underlying endpoint
  already returns (401/403 from `resolve_zotero_identity`) — no special
  handling needed beyond displaying the error text, same as
  `admin_review.html`'s existing `setStatus(err.message, true)` pattern.

## 6. Out of scope

- Exposing `confidence_threshold` / `target_collection` as form fields —
  the page uses `segment-upload`'s existing defaults, matching the "even
  easier" framing of the request. An admin who needs non-default values
  still has the CLI scripts (`docs/chapter-segmentation.md`).
- Any new authentication/authorization mechanism — this page sits on the
  exact same trust boundary the four endpoints already have.
- Persisting or pre-filling credentials across page loads (matches
  `/admin/review`'s current behavior — re-entered each visit).
