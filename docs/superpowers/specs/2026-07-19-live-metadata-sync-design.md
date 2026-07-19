# Live Client-Side Metadata Sync — Design Spec

## 1. Goal

Today, when a user edits an item's bibliographic metadata (title, authors, year, tags, item type) in Zotero, the change only reaches the backend's Qdrant index via the hourly auto-index cron job (`bin/index_libraries.py`). This feature pushes metadata changes to the backend immediately instead: the plugin watches for Zotero item `modify` events, queues the changed items, and a background job sequentially dispatches batched update requests to the backend, with debouncing, retries, and exponential backoff.

Metadata updates are cheap (a Qdrant `set_payload()` call, no re-embedding), so there's no reason to wait for the hourly cron. Content/attachment indexing (new or changed PDFs, requiring re-extraction and re-embedding) is **explicitly out of scope** — this feature only ever patches payload fields on already-indexed chunks. The design introduces a generic, type-discriminated task queue so that attachment-sync can be added later as a second task type without reworking the queue engine, but no attachment-sync logic is built now.

The hourly cron remains the system of record and safety net: it will still catch any item this feature misses (unindexed items, items edited while offline beyond what retry covers, items whose live push failed and was dropped — though per §5 nothing is ever deliberately dropped).

## 2. Current state (what already exists)

