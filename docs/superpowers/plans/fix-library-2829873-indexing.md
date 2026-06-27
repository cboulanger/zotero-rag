# Fix: Library 2829873 Indexing Failures

**Date**: 2026-06-27  
**Status**: Hotfix built and ready; dev-machine code fixes implemented (see "Resolution" below)

---

## Resolution (dev-machine code fixes — 2026-06-27)

The core failure was that **an invalid/expired embedding key produced no error**: every
embedding call failed with HTTP 401, but the per-item `except Exception` in both indexing
loops swallowed it, so the scan "completed" with 0 chunks, marked the slug `done`, and the
cron reported success. Fixes:

1. **Fatal embedding errors are now surfaced, not swallowed** (primary fix for the reported
   problem):
   - `backend/services/embeddings.py`: new `EmbeddingAuthenticationError`, raised by
     `RemoteEmbeddingService._create_embeddings_with_backoff` when the API returns HTTP
     401 (`AuthenticationError`) or 403 (`PermissionDeniedError`). Unlike 429/500, this is
     non-recoverable by retrying.
   - `backend/services/document_processor.py`: both `_index_library_full` and
     `_index_library_incremental` now re-raise `_FATAL_EMBEDDING_ERRORS`
     (`EmbeddingAuthenticationError`, `EmbeddingRateLimitExhaustedError`) instead of
     swallowing them per-item, aborting the run. (This also closes a latent hole where a
     rate-limit-exhausted error raised mid-loop would have been swallowed.)
   - `backend/services/cron_indexer.py`: `run()` now handles `EmbeddingAuthenticationError`
     — marks affected slugs `status="error"` with a descriptive message and records a
     top-level `embedding_auth_error` in `cron_status.json` (visible cross-process via the
     FastAPI root endpoint). `_index_slug` re-raises it so `run()` handles it centrally.

2. **Bug 3 — `total_items_indexed` counts successes, not attempts**
   (`_index_library_full`): now `items_added + items_updated + items_skipped` (items
   actually indexed), not `len(items_with_attachments)`. NOTE: the plan's literal
   suggestion `= items_added` is wrong for the *new* smart-sync `_index_library_full`,
   because a healthy re-scan **skips** already-current items (`items_added≈0`); the correct
   value includes `items_skipped`. `last_full_scan_indexable` stays
   `len(items_with_attachments)` (the scan floor).

3. **Bug 4 — `reconcile-count` sanity guard** (`backend/api/libraries.py`): refuses (HTTP
   409) to overwrite `total_items_indexed` when the recomputed count is < 50% of
   `last_full_scan_indexable`, preventing a concurrent-scroll undercount from triggering a
   destructive forced rescan.

4. **Bug 1 — `count_indexed_items`**: investigated and *not* changed. The plan's "wrong
   item_key stored" hypothesis is **not supported by the code** — both current and the
   container-era commit `da7eebf` store the correct parent `item_key` in `add_chunks_batch`
   (and `copy_chunks_cross_library` writes `target_item_key`). The 195-vs-6010 discrepancy
   was most consistent with a `scroll()` undercount while a scan was concurrently writing.
   The Bug 4 guard mitigates the *consequence* regardless of the precise cause.

**Tests** (all green; new tests verified red→green via `git stash`):
`test_embeddings.py::TestEmbeddingAuthenticationError`,
`test_document_processor.py` (auth-error propagation in full + incremental;
`total_items_indexed` counts successes), `test_cron_indexer.py::TestEmbeddingAuthErrorHandling`,
`test_reconcile_count.py`.

**Still deployment-only (not code):** Bug 2 (old wipe-and-rebuild image in production) needs
the CI rebuild + `DEPLOY_PULL=true` deploy; KISSKI key rotation in the systemd unit.

---

**Date**: 2026-06-27  
**Status**: Hotfix built and ready; remaining bugs identified for dev-machine work

---

## Current State (at time of diagnosis)

Library `groups/2829873` has **0 chunks** in Qdrant — completely un-indexed and unsearchable.  
A full scan is actively running in the container but will produce 0 chunks because the KISSKI embedding API key has expired.

The plugin shows `6013/6027 items (incomplete)` based on stale metadata; the actual indexed count is 0.

---

## Event Timeline (all times UTC+2 / CEST, June 2026)

