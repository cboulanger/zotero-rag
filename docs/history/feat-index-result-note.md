# Indexing Report Note

## Context

After a library indexing run, the user only saw transient status messages in the dialog — no persistent record of what happened. This feature adds a Zotero note created automatically at the end of each indexing run that summarises the run statistics and provides a linked table of every item that failed to index, with the reason and (where available) the underlying error detail from the backend.

The note mirrors the existing `createResultNote` pattern used for query results, and is placed at the top level of the indexed library (not inside any collection) so it is easy to find.

---

## Design Decisions

- **Always create when there is work to report.** The note is created when `uploaded > 0 || errors > 0 || parseErrors > 0 || noFile > 0 || skippedEmpty > 0 || skippedTimeout > 0`. Pure "everything already up to date" runs (all items skipped by the version cache) produce no note.
- **Non-fatal creation.** If note creation fails (e.g. Zotero API error), the failure is logged and the indexing flow continues normally.
- **Top-level placement.** `Zotero.Item.saveTx()` is called without `addToCollection()`, which places the note at the root of the library.
- **Backend error detail passed through.** The kreuzberg exception message (previously swallowed in server logs) is now serialised into `DocumentUploadResult.error_detail` and surfaced in the note's "Detail" column.

---

## Files Changed

### Backend

| File | Change |
|---|---|
| `backend/models/document.py` | Added `error_detail: Optional[str] = None` to `AttachmentProcessingResult` dataclass |
| `backend/services/document_processor.py` | Populated `error_detail` with `str(e)` for `KreuzbergTimeoutError` and `KreuzbergParsingError` (two call sites) |
| `backend/api/document_upload.py` | Added `error_detail: Optional[str] = None` to `DocumentUploadResult` Pydantic model; populated it from `proc_result.error_detail` in the success-path return |

### Plugin

| File | Change |
|---|---|
| `plugin/src/remote_indexer.js` | Added `FailedItem` typedef; declared `failedItems[]`; collected `no_file` items before upload loop with per-item detail; pushed entries for `parse_error`, `empty`, `timeout`, and `error` outcomes in upload loop; exposed `errorDetail` from `_uploadAttachment` return; included `failedItems` in `indexLibrary` return value |
| `plugin/src/zotero-rag.js` | Added `createIndexingReportNote(libraryId, libraryType, libraryName, result)` method — builds HTML with summary statistics (`<ul>`) and a `<table>` of failed items, each linked via `zotero://select/`; note saved at library top level |
| `plugin/src/dialog.js` | In `checkAndMonitorIndexing`, added call to `plugin.createIndexingReportNote(...)` after all result-handling, guarded by `hasReport` condition; wrapped in try/catch so failure is non-fatal |

---

## Note HTML Structure

```html
<div>
  <h2>Indexing Report: {libraryName} — {timestamp}</h2>
  <p><strong>Summary</strong></p>
  <ul>
    <li>Indexed: N</li>
    <li>Already up to date (skipped): N</li>
    <li>Missing local files: N</li>        <!-- when > 0 -->
    <li>Indexing errors: N</li>            <!-- when > 0 -->
    <li>Parse / extraction errors: N</li>  <!-- when > 0 -->
    <li>Empty (no text found): N</li>      <!-- when > 0 -->
    <li>Extraction timeouts: N</li>        <!-- when > 0 -->
  </ul>

  <!-- Only when failedItems.length > 0 -->
  <p><strong>Failed items</strong></p>
  <table>
    <tr><th>Item</th><th>Reason</th><th>Detail</th></tr>
    <tr>
      <td><a href="zotero://select/library/items/{key}">Title</a></td>
      <td>Parse error (binary/corrupted data)</td>
      <td>kreuzberg sidecar returned HTTP 422 …</td>
    </tr>
    …
  </table>
</div>
```

Reason labels:

| `reason` value | Display |
|---|---|
| `parse_error` | Parse error (binary/corrupted data) |
| `timeout` | Extraction timeout |
| `empty` | No text extracted |
| `error` | Indexing error |
| `no_file` | No local file |

---

## Data Flow

```
RemoteIndexer.indexLibrary()
  └─ _uploadAttachment() → { rateLimitHeaders, parseError?, skippedEmpty?,
                              skippedTimeout?, errorDetail? }
       ↑ error_detail comes from backend DocumentUploadResult.error_detail
         which is populated from KreuzbergParsingError / KreuzbergTimeoutError

  FailedItem entries collected for:
    • no_file items (before upload loop)
    • parse_error / empty / timeout outcomes (upload loop)
    • generic errors caught in catch block (upload loop)

  return { …existing fields…, failedItems: FailedItem[] }

dialog.js / checkAndMonitorIndexing()
  └─ if hasReport → plugin.createIndexingReportNote(libraryId, libraryType,
                                                     libraryName, indexResult)

ZoteroRAGPlugin.createIndexingReportNote()
  └─ getZoteroLibraryID() → zoteroLibraryID
  └─ build HTML (stats summary + linked failure table)
  └─ new Zotero.Item('note') → set libraryID, setNote(html) → saveTx()
```