- **Backend endpoint** `POST /api/index/items/metadata` (`backend/api/document_upload.py:893-950`, `batch_update_metadata`) already does a metadata-only Qdrant patch for a batch of items in one library. Today it's only called by the plugin's manual "Index Library" migration path (`check-indexed` → `needs_metadata_update` → `_sendMetadataUpdates`, `plugin/src/remote_indexer.js:1122-1169`), never live. It only accepts `title`/`authors`/`year`/`item_type` — no `tags`, `item_version`, or `zotero_modified` — and hand-assembles a payload `fields` dict rather than reusing the more complete internal method described next.
- **Internal method** `VectorStore.update_item_bibliographic_metadata()` (`backend/db/vector_store.py:635-682`) already does the complete patch (title, authors, tags, year, item_type, item_version, zotero_modified) and is used by the server's own metadata-only-update fast path, `DocumentProcessor._try_metadata_only_update()` (`backend/services/document_processor.py:1552-1650`), during full cron/check-indexed runs. If the item has no existing chunks in Qdrant, both this and the underlying `update_item_metadata()` (`vector_store.py:595-633`) silently return `0` — no error.
- **Plugin-side**, there is currently no `modify` event handling at all. The only existing `Zotero.Notifier` registration (`plugin/src/zotero-rag.js:198-212`) handles item `delete` only, firing a fire-and-forget `DELETE` request with no retry. It also has a latent bug: it sends the raw internal Zotero `libraryID` in the URL instead of the backend's expected `library_id` format, unlike every other endpoint call in the plugin.
- There is no generic queue/retry-with-backoff utility in the plugin; existing retry loops (`remote_indexer.js`'s `_checkIndexed`, `_uploadAttachment`) are hand-rolled per call site with a fixed delay, not exponential backoff, and run to completion synchronously inside one manual "Index Library" invocation — there's no persistent, always-on background job today.
- The plugin already has a fully authenticated request pattern (`ZoteroRAG.getAuthHeaders()` + `backendURL` pref, `zotero-rag.js:184,187,221-233`) reusable as-is; the same personal Zotero API key used for read access is sent on all mutating calls, and the backend does not require a separately write-scoped key for this endpoint.

## 3. Scope decisions

These were confirmed during design and shape the rest of this spec:

1. **Tags are in scope.** The metadata endpoint and client payload are extended to include `tags`, closing an existing gap relative to the cron path.
2. **In-memory queue only.** If the plugin restarts mid-queue, unsent updates are lost; the hourly cron is the safety net. No on-disk persistence.
3. **Debounce + batch.** Rapid successive edits to the same item are coalesced (only the latest snapshot is sent); ready items are sent in batches, not one request per edit.
4. **Unindexed items are a silent no-op.** If an item hasn't been through a full index yet, the live push's HTTP call still "succeeds" (backend returns `updated_items: 0` for it); no special client-side detection or UI surfacing. Cron will index it (with current metadata) whenever it runs.
5. **Retry indefinitely with exponential backoff**, capped at a maximum interval, rather than giving up after N attempts. A stuck batch is made visible via escalating log severity (§7), not a UI element.
6. **Only top-level regular items are watched.** Standalone attachments, notes, and annotations are excluded — they either lack these bibliographic fields or (for attachments) can't be distinguished from a real content change without out-of-scope logic.
7. **The queue engine is generic now**, keyed by a `type` field per task, with per-type dispatcher functions — so a future attachment-sync task type can reuse the debounce/batch/retry/backoff engine without changes to it. No attachment-sync dispatcher is implemented in this feature.
8. **The pre-existing delete-notifier library-ID bug is fixed** as part of this work, since a correct helper is being extracted anyway for the new modify-notifier.

## 4. Architecture

A new plugin-lifetime module, `plugin/src/task_queue.js`, is loaded eagerly from `bootstrap.js` (the same way `zotero-rag.js` is — available for the plugin's entire lifetime — not the on-demand pattern used by `remote_indexer.js`, which only exists while a dialog window is open). It defines a generic `TaskQueue` engine with no knowledge of metadata specifically: tasks carry a `type` string and a payload; **dispatchers** are plain async functions registered per type that turn a batch of same-type tasks into one backend call.

`ZoteroRAG`'s existing item notifier (`zotero-rag.js:198-212`) is extended to also handle `event === 'modify'`: for each modified top-level regular item, it builds a metadata snapshot and calls `TaskQueue.enqueue('metadata', key, snapshot, debounceMs)`. A metadata dispatcher, registered once at `init()`, groups ready tasks by backend `library_id` and POSTs to the (extended) `/api/index/items/metadata` endpoint.

```
Zotero item modify event
        │
        ▼
ZoteroRAG notifier (modify branch)
        │  build snapshot: title, authors, tags, year, item_type,
        │  item_version, zotero_modified
        ▼
TaskQueue.enqueue('metadata', 'libId:itemKey', snapshot, 4000ms)
        │  (debounce: repeated edits to the same key just
        │   overwrite payload + reset the due time)
        ▼
TaskQueue heartbeat tick (1s interval)
        │  moves tasks whose debounce window elapsed into
        │  the 'metadata' ready map
        ▼
TaskQueue._dispatchNext()  (only if no request already in flight)
        │  groups ready 'metadata' tasks by library_id,
        │  caps each batch at MAX_BATCH_SIZE
        ▼
metadata dispatcher: POST /api/index/items/metadata
        │
        ▼
backend batch_update_metadata()
        │  calls update_item_bibliographic_metadata() per item
        ▼
VectorStore.update_item_metadata(): Qdrant set_payload()
```

## 5. Component detail

### 5.1 `plugin/src/task_queue.js` (new file)

```js
var TaskQueue = {
  _pending: new Map(),      // "type:key" -> { type, key, payload, dueAt }
  _ready: new Map(),        // type -> Map(key -> task)     [survives failed dispatch attempts]
  _dispatchers: new Map(),  // type -> async (tasks: Task[]) -> DispatchResult
  _failureCount: new Map(), // type -> consecutive failure count (for backoff + log escalation)
  _nextAttemptAt: new Map(),// type -> timestamp; skip dispatch for this type until then
  _timer: null,
  _inFlight: false,

  start() { this._timer = setInterval(() => this._tick(), 1000); },
  stop() {
    clearInterval(this._timer);
    this._timer = null;
    this._pending.clear();
    this._ready.clear();
  },

  registerDispatcher(type, fn) { this._dispatchers.set(type, fn); },

  enqueue(type, key, payload, debounceMs) {
    this._pending.set(`${type}:${key}`, { type, key, payload, dueAt: Date.now() + debounceMs });
  },

  _tick() {
    const now = Date.now();
    for (const [k, task] of this._pending) {
      if (task.dueAt <= now) {
        this._pending.delete(k);
        if (!this._ready.has(task.type)) this._ready.set(task.type, new Map());
        this._ready.get(task.type).set(task.key, task);
      }
    }
    if (!this._inFlight) this._dispatchNext();
  },

  async _dispatchNext() {
    const now = Date.now();
    for (const [type, tasks] of this._ready) {
      if (tasks.size === 0) continue;
      if ((this._nextAttemptAt.get(type) || 0) > now) continue;
      const dispatcher = this._dispatchers.get(type);
      if (!dispatcher) continue;

      const batch = [...tasks.values()].slice(0, MAX_BATCH_SIZE);
      this._inFlight = true;
      try {
        const result = await dispatcher(batch);
        for (const key of result.succeededKeys) tasks.delete(key);
        this._failureCount.set(type, 0);
      } catch (e) {
        const n = (this._failureCount.get(type) || 0) + 1;
        this._failureCount.set(type, n);
        const delay = Math.min(5000 * 2 ** (n - 1), 5 * 60 * 1000);
        this._nextAttemptAt.set(type, Date.now() + delay);
        this._logDispatchFailure(type, batch, n, delay, e);
      } finally {
        this._inFlight = false;
      }
      break; // one type per tick; loop continues naturally on the next tick
    }
  },

  _logDispatchFailure(type, batch, attempt, delayMs, err) {
    const keys = batch.map(t => t.key).join(', ');
    const msg = `TaskQueue: ${type} dispatch failed (attempt ${attempt}, retry in ${delayMs}ms): ${err.message} [${keys}]`;
    if (attempt >= STUCK_THRESHOLD) {
      console.error(`TaskQueue: ${type} dispatch stuck after ${attempt} attempts, items: [${keys}]`);
    } else {
      console.warn(msg);
    }
  },
};
```

Constants: `MAX_BATCH_SIZE = 50`, `STUCK_THRESHOLD = 5` (roughly once backoff has climbed past ~1 minute).

Grouping ready tasks by `library_id` (since the backend call is per-library) happens inside the metadata dispatcher, not the generic engine — the engine only knows about `type` and opaque payloads.

### 5.2 `plugin/src/zotero-rag.js` changes

- New shared helper, extracted from the duplicated logic in `getCurrentLibrary()` (`zotero-rag.js:960-981`) and `_scanUnavailableCount()` (`zotero-rag.js:1811-1820`):
  ```js
  getBackendLibraryId(libraryID) {
      if (libraryID === Zotero.Libraries.userLibraryID) {
          const userId = this.getCurrentZoteroUserId();
          return userId ? `u${userId}` : String(libraryID);
      }
      const group = Zotero.Groups.getByLibraryID(libraryID);
      return group ? String(group.id) : String(libraryID);
  }
  ```
  `getCurrentLibrary()` and `_scanUnavailableCount()` are refactored to call this helper instead of duplicating the logic.

- The existing notifier (`zotero-rag.js:198-212`) gains a `modify` branch and the `delete` branch is fixed to use `getBackendLibraryId()`:
  ```js
  this._notifierID = Zotero.Notifier.registerObserver({
      notify: (event, type, ids, extraData) => {
          if (type !== 'item') return;
          if (event === 'delete') {
              for (const id of ids) {
                  const { libraryID, key } = extraData[id] || {};
                  if (!libraryID || !key) continue;
                  const backendLibraryId = this.getBackendLibraryId(libraryID);
                  const url = `${this.backendURL}/api/libraries/${backendLibraryId}/items/${key}/chunks`;
                  fetch(url, { method: 'DELETE', headers: this.getAuthHeaders() })
                      .catch(e => console.warn(`Failed to delete chunks for item ${key}: ${e.message}`));
              }
          } else if (event === 'modify') {
              for (const id of ids) {
                  const item = Zotero.Items.get(id);
                  if (!item || !item.isRegularItem()) continue;
                  const backendLibraryId = this.getBackendLibraryId(item.libraryID);
                  const snapshot = {
                      item_key: item.key,
                      title: item.getField('title') || null,
                      authors: this._extractAuthors(item),
                      tags: this._extractTags(item),
                      year: this._extractYear(item),
                      item_type: item.itemType || null,
                      item_version: item.version || 0,
                      zotero_modified: item.dateModified || new Date().toISOString(),
                  };
                  TaskQueue.enqueue('metadata', `${backendLibraryId}:${item.key}`, snapshot, METADATA_DEBOUNCE_MS);
              }
          }
      }
  }, ['item']);
  ```
  `_extractAuthors`/`_extractYear` are the same logic already in `remote_indexer.js:1289-1316` (moved to be shared, since `remote_indexer.js` is loaded only into dialog windows and this code must run at plugin scope regardless of any dialog being open). A new `_extractTags(item)` mirrors the backend's `_extract_tags`: `item.getTags().map(t => t.tag).filter(Boolean)`.

  `METADATA_DEBOUNCE_MS = 4000`.

- The metadata dispatcher, registered once in `init()`:
  ```js
  TaskQueue.registerDispatcher('metadata', async (tasks) => {
      const byLibrary = new Map();
      for (const task of tasks) {
          const [libraryId] = task.key.split(':');
          if (!byLibrary.has(libraryId)) byLibrary.set(libraryId, []);
          byLibrary.get(libraryId).push(task);
      }
      const succeededKeys = new Set();
      for (const [libraryId, libTasks] of byLibrary) {
          const res = await fetch(`${this.backendURL}/api/index/items/metadata`, {
              method: 'POST',
              headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
              body: JSON.stringify({
                  library_id: libraryId,
                  items: libTasks.map(t => t.payload),
              }),
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          for (const t of libTasks) succeededKeys.add(t.key);
      }
      return { succeededKeys };
  });
  ```
  (Simplified for the spec — the real implementation reuses the existing `_apiFetch`-style error handling for consistent error messages.) Note: if one library's request fails while another in the same batch succeeds, only the failed library's tasks remain in the ready map for retry — partial batch success is preserved.

- `TaskQueue.start()` is called at the end of `init()`; `TaskQueue.stop()` is called in `removeFromAllWindows()` (`zotero-rag.js:438-448`), alongside the existing `Zotero.Notifier.unregisterObserver()` call.

### 5.3 `bootstrap.js` change

Add one line loading the new module, before `zotero-rag.js` (since `zotero-rag.js`'s `init()` references `TaskQueue`):
```js
Services.scriptloader.loadSubScript(rootURI + 'task_queue.js');
Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
```

### 5.4 Backend changes

`backend/api/document_upload.py`:
```python
class ItemMetadataUpdate(BaseModel):
    item_key: str
    title: Optional[str] = None
    authors: list[str] = []
    tags: list[str] = []
    year: Optional[int] = None
    item_type: Optional[str] = None
    item_version: Optional[int] = None
    zotero_modified: Optional[str] = None
```

`batch_update_metadata` is simplified to delegate to the already-existing, more complete internal method instead of hand-assembling a partial `fields` dict:
```python
for item in request.items:
    n = vector_store.update_item_bibliographic_metadata(
        request.library_id,
        item.item_key,
        title=item.title,
        authors=item.authors,
        tags=item.tags,
        year=item.year,
        item_type=item.item_type,
        item_version=item.item_version or 0,
        zotero_modified=item.zotero_modified or "",
    )
    if n > 0:
        updated_items += 1
        updated_chunks += n
```
`update_item_bibliographic_metadata` (`vector_store.py:635-682`) gains one more keyword arg, `schema_version: int = CURRENT_SCHEMA_VERSION`, added to its `fields` dict alongside the existing ones. Every caller — the live-push endpoint here and the cron fast-path's existing call in `_try_metadata_only_update` (`document_processor.py:1638-1648`) — then keeps chunks marked as current schema as a side effect of any bibliographic patch, with no caller needing to special-case it (the cron path doesn't pass it explicitly and gets the default, which is the correct value in that context too).

No routing, auth, or response-shape changes — same URL, same `assert_can_access` check, same `BatchMetadataUpdateResult` response.

## 6. Data flow (worked example)

1. User edits an item's title, then a few seconds later adds a tag, in the Zotero UI.
2. First edit fires `modify`/`item` → handler checks `item.isRegularItem()` (true), builds a snapshot, computes `key = "u12345:ABCD1234"`, calls `TaskQueue.enqueue('metadata', key, snapshot, 4000)`.
3. Second edit (the tag) fires before 4s elapse → `enqueue` is called again with the same key, overwriting the payload (now includes the new tag) and resetting `dueAt`.
4. Once 4s pass with no further edits, the next tick moves the task into the `metadata` ready map.
5. No request is in flight, so `_dispatchNext()` groups ready tasks by `library_id` (just one item here) and POSTs the batch.
6. Backend patches the item's chunks via `update_item_bibliographic_metadata` → Qdrant `set_payload()` → 2xx response.
7. On success, the task is removed and the failure counter for `metadata` resets to 0.
8. If step 5 fails (network/5xx), the task stays in the ready map, `_nextAttemptAt` is set via backoff, and a warning (or, past `STUCK_THRESHOLD`, an error) is logged; the task is retried on a later tick once the backoff window passes.

A bulk edit (e.g. tagging 80 items at once) follows the same path: 80 `enqueue` calls with distinct keys become ready around the same time; dispatch sends them in batches of up to `MAX_BATCH_SIZE` (50), one batch per tick since only one request is ever in flight.

## 7. Error handling summary

| Failure | Behavior |
|---|---|
| Network unreachable / timeout | Batch retried with exponential backoff (5s → 10s → 20s → ... capped at 5min), indefinitely |
| Backend 5xx | Same as network failure |
| Backend 4xx | Same retry treatment — the client can't reliably distinguish transient from permanent 4xx without deeper inspection, which isn't warranted here; logging (below) makes it visible, and cron eventually reconciles the underlying data regardless |
| Item not yet indexed in Qdrant | Silent no-op; HTTP call still counts as success (`updated_items: 0` for that item, no error) |
| Plugin restarts mid-queue | In-memory queue is lost; unsent edits wait for the next hourly cron run |
| Item deleted while its metadata update is still queued/in-flight | The existing delete-notifier already fires a chunk-delete call; a subsequently-sent metadata update for the same key is a harmless no-op (chunks already gone) |
| One library's request fails within a mixed-library batch | Only that library's tasks remain queued for retry; other libraries' tasks in the same tick still succeed and are removed |

**Known limitation:** if a batch fails for a non-network reason (e.g. a single malformed item triggers a 500), the whole batch retries indefinitely per the "retry forever" policy, which can stall metadata sync for other items in that same library until backoff hits its cap — this is logged (escalating to `console.error` past `STUCK_THRESHOLD`) so it's diagnosable, and the hourly cron still corrects the underlying data independent of this queue. Isolating a single bad item within a failing batch is a possible future improvement, not built now.

## 8. Extensibility for future attachment sync (not built now)

The `TaskQueue` engine is entirely type-agnostic: adding attachment sync later means registering a new dispatcher (`TaskQueue.registerDispatcher('attachment', ...)`) and calling `TaskQueue.enqueue('attachment', key, payload, debounceMs)` from wherever attachment changes are detected — no changes to `_tick()`, `_dispatchNext()`, backoff, or logging. The per-type `_ready`/`_failureCount`/`_nextAttemptAt` maps already keep failure isolation between types, so a stuck `attachment` batch (likely to fail more often, given it would involve file uploads) would not block `metadata` dispatch, and vice versa.

## 9. Testing plan

- **Plugin unit tests** (`plugin/test/`): `TaskQueue` in isolation — debounce collapsing repeated `enqueue` calls to the same key, tick-based promotion to the ready map, sequential dispatch guard (`_inFlight`) via a mock dispatcher, backoff growth on repeated mock failure and reset on success, partial-batch success (some keys succeed, some don't), `start()`/`stop()` timer hygiene (no leaked interval after `stop()`).
- Unit tests for `_extractTags`, `getBackendLibraryId`, and the notifier's `modify` branch (using whatever `Zotero.Item` test doubles already exist in `plugin/test/`).
- **Backend tests** (`backend/tests/`): extend existing `/api/index/items/metadata` tests to cover `tags`/`item_version`/`zotero_modified`, and confirm delegating to `update_item_bibliographic_metadata` produces the same Qdrant payload as the cron/full-index path (`_try_metadata_only_update`'s existing call site).
- **Manual verification**: edit an item's title/tags in a running dev Zotero + backend, confirm the Qdrant payload updates within the debounce window without a re-embed (verify no attachment re-upload / embedding call happens); stop the backend mid-edit and confirm Browser Console shows the escalating warning→error log sequence as retries continue.