| Time | Event |
|------|-------|
| Jun 24 18:59 | Container image built and deployed (pre-`4dcc438`, old wipe-and-rebuild code) |
| Jun 25 15:00 | Full scan started for library 2829873 (old code, deleted 0 existing chunks) |
| Jun 26 06:59 | Server log entry: KISSKI `/models` health check returned 401 (possible brief key hiccup, embeddings still worked) |
| Jun 26 18:58 | Full scan completed: `items=6010 added=6010 chunks=215399 elapsed=100709s`; metadata set to `total_items_indexed=6010, last_full_scan_indexable=6010, last_indexed_version=46693` |
| Jun 26 19:00–Jun 27 08:00 | Hourly incremental cron runs: 3 items fetched (v46694–46696), 0 indexable, 0 processed, version stays at 46693 |
| Jun 27 06:54 | First embedding 401 in server log (`Error processing upload for IYB3QM2V`) — KISSKI key now reliably expired |
| Jun 27 07:00–07:14 | Several async upload embedding calls succeed briefly |
| Jun 27 07:14–07:18 | Cascade of 401 errors on async uploads |
| Jun 27 07:20–07:53 | 43 async upload requests queued (pending, not yet processed) |
| Jun 27 ~07:53 | **`POST /libraries/2829873/reconcile-count` called** (during diagnostic session); `count_indexed_items("2829873")` returned **195**; metadata updated: `total_items_indexed=195` |
| Jun 27 09:00:10 | `_resolve_mode` detects `195/24127 items (1% < 25%)`; scan_floor check fails (`195 < 5409`); forces full rescan |
| Jun 27 09:00:15 | Old wipe-and-rebuild code **deletes 217,318 chunks** and 241 dedup records for library 2829873 |
| Jun 27 09:12 | New full scan starts; fetches 24,127 items from Zotero |
| Jun 27 09:12+ | All embedding calls fail (401); scan will complete with 0 chunks stored |

---

## Root Cause Chain

### 1. Reconcile-count returned wrong value — triggering the cascade

**What happened**: `POST /libraries/2829873/reconcile-count` → `count_indexed_items("2829873")` returned **195** when the correct answer was ~**6,010 unique item keys** across 217,318 chunks.

**Code path** (`backend/api/libraries.py:175`, `backend/db/vector_store.py:1218`):
```python
# reconcile_library_count handler:
actual_count = vector_store.count_indexed_items(library_id)  # returned 195
meta.total_items_indexed = actual_count                       # overwrote 6010 with 195
vector_store.update_library_metadata(meta)

# count_indexed_items implementation:
def count_indexed_items(self, library_id: str) -> int:
    item_keys: set[str] = set()
    offset = None
    while True:
        results, next_offset = self.client.scroll(
            collection_name=self.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="library_id", match=MatchValue(value=library_id))
            ]),
            limit=1000,
            offset=offset,
            with_payload=["item_key"],
            with_vectors=False,
            timeout=self.qdrant_timeout,
        )
        for point in results:
            ik = point.payload.get("item_key")
            if ik:
                item_keys.add(ik)
        if next_offset is None:
            break
        offset = next_offset
    return len(item_keys)
```

**Evidence** (see `fix-library-2829873-indexing-logs.md` for full excerpts):

At **Jun 27 06:14** — 11 hours after the full scan stored 215,399 chunks — the plugin's
`check-indexed` calls showed **all 25 items per batch as `not_indexed`**. Only items being
concurrently processed by async uploads transitioned to `up_to_date`, and only after their
upload completed. Items supposedly covered by the full scan never appeared.

At **Jun 27 08:20** — not 07:53 as previously noted — `count_indexed_items("2829873")` returned
**195**. This 195 corresponds precisely to the items successfully async-uploaded before KISSKI
expired at 06:54 (about 15 minutes of successful uploads during the 06:14 session).

At **Jun 27 09:00:15**, `delete_library_chunks("2829873")` deleted **217,318 chunks** — confirming
those chunks existed with `library_id = "2829873"`.

**Definitive conclusion**: The 215,399 chunks from the June 25-26 full scan had the correct
`library_id = "2829873"` (found by DELETE) but **item_key values that do not match parent Zotero
item keys** (invisible to check-indexed and count_indexed_items, both of which filter by item_key).
Only the 195 items indexed via async upload had correct parent item_keys and were counted.

**Root cause to confirm**: check commit `da7eebf` — the vector_store.py version in the container
(built Jun 24, the most recent commit to that file before the container build). Either
`add_chunks_batch` / `store_item_chunks` at that commit stored wrong item_key values, or
`_process_attachment_bytes` at that commit passed the wrong value for `doc_metadata.item_key`.

