# Indexing counts: missing, unavailable, and effective total

This document explains how the plugin tracks the number of indexed items, how it
decides a library is "fully indexed", and what the "unavailable" and "missing file"
counts mean and how they are maintained.

## Concepts

### totalIndexable

Computed by `RemoteIndexer.countIndexableAttachments(libraryId, libraryType)` against
the local Zotero database.  Counts two kinds of items:

1. **Attachments** with an indexable MIME type (`application/pdf`, `text/html`,
   `application/vnd.openxmlformats-officedocument.wordprocessingml.document`,
   `application/epub+zip`), regardless of whether the file is locally present.
2. **Abstract-only items**: regular Zotero items that have no indexable attachment at
   all but carry an `abstractNote` of at least 100 words.

The sum is the number the UI shows as the denominator (e.g. `4805/10055`).
Stored in `ZoteroRAGDialog.libraryIndexableCount` (Map keyed by backend library ID).

### total_items_indexed

Returned by the backend's `GET /api/libraries/{id}/index-status` endpoint.
The number of document chunks the vector store currently holds for this library.
Used as the numerator in the UI (e.g. `4805/10055`).

### unavailableCount

The number of attachments that need indexing but have no local file even after a
sync-download attempt (`noFile` from the indexer).  Stored in:
- `ZoteroRAGDialog.libraryUnavailableCount` (in-memory Map)
- Zotero pref `extensions.zotero-rag.unavailableItems.<backendLibraryId>`

**Purpose**: subtracted from `totalIndexable` to compute `effectiveTotal` so that a
library can be considered fully indexed even when some items genuinely have no local
file:

```
effectiveTotal = totalIndexable - unavailableCount
isComplete     = total_items_indexed >= effectiveTotal
```

The UI shows a warning triangle (⚠) and `(incomplete)` when `isComplete` is false.

`unavailableCount` is set **only** by the indexer's `noFile` result at the end of
each indexing run (`checkAndMonitorIndexing`).  It is reset to 0 at the start of
every re-index (`reindexLibrary`).

### missingFilesCount

Separate from `unavailableCount`.  The raw `noFile` value from the last indexing run,
kept solely to decide whether to show the "X unavailable" clickable link that opens
the Fix Unavailable Attachments dialog.  Stored in:
- `ZoteroRAGDialog.libraryMissingFilesCount` (in-memory Map)
- Zotero pref `extensions.zotero-rag.missingFiles.<backendLibraryId>`

Cleared to 0 (via `plugin.clearMissingFilesCount(backendLibraryId)`) when the Fix
dialog performs a fresh scan and finds no missing items.

---

## Indexing pipeline and how counts are produced

Each call to `RemoteIndexer.indexLibrary()` proceeds in six steps:

### Step 1 — Collect attachments
`_collectAttachments()` queries the local Zotero DB for all items with an indexable
MIME type and calls `getFilePathAsync()` on each.  Items with `filePath = null` (file
not present locally) are included — they are kept so `check-indexed` can decide
whether the backend already has them before attempting a download.

**linkMode=3 (linked_url) items are excluded.**  These are web-only bookmarks with no
local file; they can never be uploaded.  The count of excluded items is returned as
`linkedUrls` and logged for diagnostics, but never counted as "unavailable".

### Step 2 — Client version cache
A local JSON cache (`versionCache`) stores `{ attachmentKey → item_version }` for
every item the client has successfully uploaded or confirmed up-to-date.

- **Incremental / auto mode**: items whose `item_version` in the cache is ≥ the
  current Zotero version are classified as `needs_indexing: false` without asking the
  backend.  Only new or changed items go to `check-indexed`.
- **Full mode** (`mode === 'full'`, triggered by "Re-index"): the cache is bypassed
  entirely.  All items are sent to `check-indexed` so the backend can report what it
  actually has.  This handles the case where the backend has lost data since the last
  run.

### Step 2b — Sync-download cached items with no local file
For items that the cache already considers up-to-date but whose local file is absent,
a Zotero sync download is attempted.  This keeps local files available without
re-indexing content the backend already has.

### Step 3 — check-indexed
The backend's `POST /api/libraries/{id}/check-indexed` endpoint is called with the
items not covered by the cache.  It returns `needs_indexing: true/false` for each.
If the call fails (HTTP 5xx), the whole batch is conservatively marked
`needs_indexing: true`.  Items confirmed up-to-date by the backend are written into
the version cache.

### Step 4 — Download missing files
For items that need indexing but still have no local file, a Zotero sync download is
attempted.  Items where the download also fails keep `filePath = null`.

### Step 5 — Upload

```
uploadable = toUpload where filePath != null
noFile     = toUpload where filePath == null   (counted, not uploaded)
```

`uploadable` items are uploaded to `POST /api/index/document`.  The server returns one
of the following statuses:

