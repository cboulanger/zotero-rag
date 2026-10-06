# Deferred server-side indexing for Fix Unavailable Attachments

## Problem

The "Fix Unavailable Attachments" dialog currently indexes every repaired
attachment synchronously: for each row, the plugin uploads the file bytes
to the backend and waits (via polling `/api/index/tasks/{task_id}`) for
extraction + embedding + vector storage to finish before moving to the next
row. For libraries with many unavailable attachments — or large/slow
documents — this makes a single "Fix" run take a long time, even though the
plugin's own work (finding the file, downloading it if needed, uploading
it) is fast; the wait is almost entirely the backend's embedding pipeline.

Libraries that already have [automatic indexing](../../../docs/cron-indexing.md)
configured (a stored read-only Zotero API key, drained by the hourly
autoindex scheduler/cron job) have another option: let the client upload
the bytes and move on immediately, leaving the actual indexing to the
library's existing scheduled run. This is strictly an addition for those
libraries — it changes nothing for a library that has no automatic indexing
configured, which keeps today's synchronous-only behavior.

## Goals

- Let the Fix dialog's default action finish fast: upload bytes, don't wait
  for embedding.
- Give the user an explicit, easy way to force immediate (synchronous)
  indexing instead, when they want it right away.
- Show queued-but-not-yet-indexed attachments clearly on dialog
  reopen/refresh, with an ETA, instead of looking "fixed" or "still broken."
- Reuse the existing per-library autoindex mechanism (scheduler/cron,
  key store, key pruning) rather than building a second, independent
  background worker.

## Non-goals

- This does not change behavior for libraries without automatic indexing
  configured — the dialog's Fix action remains synchronous-only for them,
  with no new checkbox/button variant shown.
- This does not add a general-purpose job queue or message broker. The
  "cache" is a plain per-library directory of pending files, drained by the
  autoindex job that already runs for that library.
- This does not change how the autoindex job discovers *new/changed* items
  via the Zotero Web API — that path is untouched. This only adds a second
  source the same job drains: files the plugin already uploaded.

## Enablement

The deferred path (and the split-button alternative, and the "Waiting to be
indexed" status) is only available for a library that already has
automatic indexing configured, i.e. has a valid stored read-only API key in
the autoindex key store (`docs/cron-indexing.md`'s "auto-index keys"
section). For any other library, the Fix dialog behaves exactly as it does
today: one "Search & Fix Selected" button, always synchronous.

This reuses an existing, already-validated per-library signal instead of
introducing a new deployment-wide flag, and it's the only case where the
draining mechanism (the next section) actually exists for that library.

## Backend: pending-upload cache

Stored as plain files under the existing `data_path`, following the same
pattern as `autoindex_keys.json` and `migration_state/` — no new database:

```
<data_path>/system/pending_uploads/<library_id>/<attachment_key>.bin         # raw file bytes
<data_path>/system/pending_uploads/<library_id>/<attachment_key>.meta.json   # sidecar metadata
```

The sidecar JSON holds the same metadata the upload endpoints already take
today (`item_key`, `mime_type`, `item_version`, `attachment_version`,
`title`/`authors`/`year`/`item_type`, `zotero_modified`), plus:

- `enqueued_at` — ISO 8601 timestamp, used to order draining within a run.
- `attempts` — integer, incremented on each failed drain attempt.
- `last_error` — string or null, the most recent failure's message.

A re-upload of the same `attachment_key` (e.g. the user runs Fix again, or
the item changed) overwrites both files in place — there is at most one
pending entry per attachment, keyed by `attachment_key`. An entry is
deleted once it's successfully processed, whether via the autoindex run's
drain step or via on-demand "process now" (below).

## Backend: API changes

**`POST /api/index/document/cache`** — new. Same multipart request shape
(`file` + `metadata` JSON) as the existing `/api/index/document/async`, but
only writes to the pending-upload cache above — no extraction, no
embedding, no background task. Returns immediately:

```json
{"status": "queued", "eta": "2026-10-06T15:30:00Z"}
```

`eta` is that library's next scheduled autoindex run (from the built-in
scheduler's known interval, or, for the external-cron deployment mode, the
top of the next hour). If the scheduler is paused or the library's
autoindex key has been pruned, `eta` is `null` and a `reason` field
explains why (`"paused"` or `"key_invalid"`) — see Edge Cases.

**`POST /api/index/document/cache/{library_id}/{attachment_key}/process-now`**
— new. Pulls that one cached entry and runs it through the existing
synchronous pipeline (the same `_execute_upload_impl` used by today's
`/api/index/document`), then deletes the cache entry on completion
(success or terminal failure). Returns the same `DocumentUploadResult`
shape `/api/index/document` returns today. This is what the dialog's
"Fix & Index Selected Now" calls for rows already sitting in the cache.