```bash
# Check what item_key value the container's add_chunks_batch stored:
git show da7eebf:backend/db/vector_store.py | grep -A 50 "def add_chunks_batch"

# Check what the container's _process_attachment_bytes passed as item_key:
git show 4dcc438^:backend/services/document_processor.py | grep -B5 -A5 "item_key"

# Cross-library copy: did copy_chunks_cross_library use the right target item_key?
git show da7eebf:backend/db/vector_store.py | grep -A 60 "def copy_chunks_cross_library"
```

Specific things to look for:
- Is `"item_key"` present in the payload dict in `add_chunks_batch` at `da7eebf`?
- If yes, is it `chunk.metadata.document_metadata.item_key` (parent key) or `attachment_key`?
- In `copy_chunks_cross_library` at `da7eebf`: is `"item_key": target_item_key` in the new payload, or is the source item_key preserved?

### 2. Wipe-and-rebuild code deleted 217,318 good chunks (09:00:15, June 27)

The running container image predates commit `4dcc438` (`fix: replace wipe-and-rebuild full indexing with safe smart sync`, Jun 25 15:56). The old `_index_library_full` in the container:

```python
# OLD code (pre-4dcc438) — container version:
async def _index_library_full(self, library_id, library_type, metadata, ...):
    chunks_deleted = self.vector_store.delete_library_chunks(library_id)   # WIPES ALL CHUNKS FIRST
    dedup_deleted  = self.vector_store.delete_library_deduplication_records(library_id)
    items = await self.zotero_client.get_library_items_since(library_id, library_type, since_version=None)
    items_with_attachments = await self._filter_indexed_attachments(items, library_id, library_type)
    for item in items_with_attachments:
        chunk_count = await self._index_item(item, library_id, library_type)
        ...
    metadata.total_items_indexed = len(items_with_attachments)   # set regardless of embedding success
    metadata.last_full_scan_indexable = len(items_with_attachments)
```

The new code (`_index_library_full` post-`4dcc438`, already in current repo) never deletes chunks upfront — it does a smart diff (add new, update changed, delete orphaned). An interrupted or failed scan leaves existing chunks intact.

### 3. KISSKI API key expired (~06:54, June 27)

The container env var `KISSKI_API_KEY=0c7b1a01e614faa4d5c04879d352f99b` expired. The correct key `d1569768d3c8ae5634b9f134ead87a46` is in the repo's `.env` file and has been updated in `.local/.env.deploy.zotero-rag.panya.de`. The systemd service file at `/etc/systemd/system/zotero-rag.service` still has the old key hardcoded in the `ExecStart` line.

### 4. Incremental mode never advances `last_indexed_version` past 46693

**The 3 stuck items**: Zotero library 2829873 has exactly 3 items modified after v46693:
- v46694: journalArticle "A Marxist Analysis of American Law"
- v46695: book "The Ideology of Advocacy..."  
- v46696: journalArticle "Commodity Form and Legal Form..."

All 3 are parent items with no attached PDF and (presumably) abstracts shorter than the 100-word threshold in `_filter_indexed_attachments`. They pass neither the attachment filter nor the abstract filter, so `items_with_attachments = []`.

**The bug** (`backend/services/document_processor.py`, `_index_library_incremental`):
```python
# Bug: max_version_seen only advances inside the loop over items_with_attachments
max_version_seen = metadata.last_indexed_version   # = 46693

for idx, item in enumerate(items_with_attachments):  # [] → loop body never runs
    item_version = item["version"]
    max_version_seen = max(max_version_seen, item_version)  # never called

metadata.last_indexed_version = max_version_seen   # still 46693 — no progress
```

**Effect**: Every cron hour, the same 3 items are fetched from Zotero (`since=46693`), filtered to zero, and the version is written back unchanged. Any new items added to the library after v46693 will never be discovered by incremental mode.

**Fix already committed** (this session, commit `98e93c3`): compute `max_version_seen` from all fetched items before filtering:
```python
max_version_seen = max(
    (item.get("version", 0) for item in items),
    default=metadata.last_indexed_version,
)
# Then filter:
items_with_attachments = await self._filter_indexed_attachments(...)
```

---

## Hotfix Applied on Server (deploy machine)

### Patch image built

