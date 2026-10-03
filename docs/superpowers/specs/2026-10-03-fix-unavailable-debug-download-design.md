# Fix Unavailable: "Download Debugging Information" — Design Spec

## 1. Goal

When a row in the **Fix Unavailable Attachments** dialog cannot be repaired
(or is repaired in a surprising way), the user and maintainers currently have
almost nothing to go on: the row says "Still times out", "Retry failed: ..." or
"Not found", and the real cause (Kreuzberg's response, the computed timeout,
which pipeline stage failed, the server log lines for that request) lives only
in the server log, interleaved with every other request.

This feature adds an opt-in checkbox to the dialog's footer. When it is
checked and the user runs **Search & Fix Selected**, the repair actions also
collect detailed diagnostics — plugin-side for every strategy tried, and
backend-side (log records, errors, extraction/indexing stage details) for every
repair step that touches the backend — and save everything as one JSON file the
user can attach to a bug report.

It is deliberately limited to a small selection (1-10 items) so the output stays
reviewable and bounded.

## 2. Current state

- **Dialog** — `plugin/src/fix-unavailable.xhtml` / `fix-unavailable.js`
  (`ZoteroFixUnavailableDialog`). Footer is `.dialog-buttons`: `#status-bar`
  (flex: 1), then Close / Delete Selected / Search & Fix Selected.
  `updateActionButtons()` already runs on every selection change and is the
  single place that enables/disables footer controls from `this.selected`.
- **Repair flow** — `searchAndFix()` buckets selected rows by type and runs:
  Phase 0 (`skipReason === 'timeout'` → `retryTimeoutSkippedAttachment`, 2x
  timeout), Phase 0b (`'no text'` → `retryEmptyTextSkippedAttachment`), Phase 1
  (batched Zotero sync download via `_tryDownloadAttachment`), Phase 2
  (`_searchAndFixUnavailableAttachment`: filename / MD5 / `owl:sameAs` / URL /
  DOI-OA resolver). Rows that are parse errors or linked files get a fixed
  status and no action.
- **Backend contact** — only Phase 0 / 0b talk to the backend, via
  `RemoteIndexer._uploadAttachment()` -> `POST /api/index/document/async` ->
  `_execute_upload()` -> `DocumentProcessor._process_attachment_bytes()`
  (`backend/api/document_upload.py`, `backend/services/document_processor.py`).
  The result (`DocumentUploadResult`) carries only `status`, `message` and a
  short `error_detail`; the plugin collapses it to
  `{fixed, stillTimedOut | stillEmpty, error}`.
- **Server-reported rows** — rows loaded from the per-library
  download-failed / skipped-server stores carry a stored `reason`/detail from
  the indexing run that recorded them.
- **Existing precedent** — the query dialog's "Export debug info" button
  (`dialog.js` `exportDebugInfo()`) saves a `trace` object as pretty-printed
  JSON via `nsIFilePicker` + `IOUtils.writeUTF8`. The backend's query path has
  `include_trace` / `TraceCollector` for the same purpose. The upload path has
  neither.
- **Logging** — backend logs go through the stdlib `logging` root config in
  `backend/main.py` (console + optional file). There is no per-request capture.

## 3. Scope decisions

| Decision | Choice |
| --- | --- |
| Where the control lives | Checkbox in the footer (`.dialog-buttons`), **left of** `#status-bar`, label "Download debugging information" |
| Visibility | Only visible while **1-10** rows are selected (`1 <= selected.size <= 10`). Hidden (`display: none`) otherwise |
| Hidden vs. checked | The checked state is remembered while the box is hidden, but is **only honored if the box is visible at the moment Search & Fix is clicked**. Hiding never silently collects data |
| Default | Unchecked on every dialog open (not persisted as a pref) |
| Trigger | Collection happens during **Search & Fix Selected** only. **Delete Selected** is unaffected |
| Output | One `.json` file, saved via native save dialog after the run finishes. Cancelling the dialog is not an error — nothing is written |
| Backend diagnostics | Opt-in per request (`include_diagnostics` form field); off by default so normal indexing pays no cost and sends no extra data |
| Scope of backend data | Only for repair steps that call the backend (timeout retry, empty-text retry). Other steps contribute plugin-side data only (see §6) |
| No new endpoint | Reuses `POST /api/index/document/async` and the task-poll endpoint; diagnostics ride on `DocumentUploadResult` |
| Auto-follow-up indexing | Out of scope. A row fixed by Phase 1/2 (file now present locally) is *not* uploaded just to gather diagnostics — repair semantics must not change when the box is checked |

