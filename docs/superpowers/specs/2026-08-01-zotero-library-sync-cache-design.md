# Zotero Library Sync Cache — Design Spec

## 1. Goal

Provide a standalone, reusable module that gives callers a "download all items
in this library" function which is cheap to call repeatedly: the first call
does a full fetch, every subsequent call fetches only what changed (added,
updated, deleted) since the last call, using Zotero's own versioning
primitives. This removes the need for any caller — starting with the planned
`ZoteroCatalogMetadataStrategy` (see
`docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md`
§5.3) — to re-download and re-filter an entire library's item set on every
invocation.

This is a preparatory/infrastructure piece: it does not itself change any
chapter-segmentation behavior. It is designed to be adopted by
`ZoteroCatalogMetadataStrategy` when that strategy is implemented, and
potentially by other future callers that need "give me this library's items,
efficiently, repeatedly" (out of scope to migrate existing callers now — see
§8).

## 2. Non-goals

- **Not a replacement for `document_processor.py`'s existing indexing sync.**
  That pipeline has its own incremental-sync logic tied to Qdrant chunk
  storage and embedding generation; it solves a different problem (What text
  needs (re-)embedding?) and is left untouched.
- **Not a distributed/multi-process cache.** Single-writer-at-a-time is
  assumed. No cross-process locking is implemented (see §6).
- **Not a generic Zotero item cache for collections or tags.** Only items
  (all item types) are synced, matching how Zotero's own version/deletion
  semantics work (see §4). Collections/tags caching is a possible future
  extension, not built here.
- **Does not fix `get_library_items_since`'s existing silent-partial-result
  behavior for any of its current callers** (`document_processor.py`) — that
  behavior is preserved by default (see §5's `raise_on_error` addition, which
  defaults to `False`).

## 3. Module & location

New module: `backend/zotero/library_cache.py`, alongside the existing
`backend/zotero/web_api.py`. It wraps a `ZoteroWebAPI` instance as a
dependency (not a subclass) — it adds a caching/sync-state layer on top of
the existing HTTP client, without duplicating any pagination, auth, or
rate-limit logic.

```python
from dataclasses import dataclass
from pathlib import Path

from backend.zotero.web_api import ZoteroWebAPI


@dataclass(frozen=True)
class SyncResult:
    """Summary of a single sync() call."""
    added: int
    updated: int
    deleted: int
    library_version: int
    was_full_sync: bool


class ZoteroLibraryCache:
    """Local SQLite cache of a single Zotero library's items, kept in sync
    via Zotero's version-based incremental sync primitives."""

    def __init__(
        self,
        client: ZoteroWebAPI,
        library_id: str,
        library_type: str,
        cache_path: Path,
    ) -> None:
        ...

    async def sync(self, force_full: bool = False) -> SyncResult:
        """Bring the local cache up to date with the remote library.

        Performs a full sync if the cache is empty (or force_full=True),
        otherwise an incremental sync using the stored high-water-mark
        version. Raises `LibrarySyncError` if the remote fetch fails
        partway (see §5) — the cache is left at its last-known-good state.
        """
        ...

    async def get_all_items(self, item_types: list[str] | None = None) -> list[dict]:
        """Sync, then return cached items as raw Zotero item dicts
        (key/version/library/data), optionally filtered to the given
        `data.itemType` values. Filtering happens in SQL, not in Python,
        so callers that only want e.g. book/bookSection items never pay
        to deserialize the rest of the library.
        """
        ...


class LibrarySyncError(Exception):
    """Raised when sync() cannot complete a fetch and must abort without
    advancing the stored library version."""
```

`library_id`/`library_type` follow the same backend-format convention as
`ZoteroWebAPI` (`u12345` for user libraries, `678` for group libraries) — the
cache passes them straight through to the wrapped client, no translation of
its own.

One SQLite file per library, at
`<cache_path>/<library_type>_<library_id>.sqlite3`
(e.g. `data/zotero_cache/user_u12345.sqlite3`,
`data/zotero_cache/group_678.sqlite3`).

## 4. Schema

```sql
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    data TEXT NOT NULL          -- full raw Zotero item JSON: {key, version, library, data, ...}
);
CREATE INDEX IF NOT EXISTS idx_items_item_type ON items(item_type);

CREATE TABLE IF NOT EXISTS sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 0),   -- singleton row, id is always 0
    library_version INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT NOT NULL
);
```

`item_type` is extracted from `item["data"]["itemType"]` at write time so
`get_all_items(item_types=[...])` can filter with a plain `WHERE item_type IN
(...)` clause. `data` stores the *entire* raw item object returned by the
Zotero API (not just the `data` sub-object) so callers get the same shape
`ZoteroWebAPI.get_library_items_since` already returns — no information is
lost or reshaped by caching.

The DB connection is opened with `PRAGMA journal_mode=WAL` so a `sync()`
write transaction doesn't block concurrent `get_all_items()` reads from
other tasks in the same process.