```bash
cat > /tmp/Dockerfile.patch << 'EOF'
FROM localhost/zotero-rag:latest
COPY backend/services/document_processor.py /app/backend/services/document_processor.py
EOF
sudo podman build -f /tmp/Dockerfile.patch -t zotero-rag:latest /home/cloud/zotero-rag
# Verified: sudo podman run --rm zotero-rag:latest grep -c "max_version_seen = max(" /app/backend/services/document_processor.py  → 3
```

The incremental version fix is in the image. The image does NOT yet contain the safe smart sync `_index_library_full` (that requires a full image rebuild via CI).

### Steps to apply hotfix on server (requires sudo)

```bash
# 1. Update KISSKI key in systemd service file
sudo sed -i 's/-e KISSKI_API_KEY=0c7b1a01e614faa4d5c04879d352f99b/-e KISSKI_API_KEY=d1569768d3c8ae5634b9f134ead87a46/' /etc/systemd/system/zotero-rag.service

# 2. Reload daemon and restart service
sudo systemctl daemon-reload && sudo systemctl restart zotero-rag.service

# 3. Verify startup after ~10s
sleep 10 && sudo systemctl status zotero-rag.service | head -5
```

**What happens after restart:**
- Current failing scan is killed (0 chunks would have been stored anyway)
- New container starts with patched image + correct KISSKI key
- Next cron run (~within the hour) detects `total_items_indexed=195`, `zotero_total=24127` → under-indexed → triggers full scan
- Full scan uses **old wipe-and-rebuild code** (no chunks exist to wipe, so no risk) with working KISSKI key
- ~6,010 items indexed over ~28 hours

---

## Remaining Bugs to Fix on Development Machine

### Bug 1: `count_indexed_items` may return wrong count [INVESTIGATE FIRST]

**Risk**: Every call to `POST /libraries/{library_id}/reconcile-count` (triggered by the plugin after an upload batch) can corrupt `total_items_indexed` if `count_indexed_items` returns an incorrect value, which then triggers an unnecessary full rescan that wipes all chunks.

**What to investigate** (see detailed hypotheses in Root Cause section above):

1. Check `item_key` presence in payload at container-era commit `da7eebf`:
   ```bash
   git show da7eebf:backend/db/vector_store.py | grep -A 50 "def add_chunks_batch"
   git show da7eebf:backend/db/vector_store.py | grep -A 30 "def store_item_chunks"
   ```

2. Check whether `_process_attachment_bytes` at `da7eebf` set `doc_metadata.item_key` correctly:
   ```bash
   git show da7eebf:backend/services/document_processor.py | grep -B5 -A5 "item_key"
   ```

3. Write a unit test that indexes a mock item with 3 chunks, then calls `count_indexed_items` and asserts the return value is 1 (one unique item_key). Run it against both the current code and, if possible, the `da7eebf` version of vector_store.

4. Check the Qdrant client version used at container time to understand scroll-under-concurrent-writes behaviour:
   ```bash
   git show da7eebf:backend/pyproject.toml | grep qdrant
   ```

### Bug 2: Old wipe-and-rebuild code still in production image [DEPLOY via CI]

Commit `4dcc438` is in the current repo but not in the deployed image (built Jun 24). The new `_index_library_full` does a smart diff — never wipes chunks upfront. Trigger a CI build and `DEPLOY_PULL=true` deploy.

Until CI deploys: the hotfix image has the incremental fix but NOT the safe smart sync. Any new forced full scan will still wipe-and-rebuild (acceptable while there are 0 chunks to wipe).

### Bug 3: `total_items_indexed` counts attempted items, not successful ones

**Location**: `backend/services/document_processor.py`, `_index_library_full`, near the end of both old and new versions:
```python
# WRONG — set even when all items failed to embed:
metadata.total_items_indexed = len(items_with_attachments)   # e.g. 6010 even if 0 chunks stored

# CORRECT — count only items that contributed at least one chunk:
metadata.total_items_indexed = items_added
```

`items_added` is already tracked in the loop (incremented when `_index_item` returns > 0 chunks). No new counter needed.

**Consequence of the bug**: If a full scan runs with an expired embedding key, it completes with `total_items_indexed = 6010` and `last_full_scan_indexable = 6010` but 0 actual chunks. The `_resolve_mode` scan_floor protection then keeps `_resolve_mode` returning "auto" indefinitely (`6010 >= 5409`), so the library is silently un-indexed with no automatic recovery.

**Current code reference**: `backend/services/document_processor.py:400` (new safe-sync `_index_library_full`):
```python
metadata.total_items_indexed = len(items_with_attachments)   # line 400
metadata.last_full_scan_indexable = len(items_with_attachments)  # line 401
```