Debug collection must **never change repair behavior or outcome**: same
strategies, same order, same row statuses. It only observes.

## 4. UI

### 4.1 Markup (`fix-unavailable.xhtml`)

Add to `.dialog-buttons`, as the first child (before `#status-bar`):

```html
<label id="debug-download-label" style="display:none"
       title="Collect detailed diagnostics for the selected items during repair and save them as a JSON file">
  <input type="checkbox" id="debug-download-cb"/> Download debugging information
</label>
```

Styled consistently with `#table-toolbar label` (12px, `#555`, flex, 4px gap,
`user-select: none`, `flex-shrink: 0`). `#status-bar` keeps `flex: 1` so
status text is pushed right, as today. At narrow widths the label may wrap;
it must not push the buttons out of the footer.

### 4.2 Visibility logic (`fix-unavailable.js`)

New helper `_updateDebugCheckboxVisibility()` (JSDoc-typed), called from
`updateActionButtons()` **and** from `_setAllButtonsDisabled()` (so the
checkbox is disabled, not hidden, while a run is in progress — the user can
see what mode the run is in but cannot flip it mid-run):

```js
const n = this.selected.size;
label.style.display = (n >= 1 && n <= DEBUG_MAX_ITEMS) ? '' : 'none';
cb.disabled = this.isRunning;
```

`DEBUG_MAX_ITEMS = 10` is a named constant at the top of the file, not a pref.

Note `updateActionButtons()` currently returns early when `this.isRunning`;
the visibility update must happen before that early return or be driven
separately so selection changes after a run completes are reflected.

### 4.3 Reading the flag

At the top of `searchAndFix()`, after `indices` is computed:

```js
const collectDebug = cb.checked && indices.length >= 1 && indices.length <= DEBUG_MAX_ITEMS;
```

(Recomputing from `indices` rather than trusting the DOM visibility alone
guards against a stale display state.) If `collectDebug` is true, a
`DebugReport` object (§5) is created and threaded through the phases.

## 5. Output file

`zotero-rag-fix-debug-<backendLibraryId>-<YYYYMMDD-HHMMSS>.json`
(`:` and `/` in the library id replaced by `-`). Saved with the same
`nsIFilePicker` / `IOUtils.writeUTF8(path, JSON.stringify(report, null, 2))`
pattern as `dialog.js`'s `exportDebugInfo()`; extract the shared picker+write
logic into a small helper rather than copy it a second time (see §9).