A `library_version` of `0` (the default for a freshly created `sync_state`
row, and the state before any row exists) is the signal "never synced" and
triggers a full sync — mirroring how `document_processor.py` treats a
missing/zero `last_indexed_version` on `LibraryIndexMetadata` (see
`backend/models/library.py`).

## 5. Sync algorithm

Mirrors the existing full/incremental logic in
`backend/services/document_processor.py`'s `_index_library_full` /
`_index_library_incremental` (same high-water-mark approach: the new stored
version is the max `version` field across *all* fetched items, not a
separate `get_library_version()` call) — this is deliberate, so the two
places in the codebase that do Zotero version-based sync behave identically
rather than subtly differently.

**Full sync** (`library_version == 0`, or `force_full=True`):

```python
items = await client.get_library_items_since(
    library_id=self.library_id,
    library_type=self.library_type,
    since_version=None,
    raise_on_error=True,
)
# upsert every item; no deletion pass needed (cache was empty or is being rebuilt)
max_version_seen = max((item.get("version", 0) for item in items), default=0)
```

**Incremental sync** (otherwise):

```python
items = await client.get_library_items_since(
    library_id=self.library_id,
    library_type=self.library_type,
    since_version=stored_version,
    raise_on_error=True,
)
deleted_keys = await client.get_deleted_item_keys(
    library_id=self.library_id,
    library_type=self.library_type,
    since_version=stored_version,
    raise_on_error=True,
)
# upsert changed/added items, delete rows for deleted_keys
max_version_seen = max((item.get("version", 0) for item in items), default=stored_version)
```

In both cases, all row upserts/deletes **and** the `sync_state` version bump
happen inside a single SQLite transaction (`BEGIN` / `COMMIT`). A crash or
exception mid-sync leaves the cache at its previous consistent state — never
a bumped version with missing/partial data, and never partially-applied
row changes with a stale version (which would cause some changes to be
silently skipped on the next incremental sync).

`SyncResult.added` vs `.updated` is determined by checking, before writing,
which of the fetched items' keys already exist in the `items` table (a
single `SELECT key FROM items WHERE key IN (...)` against the fetched key
set): keys not found are counted as `added`, keys found are `updated`, then
all are written with one `INSERT OR REPLACE`. `.deleted` is simply
`len(deleted_keys)` for the incremental path, or `0` for a full sync.

### Closing a correctness gap, not inheriting it

`ZoteroWebAPI.get_library_items_since` and `get_deleted_item_keys` currently
swallow HTTP errors mid-pagination: on a non-200 response they log and
return whatever was accumulated so far, with no signal to the caller that
the result is a truncated partial rather than the complete delta.
`document_processor.py` accepts this risk today (a pre-existing, unrelated
behavior this spec does not change for that caller).

For `ZoteroLibraryCache`, silently treating a partial fetch as "the whole
delta" would be worse than doing nothing: it would advance the stored
version past data the cache never actually received, and the gap would
never self-heal on a later sync (the version watermark has already moved
past it). To prevent this, add an opt-in `raise_on_error: bool = False`
parameter to both methods in `backend/zotero/web_api.py`:

```python
# backend/zotero/web_api.py — modify get_library_items_since and get_deleted_item_keys

async def get_library_items_since(
    self,
    library_id: str,
    library_type: str = "user",
    since_version: Optional[int] = None,
    limit: Optional[int] = None,
    start: int = 0,
    raise_on_error: bool = False,
) -> list[dict[str, Any]]:
    ...
    while True:
        params["start"] = current_start
        async with self.session.get(url, params=params) as resp:
            await self._handle_rate_limit(resp)
            if resp.status != 200:
                logger.error("get_library_items_since failed: HTTP %s", resp.status)
                if raise_on_error:
                    raise LibraryFetchError(
                        f"get_library_items_since failed: HTTP {resp.status}"
                    )
                break
            items = await resp.json()
            if not isinstance(items, list):
                if raise_on_error:
                    raise LibraryFetchError("get_library_items_since: non-list response body")
                break
            ...
```

```python
async def get_deleted_item_keys(
    self,
    library_id: str,
    library_type: str = "user",
    since_version: int = 0,
    raise_on_error: bool = False,
) -> list[str]:
    ...
    if resp.status == 200:
        data = await resp.json()
        return data.get("items", [])
    logger.warning("get_deleted_item_keys failed: HTTP %s", resp.status)
    if raise_on_error:
        raise LibraryFetchError(f"get_deleted_item_keys failed: HTTP {resp.status}")
    return []
```

`LibraryFetchError` is a new exception class defined in `web_api.py`
(`class LibraryFetchError(Exception): pass`). Default value `False` for
`raise_on_error` on both methods means every existing call site
(`document_processor.py`, `cron_indexer.py`, any test) is unaffected —
this is a strictly additive change.