**Note on threshold impact**: After the fix, `total_items_indexed` reflects successfully-embedded items. If most items embed successfully (typical case), the value is ~6,010. If the key expires mid-scan and 3,000 items are processed before failure, the value is 3,000. `_resolve_mode` would then see `3000/24127 = 12% < 25%` but `scan_floor = 6010`, `3000 >= 5409 → false` → would force another full scan. This is the correct behaviour.

### Bug 4: `reconcile-count` can silently corrupt metadata [DEFENSIVE]

**Location**: `backend/api/libraries.py:175`, `reconcile_library_count`

**Fix**: add a sanity check before overwriting `total_items_indexed`:

```python
@router.post("/libraries/{library_id}/reconcile-count", response_model=LibraryIndexMetadata)
def reconcile_library_count(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    meta = vector_store.get_library_metadata(library_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Library not indexed")

    actual_count = vector_store.count_indexed_items(library_id)

    # Guard against a buggy/partial count wiping a well-indexed library.
    # If the computed count drops by more than 50% from the established scan floor,
    # refuse the update and let the caller decide (prevents cascade wipe like June 27 incident).
    if meta.last_full_scan_indexable > 0 and actual_count < meta.last_full_scan_indexable * 0.5:
        logger.warning(
            "reconcile-count: computed count %d is < 50%% of scan_floor %d for library %s — refusing update",
            actual_count, meta.last_full_scan_indexable, library_id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Computed count {actual_count} is implausibly low relative to "
                f"scan_floor {meta.last_full_scan_indexable}. Not updating. "
                "If the library was intentionally re-indexed to fewer items, "
                "run a full scan first to reset last_full_scan_indexable."
            ),
        )

    meta.total_items_indexed = actual_count
    vector_store.update_library_metadata(meta)
    logger.info("Reconciled total_items_indexed for library=%s: %d", library_id, actual_count)
    return meta
```

---

## Key Code Locations for Dev-Machine Work

| Symbol | File | Notes |
|--------|------|-------|
| `count_indexed_items` | `backend/db/vector_store.py:1218` | Scroll-based unique-item-key count; suspected source of Bug 1 |
| `reconcile_library_count` | `backend/api/libraries.py:175` | Calls above; needs sanity check (Bug 4) |
| `_index_library_incremental` | `backend/services/document_processor.py:180` | Version tracking bug fixed in commit `98e93c3` |
| `_index_library_full` (new) | `backend/services/document_processor.py:298` | `total_items_indexed = len(items_with_attachments)` at line 400 (Bug 3) |
| `_resolve_mode` | `backend/services/cron_indexer.py:330` | Under-indexed detection; uses `total_items_indexed` and `last_full_scan_indexable` |
| `add_chunks_batch` | `backend/db/vector_store.py:~270` | Check `item_key` in payload dict (Bug 1 investigation) |

---

## Deployment Checklist After Hotfix

- [ ] Apply KISSKI key + restart on server (commands in hotfix section above)
- [ ] Verify new full scan starts: `sudo podman logs -f zotero-rag-zotero-rag-panya-de | grep 2829873`
- [ ] After ~28h, verify chunk count reaches ~215,000:
  ```bash
  sudo podman exec zotero-rag-zotero-rag-panya-de python3 -c "
  from qdrant_client import QdrantClient
  from qdrant_client.models import Filter, FieldCondition, MatchValue
  c = QdrantClient('http://qdrant:6333')
  r = c.count('document_chunks', count_filter=Filter(must=[FieldCondition(key='library_id', match=MatchValue(value='2829873'))]), exact=True)
  print('chunks:', r.count)
  "
  ```
- [ ] Investigate Bug 1 (`count_indexed_items` root cause) — do this before next `reconcile-count` call
- [ ] Fix Bug 3 (`total_items_indexed = items_added`) + tests
- [ ] Fix Bug 4 (`reconcile-count` sanity check)
- [ ] CI rebuild (includes safe smart sync) → `DEPLOY_PULL=true` deploy

---

## Files Changed in This Session

| File | Change | Committed |
|------|--------|-----------|
| `backend/services/document_processor.py` | Fixed incremental version tracking (Bug 4 root cause fix) | Yes — `98e93c3` |
| `.local/.env.deploy.zotero-rag.panya.de` | Updated KISSKI key (secrets — not committed) | No |