### 5.1 Schema

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-10-03T12:34:56Z",
  "tool": "fix-unavailable",
  "plugin": { "version": "...", "zotero_version": "...", "platform": "..." },
  "backend": {
    "url_host": "rag.example.com",        // host only, never full URL w/ credentials
    "is_local": false,
    "server": { /* from first diagnostics payload received, see 6.2 */ }
  },
  "library": { "backend_library_id": "groups/6297749", "zotero_library_id": 31, "library_type": "group" },
  "selection": { "count": 3, "dialog_total_rows": 42 },
  "items": [
    {
      "item_key": "ABCD1234",             // parent (or standalone attachment) key
      "attachment_key": "EFGH5678",
      "title": "…",
      "mime_type": "application/pdf",
      "file": { "exists_locally": true, "size_bytes": 12345678, "basename": "x.pdf", "is_linked": false },
      "initial_state": {                   // what put the row in the dialog
        "type_label": "timeout",
        "skip_reason": "timeout",
        "is_parse_error": false,
        "server_download_failed": false,
        "server_recorded_detail": "…"      // stored reason/detail from the store, if any
      },
      "steps": [                           // chronological, one entry per attempted step
        {
          "phase": "timeout_retry",        // see 5.2
          "started_at": "…", "duration_ms": 41230,
          "outcome": "still_timed_out",    // see 5.2
          "plugin": { /* step-specific, see 6.1 */ },
          "backend": { /* DiagnosticsPayload (6.2) or null */ },
          "backend_note": null             // e.g. "server did not return diagnostics (older version?)"
        }
      ],
      "final_row_status": { "css_class": "not-found", "text": "Still times out — delete or raise the limit further" },
      "errors": [ { "where": "plugin|backend", "message": "…", "stack": "…" } ]
    }
  ],
  "summary": { "fixed": 1, "not_found": 1, "errors": 1 }
}
```

### 5.2 Step phases and outcomes

`phase` is one of: `timeout_retry`, `empty_text_retry`, `sync_download`,
`other_library_search`, `skipped_linked_file`, `skipped_parse_error`.

`outcome` is one of: `fixed`, `still_timed_out`, `still_empty`, `not_found`,
`copy_failed`, `error`, `skipped`.

Rows that get a fixed status without any action (linked file, parse error) get
a single `skipped` step so every selected row has at least one step and the
reader never has to infer "nothing was tried".

### 5.3 Redaction and limits

The file is meant to be attached to public issues, so:

- **Never include**: API keys, `Authorization`/`X-*-Key` headers, the
  `AUTOINDEX_SECRET`, env vars, full backend URL (host only), file contents,
  absolute local file paths (basename + size only; any path in an error
  message is passed through `_redactPaths()` which replaces the user's home /
  Zotero data directory prefix with `~` / `<zotero-data>`).
- **May include**: item titles/keys/authors/year (the user's own library
  metadata, and the test library is public), MIME types, sizes, timings,
  server log lines scoped to that request (see 6.2), exception types/messages
  and tracebacks.
- **Size caps**: per backend payload, at most `DIAG_MAX_LOG_RECORDS = 500`
  log records and `DIAG_MAX_BYTES = 256 KiB` of serialized diagnostics;
  overflow is dropped oldest-first and recorded as
  `"truncated": {"log_records_dropped": N}`. HTTP response-body excerpts are
  capped at 2 KiB each. With <= 10 items the whole file is bounded to a few
  MiB worst case.

The report helper centralizes redaction: steps are added only through
`report.addStep(...)`, which passes strings through `_redactPaths()`, so a call
site cannot forget it.

## 6. Diagnostics content

### 6.1 Plugin-side, per step

| Phase | Recorded in `step.plugin` |
| --- | --- |
| `timeout_retry` / `empty_text_retry` | `timeout_multiplier` sent, file size, upload attempts (count + per-attempt error), HTTP status of the async POST, task id, number of polls and total poll time, raw `status` / `message` / `error_detail` of the final `DocumentUploadResult`, `rate_limit_retries` |
| `sync_download` | `sync_enabled` (Zotero sync on/off), reason returned by `_tryDownloadAttachment` (`sync-disabled`, `not-found`, `rejected`, ...), whether the file existed afterwards, any exception |
| `other_library_search` | For each strategy (filename, MD5/`storageHash`, `owl:sameAs`, direct URL, DOI/OA resolver): attempted yes/no, the lookup key used (filename / hash / URL **host** / DOI), number of candidates considered, per-candidate outcome (`not_found`, `no_local_file`, `copy_failed:<msg>`), and the one that succeeded (`via`) |

Capturing the per-strategy trail requires `_searchAndFixUnavailableAttachment`
to accept an optional `trace` collector argument (`null` by default — when
absent the function behaves exactly as today). It must not alter control flow;
collector calls are fire-and-forget and wrapped so an exception inside the
collector cannot break a repair.

### 6.2 Backend-side payload (`DiagnosticsPayload`)

Returned in `DocumentUploadResult.diagnostics` (new optional field, `null`
unless requested). Pydantic model in `backend/models/diagnostics.py`:

```python
class DiagnosticLogRecord(BaseModel):
    ts: str            # ISO-8601 UTC
    level: str         # DEBUG|INFO|WARNING|ERROR
    logger: str        # e.g. backend.services.extraction.kreuzberg
    message: str
    exc_text: str | None = None   # formatted traceback if the record had exc_info