`ZoteroLibraryCache.sync()` passes `raise_on_error=True` for both calls,
catches `LibraryFetchError`, rolls back the SQLite transaction (or simply
never commits it), and re-raises as `LibrarySyncError` so callers get a
cache-module-specific exception type without needing to import
`web_api.py`'s internals.

## 6. Concurrency

SQLite WAL mode permits concurrent readers during a writer's transaction.
No distributed or cross-process write lock is implemented — the design
assumes a single process drives `sync()` for a given library at a time
(e.g. one script invocation, or one strategy call within one indexing run).
If two processes ever called `sync()` on the same library concurrently,
SQLite's own locking would serialize the writes (one blocks until the other
commits), so there is no data-corruption risk — just no coordination to
avoid redundant work. This is a deliberate scope limit (YAGNI): nothing in
the current or planned call sites needs multi-process sync coordination for
this cache specifically. If that need arises, revisit with a real
requirement rather than building it speculatively now.

## 7. Settings

New field in `backend/config/settings.py`, following the exact pattern
already used for `review_queue_path`:

```python
zotero_cache_path: Optional[Path] = Field(
    default=None,
    description="Directory for per-library Zotero item sync caches (SQLite). "
                "Defaults to <data_path>/zotero_cache."
)
```

Added to the `expand_path` field_validator's field list (alongside
`review_queue_path`) and given a default-fill line in `set_derived_paths`:

```python
if self.zotero_cache_path is None:
    self.zotero_cache_path = self.data_path / "zotero_cache"
```

And a `mkdir` line in `ensure_directories()`, matching the existing
`review_queue_path` block:

```python
if self.zotero_cache_path:
    self.zotero_cache_path.mkdir(parents=True, exist_ok=True)
```

## 8. Integration point (informational — not part of this implementation)

When `ZoteroCatalogMetadataStrategy` (chapter-segmentation strategy pipeline
spec, §5.3) is implemented, it constructs one `ZoteroLibraryCache` per
library (reusing the same `ZoteroWebAPI` instance already passed into
`run()`) and calls:

```python
cache = ZoteroLibraryCache(
    client=zotero_client,
    library_id=library_id,
    library_type=library_type,
    cache_path=settings.zotero_cache_path,
)
book_sections = await cache.get_all_items(item_types=["bookSection"])
```

instead of re-fetching and re-filtering the whole library on every book
processed. This integration is **not** part of this spec's implementation
scope — it belongs to the strategy-pipeline plan, which will reference this
module once it exists. No other existing call site
(`document_processor.py`, `cron_indexer.py`, `chapter_upload.py`,
`chapter_retrofit.py`) is modified to use `ZoteroLibraryCache` as part of
this work; migrating them is a separate, future decision (see §2).

## 9. Error handling summary

- Network/HTTP failures during `sync()` raise `LibrarySyncError`; the local
  cache is left exactly as it was before the call (no partial state).
- A caller that gets `LibrarySyncError` from `get_all_items()` (which calls
  `sync()` internally) can catch it and fall back to reading whatever is
  already cached (`get_all_items` could optionally expose a
  `skip_sync: bool = False` escape hatch for this — deferred until a caller
  actually needs it, per YAGNI; not built in this pass).
- Malformed/missing `data.itemType` on a fetched item (should not happen per
  the Zotero API contract, but defensively): stored as `item_type = ""`
  rather than raising, so a single malformed item can't abort an entire
  sync; it simply won't match any `item_types` filter.

## 10. Testing

Unit tests in `backend/tests/test_zotero_library_cache.py`, using
`unittest.IsolatedAsyncioTestCase` and a fake/mock `ZoteroWebAPI` (no real
HTTP), covering:

- First `sync()` call on an empty cache → full fetch path, all items stored,
  `sync_state.library_version` set to the max version seen.
- Second `sync()` call → incremental fetch path using the stored version;
  added/updated items upserted, deleted keys removed from `items`.
- `force_full=True` → re-runs the full-fetch path even when a stored version
  already exists.
- `get_all_items(item_types=["bookSection"])` → only matching rows returned;
  a mixed-type fixture confirms non-matching rows are excluded.
- `raise_on_error` propagation: mock `ZoteroWebAPI.get_library_items_since`
  to raise `LibraryFetchError`; assert `sync()` raises `LibrarySyncError` and
  `sync_state.library_version` is unchanged from before the call.
- Two consecutive `sync()` calls with no remote changes → second call is a
  no-op incremental sync (empty `items`/`deleted_keys`) and `SyncResult`
  reports `added=0, updated=0, deleted=0`.

`backend/tests/test_web_api.py` (or wherever `ZoteroWebAPI` is currently
tested — confirm at implementation time) gets new cases for the
`raise_on_error=True` path on `get_library_items_since` and
`get_deleted_item_keys`, confirming the default (`False`) behavior is
byte-for-byte unchanged from today.
