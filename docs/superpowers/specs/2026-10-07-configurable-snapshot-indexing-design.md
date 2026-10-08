# Configurable Snapshot-Attachment Indexing — Design Spec

## 1. Goal

Zotero's "Save Snapshot" action attaches a local copy of a webpage to an
item, titled `"Snapshot"` by default, as an HTML attachment. Today every
`text/html` attachment is treated as indexable (see
`plugin/src/remote_indexer.js`'s `INDEXABLE_TYPES`), so these snapshots get
indexed like any PDF — which is usually not wanted: a page snapshot rarely
carries the kind of content worth retrieving, and it adds noise and cost to
every library that has any.

This feature adds a global, admin-controlled setting, **"Index Snapshots of
webpages"**, default **off**. While off:

- Attachments titled exactly `"Snapshot"` are silently excluded from
  indexing — both the plugin's own upload path and the backend's autonomous
  (cron/auto-index) path.
- The same attachments are silently excluded from the **Fix Unavailable**
  dialog's list, even if they happen to be unreadable/missing/too
  large/etc. — they are simply never surfaced there.

Turning the setting off (whether freshly unchecked, or via a standalone
cleanup action) offers to delete any **already-indexed** Snapshot chunks
from the vector store, server-wide, behind a double confirmation.

This is a server-wide setting — the same "admin" concept already used by the
autoindex scheduler's pause/resume/run-now controls (owner/admin of the
server's `AUTHORIZED_GROUP_ID`) — not a per-library preference.

## 2. Current state

- **Attachment collection for indexing** — `plugin/src/remote_indexer.js`'s
  `_collectAttachments()` (used by the plugin's direct "index this library"
  flow) filters by `INDEXABLE_TYPES` (`application/pdf`, `text/html`,
  `.docx` mime, `application/epub+zip`) and excludes `linkMode === 3`
  (bare linked URL, no file). It has no concept of attachment title.
- **Backend autonomous indexing** — `backend/services/document_processor.py`
  fetches full Zotero item JSON via the Zotero API (`get_library_items_since`,
  `get_item_children`) and filters attachments by
  `att["data"]["contentType"] in INDEXABLE_MIME_TYPES` at four call sites:
  the full-scan streaming pre-filter (~line 686-707, which also decides
  catalog-only-vs-has-attachment for items with no indexable attachment),
  `_index_item`'s real per-item attachment selection (~line 1023-1026), and
  two more in the incremental-sync path (~line 1662-1678, ~1804-1831). Each
  Zotero API attachment object already includes `data.title`, so the title
  is available here — it's just never read.
- **Chunk/document metadata** — `backend/models/document.py`'s
  `DocumentMetadata` stores the *parent item's* title (for citations) but
  has no field for the attachment's own title. `CURRENT_SCHEMA_VERSION` is
  currently `6`.
- **Plugin upload metadata** — `backend/api/document_upload.py`'s
  `_parse_upload_request()` reads `meta_dict["title"]` (parent item title,
  sent by the plugin) into `DocumentMetadata.title`. No attachment-title
  field exists in this request today.
- **Fix Unavailable list** — `plugin/src/zotero-rag.js`'s
  `_getUnavailableAttachments()` does a SQL scan of local Zotero attachments
  missing their file, then appends four more sub-scans, each deduplicating
  by attachment key: `_getParseErrorAttachments`, `_getSkippedServerAttachments`,
  `_getTooLargeAttachments`, `_getDownloadFailedAttachments`. All five
  resolve to a live Zotero attachment item (so `.getField('title')` is
  available at each site) before pushing a row.
- **Admin-gated settings precedent** — `backend/services/autoindex_scheduler.py`
  has `read_scheduler_state(data_path)` / `write_scheduler_state(data_path, state)`,
  reading/writing `data_path/system/autoindex_scheduler_state.json`, with a
  safe empty-dict default when the file doesn't exist. `backend/api/autoindex.py`
  gates mutating admin endpoints with `Depends(require_authorized_group_admin)`
  from `backend/dependencies.py`.
- **Deleting indexed content** — `backend/db/vector_store.py` has
  `get_item_chunks(library_id, item_key)` / `delete_item_chunks(library_id, item_key)`
  (whole-item granularity only, no attachment-level filter) and
  `delete_item_deduplication_records(library_id, item_key)`. Nothing today
  scrolls or deletes across *all* libraries at once, or filters by anything
  other than `library_id`/`item_key`.
- **Admin UI** — `plugin/src/autoindex-status.xhtml` has an `#admin-controls`
  block (`display: none` by default), shown via
  `updateAdminControlsVisibility(data)` in `plugin/src/autoindex-status.js`
  when the polled `/api/autoindex/status` response has `is_admin === true`.
  It already holds a checkbox (`#admin-scope-toggle`, "Show all users' jobs")
  alongside buttons for run-now/pause/resume/abort — the established home
  for server-wide admin controls.
- **Plugin's cached-fetch pattern** — `plugin/src/zotero-rag.js`'s
  `getAutoIndexedLibraryIds()` fetches once, caches the result plus a
  timestamp, reuses it for 5 minutes, and falls back to a safe empty default
  on any fetch error. This is the template for the new settings fetch.

## 3. Scope decisions

| Decision | Choice |
| --- | --- |
| Setting scope | Global/server-wide, not per-library |
| Default | Off (snapshots excluded) |
| Title match | Exact, case-sensitive match on `"Snapshot"`, applied only to `text/html` attachments — a renamed snapshot is treated as a normal attachment, and a non-HTML file (e.g. a PDF) titled "Snapshot" is never excluded, since Zotero never auto-assigns that title outside a webpage snapshot |
| Exclusion visibility | Fully silent — no skip-reason, no persisted record, never shown in Fix Unavailable, unlike the `skipped_too_large`/`skipped_empty` family |
| Where admin reads/writes it | Existing `#admin-controls` block in the autoindex-status dialog |
| Read access | Any authenticated Zotero identity may `GET` the current value (the plugin needs it outside admin contexts too) |
| Write access | Admin only (`require_authorized_group_admin`) |
| Delete-existing-entries trigger | Both: (a) automatically offered when an admin unchecks the box, and (b) a standalone "Delete indexed Snapshot entries…" button, always available, independent of the checkbox |
| Delete scope | Every library on the server (matches the setting's global scope) |
| Delete safety | Two-step confirm: "delete them?" then "this cannot be undone" — purge only runs after both are accepted |

## 4. Data model change

`backend/models/document.py`:

```python
CURRENT_SCHEMA_VERSION: int = 7  # was 6

class DocumentMetadata(BaseModel):
    ...
    attachment_title: Optional[str] = Field(
        None, description="The attachment's own Zotero title (not the parent item's). "
                           "Used to identify Snapshot attachments for the admin-controlled "
                           "indexing toggle and purge. None for chunks indexed before this field existed."
    )
```

No migration needed: existing chunks simply have `attachment_title: None`,
which never matches the purge filter's exact `"Snapshot"` check, so old data
is inert with respect to this feature until re-indexed.

Populated at every chunk-creation site:

- `backend/services/document_processor.py`'s `_index_item` (and the two
  incremental-sync equivalents): `attachment_title=attachment["data"].get("title")`.
- `backend/api/document_upload.py`'s `_parse_upload_request`:
  `attachment_title=meta_dict.get("attachment_title")`.
- Plugin side (`remote_indexer.js`, wherever it builds the upload metadata
  JSON for `_uploadAttachment`): add `attachment_title: att.zoteroItem.getField('title') || null`.

## 5. Backend: settings store and API

New `backend/services/admin_settings_store.py`, mirroring
`autoindex_scheduler.py`'s state helpers:

```python
def read_admin_settings(data_path: Path) -> dict:
    """Returns {"index_snapshots": False} if the file doesn't exist yet."""

def write_admin_settings(data_path: Path, settings: dict) -> None:
    """Atomic write, same _atomic_write_json helper as autoindex_scheduler.py."""
```

State file: `data_path/system/admin_settings.json`, shape
`{"index_snapshots": bool}`.

New `backend/api/admin_settings.py`, registered in `backend/main.py` as
`app.include_router(admin_settings.router, prefix="/api", tags=["admin-settings"])`:

```text
GET  /api/admin/settings                  — any authenticated identity
PUT  /api/admin/settings                  — admin only; body {"index_snapshots": bool}
POST /api/admin/settings/purge-snapshots  — admin only; no body
```

- `GET` returns the current `read_admin_settings(get_settings().data_path)`.
- `PUT` validates the body, calls `write_admin_settings(...)`, returns the
  saved state. Purely a flag flip — no deletion as a side effect, regardless
  of which direction the value changes.
- `POST /purge-snapshots` scrolls `VectorStore`'s chunks collection with no
  `library_id` filter (every library), matching
  `FieldCondition(key="document_metadata.attachment_title", match=MatchValue(value="Snapshot"))`
  (adjust the exact payload key to however `document_metadata` is flattened
  into Qdrant's payload — follow the existing `attachment_key`/`item_key`
  top-level payload convention used elsewhere in `vector_store.py`, i.e. the
  attachment title should be written as a top-level `attachment_title`
  payload field, not nested, exactly parallel to how `attachment_key` is
  already stored). Collects the distinct `(library_id, item_key)` pairs
  touched, deletes the matched chunks via a new
  `VectorStore.delete_chunks_by_filter(filter)` helper (or reuses
  `delete_chunks_by_ids` with IDs gathered from the scroll — implementer's
  choice, whichever fits the existing Qdrant client usage better), then
  calls `delete_item_deduplication_records(library_id, item_key)` for each
  touched pair. Returns `{"deleted_chunks": int, "deleted_attachments": int}`
  (`deleted_attachments` = count of matched points, i.e. distinct attachments
  purged, since one attachment can have multiple chunks).

## 6. Backend: enforcement at indexing time

`backend/services/document_processor.py` gains a shared helper:

```python
def _is_indexable_attachment(att_data: dict, index_snapshots_enabled: bool) -> bool:
    if att_data.get("contentType") not in INDEXABLE_MIME_TYPES:
        return False
    # Scoped to text/html specifically, not any indexable MIME type: Zotero
    # only ever auto-titles HTML webpage-snapshot attachments "Snapshot", so
    # a PDF (or other type) that happens to carry that exact title must not
    # be excluded.
    if not index_snapshots_enabled and att_data.get("contentType") == "text/html" and att_data.get("title") == "Snapshot":
        return False
    return True
```

Every one of the four existing `... in INDEXABLE_MIME_TYPES` filter
expressions (full-scan streaming pre-filter, `_index_item`, and the two
incremental-sync sites) is replaced with a call to this helper, passing the
attachment's `data` dict.

`index_snapshots_enabled` is read once per scan —
`read_admin_settings(get_settings().data_path).get("index_snapshots", False)`
— at the top of `_index_library_full`, `_index_library_incremental`, and
`_index_item` (when called standalone, e.g. from the deferred-upload path),
and threaded down as a parameter rather than re-read per attachment.

The full-scan streaming pre-filter (~line 650-665) currently keeps only
`contentType` in its memory-trimmed `children_by_parent` map ("Keep only
contentType — avoids referencing full item dicts"); it must also keep
`title`, so the pre-filter's "does this item have an indexable attachment at
all" check can correctly treat a Snapshot-only item as catalog-only (no
indexable attachment) when the setting is off, consistent with the real
per-item selection in `_index_item`.

## 7. Plugin: enforcement and cached settings fetch

`plugin/src/zotero-rag.js` gains:

```js
async getIndexSnapshotsEnabled() {
  const TTL_MS = 5 * 60 * 1000;
  const now = Date.now();
  if (this._indexSnapshotsEnabled !== undefined && (now - this._indexSnapshotsEnabledFetchedAt) < TTL_MS) {
    return this._indexSnapshotsEnabled;
  }
  let enabled = false; // safe default: exclude
  try {
    const resp = await fetch(`${this.backendURL}/api/admin/settings`, { headers: this.getAuthHeaders() });
    if (resp.ok) {
      const data = await resp.json();
      enabled = data.index_snapshots === true;
    }
  } catch (e) {
    this.log(`[ZoteroRAG] getIndexSnapshotsEnabled failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  this._indexSnapshotsEnabled = enabled;
  this._indexSnapshotsEnabledFetchedAt = now;
  return enabled;
}
```

Usage sites, all gated on `!(await this.getIndexSnapshotsEnabled())` plus a
title check (`attachmentItem.getField('title') === 'Snapshot'`):

- `remote_indexer.js`'s `_collectAttachments()` — skip before adding to
  `result`, next to the existing `linkMode === 3` exclusion. `_collectAttachments`
  takes the plugin instance (or at least a way to call `getIndexSnapshotsEnabled`)
  as a parameter, or the caller pre-fetches the flag and passes it in —
  implementer's choice, following whatever's least invasive to the existing
  call signature.
- `zotero-rag.js`'s `_getUnavailableAttachments()` — skip in the main SQL-loop
  right after resolving `attachment`/before pushing to `result`.
- `zotero-rag.js`'s `_getParseErrorAttachments`, `_getSkippedServerAttachments`,
  `_getTooLargeAttachments`, `_getDownloadFailedAttachments` — same skip,
  applied wherever each resolves its Zotero attachment item, so a
  Snapshot-titled entry already recorded in a persisted store (from before
  this feature shipped) stops appearing too.

## 8. Admin UI

`plugin/src/autoindex-status.xhtml`, inside `#admin-controls`:

```html
<label id="admin-index-snapshots-label" class="admin-scope-toggle">
  <input id="admin-index-snapshots-toggle" type="checkbox"/> Index Snapshots of webpages
</label>
<button id="admin-purge-snapshots-button" type="button" class="dialog-button">Delete indexed Snapshot entries…</button>
```

`plugin/src/autoindex-status.js`:

- On dialog load/poll, when admin controls are shown, `GET /api/admin/settings`
  and set the checkbox's `checked` state (separate from the 5-minute-TTL
  plugin-wide cache used for indexing decisions — the dialog wants the live
  value, not a stale cached one, so this calls the endpoint directly rather
  than going through `getIndexSnapshotsEnabled()`).
- Checkbox `change` handler:
  - Checked → `true`: `PUT /api/admin/settings {"index_snapshots": true}`, no prompt.
  - Unchecked → `false`: first `PUT` the flag off, then run the shared
    confirm flow below. If the admin declines either confirmation, the flag
    stays off (unchecking always takes effect) but no purge runs.
- `#admin-purge-snapshots-button` click handler: runs the same confirm flow
  directly, independent of the checkbox's current state or value.
- Shared confirm flow — two distinct, sequential confirm dialogs (matching
  whatever confirm helper this dialog already uses elsewhere — check
  `fix-unavailable.js`/`autoindex-status.js` for the existing pattern, e.g.
  `Services.prompt.confirm`, rather than introducing a new one):
  1. First: "Delete all already-indexed Snapshot entries from the index
     now?" — if no, stop here; nothing is deleted.
  2. Second (only if step 1 was yes): "This cannot be undone. Continue?" —
     if no, stop here; nothing is deleted.
  3. Only if both were yes: `POST /api/admin/settings/purge-snapshots`,
     then show the result (e.g. in the dialog's existing status/banner
     area): "Deleted N chunk(s) across M attachment(s)."