class DiagnosticStage(BaseModel):
    name: str          # dedup_check | stale_chunk_purge | extraction | embedding | store | library_metadata
    started_at: str
    duration_ms: int
    outcome: str       # ok | skipped | error
    details: dict[str, Any] = {}

class DiagnosticsPayload(BaseModel):
    request_id: str                 # also logged on the server for cross-reference
    server: dict[str, Any]          # allowlisted, non-secret (below)
    stages: list[DiagnosticStage]
    log_records: list[DiagnosticLogRecord]
    final_status: str               # raw proc_result.status / "error"
    error: dict[str, str] | None    # {type, message, traceback}
    truncated: dict[str, int] = {}
```

**`server`** is built from an explicit allowlist, never by dumping settings:
backend version, `extraction_backend`, `kreuzberg_timeout_seconds` (the cap),
Kreuzberg URL **host only**, OCR enabled flag, embedding model name and
dimensions, vector-store mode (embedded/server), Python version.

**`stages[].details`** — the useful part for extraction failures:

- `dedup_check`: content hash prefix (8 chars), whether a record matched,
  whether it was same-item, whether it was orphaned and purged.
- `stale_chunk_purge`: stale version, new version, chunks deleted.
- `extraction`: mime type, byte size, **computed base timeout, multiplier,
  effective timeout, cap**, Kreuzberg request URL path, HTTP status, response
  body excerpt (<= 2 KiB), for split PDFs the per-part result list (part
  index, page range, `ok | timeout | parse_error | empty`, chunk count,
  elapsed), number of chunks returned, and the exception class/message when
  `KreuzbergTimeoutError` / `KreuzbergParsingError` is raised.
- `embedding`: chunk count, batch count, `rate_limit_retries`, upstream error.
- `store`: chunks written, collection, any Qdrant error (type + message).

**`log_records`** — every log record emitted by `backend.*` loggers while this
request was being processed, at **DEBUG** level regardless of the server's
configured level, scoped to this request only (§7).

## 7. Backend design

### 7.1 Request flag

Both upload endpoints (`/index/document`, `/index/document/async`) accept
`include_diagnostics: bool = Form(False)`, parsed alongside `timeout_multiplier`
in `_parse_upload_request()` and threaded through `_execute_upload()` into
`DocumentProcessor._process_attachment_bytes()` the same way
`timeout_multiplier` is today. `_run_task()` stores the result (now including
diagnostics) in `_upload_tasks`, so the poll endpoint returns it unchanged;
`DocumentUploadResult.diagnostics` is `None` when the flag is false, so the
default response is byte-identical to today.

### 7.2 `DiagnosticsCollector` (`backend/services/diagnostics_collector.py`)

Modeled on `TraceCollector`: one instance per request, created in
`_execute_upload()` iff `include_diagnostics`, with
`stage(name)` (async/sync context manager recording timing + outcome +
details), `set_error(exc)`, and `finalize() -> DiagnosticsPayload`.

**Per-request log capture.** A `contextvars.ContextVar[str | None]` named
`current_diagnostics_request_id` is set by `_execute_upload()` for the duration
of the request. A single `logging.Handler` subclass
(`RequestScopedLogHandler`), attached once to the `backend` logger at startup
with level `DEBUG`, appends each record to the active collector **only if** the
record was emitted in a context where the ContextVar equals that collector's
id. `contextvars` propagate into `asyncio.to_thread` and tasks created within
the request, so log lines from threadpool-offloaded Qdrant/embedding calls are
attributed correctly, and concurrent requests never see each other's lines.

Caveats to handle explicitly:

- The handler is a no-op (one ContextVar read) when no request is active, so
  normal operation is unaffected.
- The `backend` logger's *effective* level may be above DEBUG, in which case
  DEBUG records are never created. The collector therefore temporarily
  lowers the level of the specific loggers involved
  (`backend.services.extraction.*`, `backend.services.document_processor`,
  `backend.api.document_upload`) only via the handler's own `filter` + a
  scoped `logger.setLevel` guarded by a refcount (restored when the last
  active diagnostics request finishes). Existing console/file handlers keep
  their own levels, so server log volume does not change.
- Records are dropped past `DIAG_MAX_LOG_RECORDS` oldest-first (ring buffer).
- Secrets: a `logging.Filter` on the handler scrubs known secret-bearing
  patterns (`Authorization:`, `api[_-]?key`, `X-.*-Key`) from `message` and
  `exc_text` before storage, as defense in depth; the allowlist in §6.2 is the
  primary control.

### 7.3 Instrumentation points

Minimal, additive, no control-flow change:

- `document_upload._execute_upload()` — wraps the existing steps in
  `collector.stage(...)` (dedup check, stale purge, process, library
  metadata). The existing `try/except` that builds the `status="error"`
  result also calls `collector.set_error(e)` and attaches the payload to the
  error result — **important**: today an `error` result is turned into a
  thrown `Error` by the plugin (`_uploadAttachment`), so the plugin must still
  be able to retrieve the diagnostics (see §8.3).
- `document_processor._process_attachment_bytes()` and the split-PDF helper
  (`~line 1360-1440`) — record the `extraction`, `embedding` and `store`
  stages and the per-part list, using the collector if one is active (looked up
  through the ContextVar, so the processor signature gains no parameter).
- `extraction/kreuzberg.py` — record computed timeout / multiplier / cap and
  the HTTP status + body excerpt at the points where it already raises
  `KreuzbergTimeoutError` / `KreuzbergParsingError` or logs a failure.

When no collector is active every hook is a single `None` check.

### 7.4 Authorization and data exposure

No new endpoint and no new authorization surface: diagnostics are returned only
to the caller that made the upload request, over the same authenticated route,
and contain only (a) that request's own log lines and (b) the allowlisted
non-secret server info. Nothing outside the caller's own request context can
be read through this flag.

## 8. Plugin design

### 8.1 `DebugReport` helper (`plugin/src/fix-unavailable-debug.js`, new)

Small, DOM-free module (loaded by `fix-unavailable.xhtml` like the other
scripts, and `require`-able from `node --test`):

- `createReport({plugin, library, selection})` -> report
- `report.startItem(info)` -> item handle; `item.addStep({phase, ...})`
  returns a step handle with `.finish(outcome, pluginData, backendPayload)`
- `report.addError(item, where, err)`
- `report.finalize(summary)` -> plain object matching §5.1
- `redactPaths(str)`, and the caps from §5.3

Pure data in / data out so redaction, truncation and schema shape are
unit-testable without Zotero.

### 8.2 Threading through `searchAndFix()`

`searchAndFix()` creates the report iff `collectDebug`, and each phase wraps
its per-item work:

```js
const step = report?.startItem(info).addStep({ phase: 'timeout_retry' });
const result = await this.plugin.retryTimeoutSkippedAttachment(
  info.attachmentItem, info.parentItem, this.libraryID, { includeDiagnostics: collectDebug });