| Server status          | Client outcome     | Notes                                               |
|------------------------|--------------------|-----------------------------------------------------|
| `indexed_fresh`        | `uploaded++`       | Stored in Qdrant                                    |
| `copied_cross_library` | `uploaded++`       | Copied from another library's chunks                |
| `skipped_duplicate`    | `uploaded++`       | Already present with same version                   |
| `skipped_empty`        | `skippedEmpty++`   | No text extracted (scanned or protected PDF)        |
| `skipped_timeout`      | `skippedTimeout++` | Kreuzberg timed out (very large files)              |
| `skipped_parse_error`  | `parseErrors++`    | Binary data / corrupt file                          |
| `error`                | `errors++`         | Thrown as exception                                 |

**Version cache**: all outcomes except `error` write `versionCache[key] = version`.
This prevents pointless re-uploads on the next incremental run.  A reindex
(`force_refresh=True`) bypasses the client version cache and retries everything.

**Server cache**: the backend updates its own `check-indexed` cache for all terminal
skipped statuses (`skipped_empty`, `skipped_timeout`, `skipped_parse_error`,
`skipped_duplicate`), in addition to successfully indexed items.  On the next
incremental run the server returns `up_to_date` for these items — no Kreuzberg call is
made.  On a forced reindex the server cache is bypassed and Kreuzberg is retried.

A 0-byte file is rejected client-side before the upload attempt.

### Step 6 — Abstract-only items
Items with no indexable attachment but a substantial abstract are uploaded as
text-only documents.  They follow the same version-cache logic.

### Return values

| Field              | Meaning                                                              |
|--------------------|----------------------------------------------------------------------|
| `uploaded`         | Items actually stored in Qdrant (`indexed_fresh` / `copied_*`)      |
| `skipped`          | Items not needing indexing (cache or backend confirmed)              |
| `noFile`           | Items needing indexing but with no local file after download attempts |
| `linkedUrls`       | linkMode=3 attachments excluded from indexing (web-only bookmarks)  |
| `skippedEmpty`     | Items where server extracted no text (`skipped_empty`)              |
| `skippedEmptyKeys` | Attachment keys for `skippedEmpty` items (persisted for Fix dialog) |
| `skippedTimeout`   | Items where Kreuzberg timed out (`skipped_timeout`)                 |
| `skippedTimeoutKeys` | Attachment keys for `skippedTimeout` items                        |
| `parseErrors`      | Items with binary/corrupt data (`skipped_parse_error`)              |
| `parseErrorKeys`   | Attachment keys for `parseErrors` items                             |
| `errors`           | Items that failed to upload (server error, empty file, etc.)        |
| `firstError`       | Error message of the first failure, shown in the UI                 |

---

## How the UI display is derived

```
totalIndexable  = countIndexableAttachments()          (local DB)
unavailable     = noFile from last indexing run        (persisted in prefs)
effectiveTotal  = totalIndexable - unavailable
indexed         = total_items_indexed                  (from backend)

isComplete = indexed >= effectiveTotal
```

| Display             | Condition |
|---------------------|-----------|
| `✓ N/M items`       | `isComplete` is true |
| `⚠ N/M items (incomplete)` | `isComplete` is false |
| `∅ not indexed`     | no backend metadata exists yet |

The `M` shown in the UI is `effectiveTotal`, not `totalIndexable`, so items that
genuinely have no local file do not make the library appear permanently incomplete.

---

## Prefs

Both counts survive dialog close/reopen via Zotero preferences.  The key uses the
**backend library ID** (e.g. `u39226` for the user library, or a numeric group ID):

| Pref key | Value |
|----------|-------|
| `extensions.zotero-rag.unavailableItems.<backendLibraryId>` | `unavailableCount` (`noFile`) |
| `extensions.zotero-rag.missingFiles.<backendLibraryId>`     | `missingFilesCount` (`noFile`) |

Both hold the same numeric value after an indexing run; they serve different purposes
(effective-total calculation vs. fix-dialog link visibility) and are cleared
independently.

---

## Fix Unavailable Attachments dialog

Opened by clicking the "X unavailable" link next to a library row.  On open it calls
`plugin._getUnavailableAttachments(zoteroInternalLibraryID)`, which:

1. Queries the local Zotero DB for all non-deleted attachments with
   `linkMode IN (0, 1, 2)` (imported files, imported URLs, linked files).
2. Calls `attachment.fileExists()` on each.
3. Returns only items where the file does not exist AND the attachment has a parent
   item.

If the scan returns 0 items, `plugin.clearMissingFilesCount(backendLibraryId)` is
called, which zeroes the pref and notifies the main dialog to remove the link.

Note: the dialog uses the **Zotero internal library ID** (numeric) for DB queries and
the **backend library ID** (string) for prefs and callbacks.  `openFixUnavailableDialog`
resolves both from the backend library ID passed by the main dialog.