## 9. Testing

- **Backend:**
  - `admin_settings_store.py`: default-when-missing, round-trip read/write.
  - `admin_settings.py` endpoints: `GET` works for non-admin identities;
    `PUT`/`purge-snapshots` 403 for non-admin, succeed for admin; `PUT`
    persists and is reflected in a subsequent `GET`; `purge-snapshots`
    deletes only chunks with `attachment_title == "Snapshot"`, leaves
    others (including chunks with `attachment_title: None`) untouched,
    across more than one library in the same test.
  - `_is_indexable_attachment`: true for a normal PDF regardless of the
    flag; false for a Snapshot-titled `text/html` attachment when the flag
    is off; true for the same attachment when the flag is on; true for a
    `text/html` attachment titled something else regardless of the flag.
  - Full-scan streaming pre-filter: an item whose only attachment is a
    Snapshot is treated as catalog-only (or dropped, if it also has no
    qualifying abstract) when the flag is off.
  - `_parse_upload_request` / `_index_item`: `attachment_title` lands in
    the stored `DocumentMetadata`.
- **Plugin:**
  - `getIndexSnapshotsEnabled()`: caches for the TTL window, refetches
    after expiry, defaults to `false` on a fetch error, matching the
    `getAutoIndexedLibraryIds` test patterns already in
    `plugin/test/zotero-rag.test.js`.
  - `_collectAttachments`: a Snapshot-titled `text/html` attachment is
    excluded when the flag is off, included when on.
  - `_getUnavailableAttachments` and each of its four sub-scans: a
    Snapshot-titled attachment is excluded from the returned list when the
    flag is off, regardless of which sub-scan would otherwise have
    surfaced it.
  - Admin dialog: checkbox reflects the fetched value; checking sends
    `PUT {"index_snapshots": true}` with no confirm; unchecking sends the
    `PUT` then runs the two-step confirm, calling purge only on double-yes;
    the standalone purge button runs the same two-step confirm independent
    of checkbox state; declining either confirm makes no purge call.

## 10. Out of scope

- Per-library overrides of this setting.
- Detecting snapshot-type attachments by anything other than exact title
  match on `text/html` attachments (e.g. `linkMode` heuristics, or applying
  the title match to other MIME types) — a user-renamed snapshot is treated
  as a normal attachment, and a non-HTML file (e.g. a PDF) someone happens
  to title "Snapshot" is never excluded; both are accepted trade-offs of the
  title-match approach chosen here.
- Retroactively re-indexing anything — this feature only ever removes or
  skips; turning the setting back on does not automatically re-index
  previously-skipped snapshots (they get picked up next time their library
  is indexed, via the normal incremental/full sync cycle, same as any other
  previously-unindexed attachment).