step?.finish(outcomeFor(result), result.pluginDiag, result.backendDiag);
```

All `report?.` calls are optional-chained so the non-debug path is identical to
today. The final `setRowStatus(...)` text/class is recorded as
`final_row_status`. `try/catch` blocks that already exist keep their behavior
and additionally call `report?.addError(...)`.

### 8.3 Getting diagnostics out of the upload helpers

- `RemoteIndexer._uploadAttachment()` gains an `includeDiagnostics` option:
  when true it appends `include_diagnostics=true` to the form data and, for
  **every** terminal result — including `skipped_*` **and the `status:
  "error"` case that currently throws** — returns/attaches
  `diagnostics: result.diagnostics ?? null` plus the plugin-side data from
  §6.1 (attempt count, HTTP status, task id, poll count/time). For the error
  case it throws an `Error` whose `.diagnostics` property carries the payload
  (so existing `catch` handling is unchanged, and the retry helpers read
  `e.diagnostics`).
- `retryTimeoutSkippedAttachment` / `retryEmptyTextSkippedAttachment` accept
  `{ includeDiagnostics }`, forward it, and add `pluginDiag` / `backendDiag`
  to their return objects (existing fields untouched).
- If the server returns no `diagnostics` while one was requested (older
  backend), the step records `backend: null, backend_note: "server did not
  return diagnostics (backend may predate this feature)"` — never an error.