**`POST /api/libraries/{library_id}/check-indexed`** — extended. For any
requested item/attachment currently present in that library's pending
cache, the response's per-item `reason` becomes `"queued"`, with an
additional `eta` field (same semantics as above). This lets the dialog show
"Waiting to be indexed" immediately on open/refresh, without a separate
round-trip per item.

## Backend: autoindex job integration

Each per-library autoindex run (`bin/index_libraries.py`, whether fired by
the built-in scheduler or external cron) additionally drains that
library's `pending_uploads/<library_id>/` directory — processing each
cached entry through the normal extract+embed+store pipeline — in addition
to its existing Zotero-Web-API-based scan for new/changed items. This is
the only mechanism that ever processes a deferred upload, and it is also
the only way a WebDAV-stored attachment's bytes are ever indexed at all,
since the autoindex job's own Zotero-API scan cannot reach WebDAV storage
(WebDAV credentials never leave the local Zotero client).

A cached entry that fails during drain is left in place with `attempts`
incremented and `last_error` recorded; it's retried on every subsequent
run with no hard attempt cap in v1 (see Edge Cases for the tradeoff this
accepts).

## Plugin: RemoteIndexer

`RemoteIndexer._uploadAttachment()` gains a `defer` option:

- `defer: true` — POST to `/api/index/document/cache`, no polling. Returns
  `{queued: true, eta}` to the caller immediately.
- `defer: false` — unchanged: POST to `/api/index/document/async`, then
  poll `/api/index/tasks/{task_id}` to completion, exactly as today.

Every call site in `fix-unavailable.js` that currently uploads a repaired
attachment (`_uploadDownloadFailedAttachment`, the Phase 1 search+fix
upload, the Phase 2 fallback-found upload) threads this flag through from
the dialog action that triggered it.

## Dialog UX

**Split button replaces a checkbox.** The footer's single Fix button
becomes a split button:

- Default: **"Search & Fix Selected"** — searches/downloads as needed, then
  uploads with `defer: true`. Fast; rows that reach the backend
  successfully move to `status-queued` (below), not `status-fixed`.
- Dropdown alternative: **"Fix & Index Selected Now (slow)"** — same
  search/download step, but uploads with `defer: false` (today's existing
  synchronous behavior). Rows move straight to `status-fixed` or
  `status-error` once indexing actually completes.

The same split button handles already-queued rows too: running the default
action on a row already in `status-queued` is a no-op (nothing to search
or download — the bytes are already cached server-side), while running the
dropdown alternative on it calls `process-now` directly, skipping
search/download entirely.

**New row status: `status-queued`** (amber), alongside the four existing
classes (`fixed`/`error`/`not-found`/`searching`). Text: "Waiting to be
indexed — next run in ~N min", tooltip showing the absolute local time from
`eta`. Populated two ways:

1. Immediately after a `defer: true` upload succeeds during a Fix run.
2. On dialog open/refresh, for any row whose `check-indexed` lookup reports
   `reason: "queued"` — so reopening the dialog later shows accurate
   queued state without the user needing to run Fix again.

## Edge cases

- **Autoindex key revoked/pruned while entries are queued**: the drain step
  can't run for that library until the key is restored. The dialog shows
  "Indexing paused — automatic indexing key is no longer valid" (sourced
  from the existing key-pruning state) instead of computing a stale or
  wrong ETA.
- **Scheduler explicitly paused** (admin `POST /scheduler/pause`): same
  treatment — "Indexing currently paused" instead of a bogus ETA.
- **Repeated drain failures**: no hard retry cap in v1. A cached entry that
  keeps failing (e.g. a genuinely malformed document) retries every
  scheduled run indefinitely, spending a small amount of cycles each time
  rather than silently disappearing. If this proves to waste meaningful
  resources in practice, a future tightening can add a max-attempts cutoff
  that surfaces a terminal error in the dialog instead of retrying forever.
- **Disk usage**: no hard cap on pending-cache size in v1. Given this
  project's prior production incident with unbounded dangling-image disk
  growth, this is worth a one-line callout in `docs/cron-indexing.md` once
  implemented, so it gets checked during routine disk-usage monitoring
  rather than discovered during an outage.
- **Version changes while queued**: re-uploading overwrites the cache entry
  in place (keyed by `attachment_key`), so there's no special staleness
  handling needed — the next drain always processes the latest version.

## Testing plan

- Backend: unit tests for the pending-upload cache (write/overwrite/delete,
  sidecar metadata round-trip), the two new endpoints, and the
  `check-indexed` extension's `queued`/`eta` reporting.
- Backend: an autoindex-job integration test verifying a run drains and
  clears a library's pending cache, and that a failing entry is retained
  with `attempts`/`last_error` updated rather than dropped.
- Plugin: `RemoteIndexer._uploadAttachment()` tests for both `defer` values.
- Plugin: `fix-unavailable.js` tests for the split button's two actions
  across fresh rows and already-`status-queued` rows (including the
  default-action no-op case), and for `status-queued` rendering from a
  `check-indexed` refresh.