### 8.4 Saving

After the final status line is set (end of `searchAndFix()`, inside a
`try/finally` so `isRunning` and button state are always restored):

1. `report.finalize(summary)`.
2. Open the save dialog (default name per §5). The status bar shows
   "Choose where to save the debug file…" before the picker opens.
3. On success, status bar: `Done. 2 fixed, 1 not found. Debug info saved to <basename>.`
   On picker cancel: `Done. … Debug info not saved.` On write failure:
   `Done. … Failed to save debug info: <msg>` (row statuses untouched; the
   report object is kept on `this.lastDebugReport` so a second attempt could
   be offered later — out of scope for v1, noted as a follow-up).

The picker/write code is factored from `dialog.js` `exportDebugInfo()` into a
shared helper (e.g. `plugin/src/utils/save-json.js`, or an existing shared
module if one fits) and both callers use it.

## 9. Files touched

**Backend**

- `backend/api/document_upload.py` — `include_diagnostics` form field on both
  upload endpoints, `DocumentUploadResult.diagnostics`, stage wrapping, error
  path attaches payload.
- `backend/models/diagnostics.py` (new) — Pydantic models from §6.2.
- `backend/services/diagnostics_collector.py` (new) — collector, ContextVar,
  `RequestScopedLogHandler`, redaction filter, level refcounting.
- `backend/services/document_processor.py` — extraction / embedding / store
  stage recording, split-PDF per-part details.
- `backend/services/extraction/kreuzberg.py` — timeout/multiplier/cap and HTTP
  status + body excerpt into the active collector.
- `backend/main.py` — attach the `RequestScopedLogHandler` at startup.

**Plugin**

- `plugin/src/fix-unavailable.xhtml` — checkbox + footer styles, load the new
  script.
- `plugin/src/fix-unavailable.js` — visibility logic, `collectDebug`, report
  threading, save step.
- `plugin/src/fix-unavailable-debug.js` (new) — `DebugReport` helper.
- `plugin/src/remote_indexer.js` — `includeDiagnostics` option,
  diagnostics on skip/error results.
- `plugin/src/zotero-rag.js` — `retry*SkippedAttachment` options/return
  fields; optional `trace` argument on `_searchAndFixUnavailableAttachment` and
  `_tryDownloadAttachment`.
- `plugin/src/dialog.js` — use the shared save-JSON helper.
- `plugin/src/locale/*` — label/tooltip/status strings if the dialog's other
  strings are localized (the current XHTML hard-codes English; follow
  whatever the file does today, don't introduce a new i18n mechanism).

**Docs**

- `docs/fix-unavailable-attachments.md` — new "Downloading debugging
  information" section (what the box does, the 1-10 item limit, what is and
  isn't in the file, redaction). Describe current behavior only, per repo
  doc rules.
- `docs/superpowers/plans/2026-10-03-fix-unavailable-debug-download.md` —
  implementation plan (written after this spec is approved).
- `CHANGELOG.md` — entry.

## 10. Testing

**Backend (`backend/tests/`, `unittest`)**

- `test_diagnostics_collector.py`: ContextVar scoping — two concurrent
  collectors each receive only their own log records, including records emitted
  from inside `asyncio.to_thread`; ring-buffer truncation sets
  `truncated.log_records_dropped`; secret-scrubbing filter; level refcount
  restores the original logger level after the last request; handler is inert
  with no active request.
- `test_document_upload.py` (extend): default request (`include_diagnostics`
  false) returns `diagnostics is None` and the response JSON is unchanged;
  flag true returns a payload with the expected stages for success,
  `skipped_timeout`, `skipped_empty`, and exception (`status == "error"`
  result still carries `diagnostics.error`); `server` block contains only
  allowlisted keys and no secrets.
- `test_kreuzberg_extractor.py` (extend): timeout/multiplier/cap and HTTP
  status/body excerpt land in the `extraction` stage when a collector is
  active; nothing is recorded (and nothing breaks) when none is.
- Existing indexing tests must pass unchanged (instrumentation is no-op
  without a collector).

**Plugin (`node --test`, `plugin/test/`)**

- `fix-unavailable-debug.test.js`: schema shape from §5.1; `redactPaths`
  (home dir, Zotero data dir, Windows paths); size caps; every selected row
  gets >= 1 step; secrets never appear in `JSON.stringify(report)` when
  headers/keys are passed in step data.
- `fix-unavailable.test.js` (extend): checkbox visible for selection sizes
  0 / 1 / 10 / 11 -> hidden / shown / shown / hidden; stays disabled while
  running; unchecked-by-default; flag is ignored when selection is out of
  range at click time; **with the box checked, row statuses and the sequence of
  plugin calls are identical to the unchecked run** (behavior-neutrality);
  save is offered once at the end, picker cancel does not throw or change
  statuses.
- `remote_indexer` tests: `include_diagnostics` form field sent only when
  requested; diagnostics propagated on `skipped_*` and attached to the thrown
  error in the `status: "error"` case; missing `diagnostics` from an old
  server yields `backend_note`, not a failure.

**Live check** (per CLAUDE.md "Live Query Debugging": use the
`test-rag-plugin` group library only): select one known-timeout item, check the
box, run Search & Fix, confirm the saved JSON contains a populated
`extraction` stage and request-scoped log lines; repeat with 11 selected to
confirm the checkbox disappears.

## 11. Non-goals

- Changing any repair strategy, ordering, or timeout behavior.
- Persisting the checkbox state as a preference.
- Collecting diagnostics for **Delete Selected**, or for more than 10 items.
- Re-uploading a Phase 1/2-fixed item to the backend to gather extraction
  diagnostics (possible follow-up: an explicit "index and diagnose" step).
- A server-side diagnostics store / retrieval-by-request-id endpoint; the
  `request_id` is included only so a maintainer can grep the server log.
- Streaming or uploading the file anywhere. It is written locally; the user
  decides whether to share it.
- Re-offering the save dialog after a cancelled/failed save (follow-up).

## 12. Open questions

1. **Level override mechanics** (§7.2): refcounted `setLevel` on three named
   loggers vs. installing a dedicated `DEBUG`-level logger hierarchy for
   request-scoped capture. Preference here is the former (smaller change);
   confirm during planning that no existing handler downstream would be
   flooded by the lowered level (console/file handlers have their own levels,
   so expected safe).
2. **Kreuzberg sidecar logs**: the sidecar is a separate container; this spec
   captures only what the backend sees (HTTP status + body). Pulling sidecar
   container logs would need container access the backend doesn't have — left
   out unless the response-body excerpt proves insufficient in practice.
3. **Server-reported rows with no backend step** (e.g. a pure download-failed
   row fixed by Phase 1): the file contains the stored `server_recorded_detail`
   but no live backend data. Acceptable for v1, or should such rows trigger an
   immediate diagnostic re-index (see Non-goals)?
