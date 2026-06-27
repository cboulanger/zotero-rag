# Fix: Library 2829873 Indexing Failures

**Date**: 2026-06-27  
**Status**: Hotfix built and ready; remaining bugs identified for dev-machine work

---

## Current State (at time of diagnosis)

Library `groups/2829873` has **0 chunks** in Qdrant — completely un-indexed and unsearchable.  
A full scan is actively running in the container but will produce 0 chunks because the KISSKI embedding API key has expired.

The plugin shows `6013/6027 items (incomplete)` based on stale metadata; the actual indexed count is 0.

---

## Root Cause Chain

### 1. Reconcile-count returned wrong value (triggering the cascade)

During a diagnostic session, the `/libraries/2829873/reconcile-count` API endpoint was called.  
`count_indexed_items("2829873")` returned **195** when there were actually **217,318 chunks** with `library_id = "2829873"` — the correct number of unique item keys should have been ~6,010.

The cause of this incorrect count is **unexplained** (see open question below).

Setting `total_items_indexed = 195` caused `_resolve_mode` to detect under-indexing:
- `indexed = 195`, `zotero_total = 24,127` → ratio = 1% < 25% threshold
- `scan_floor = 6,010` (from prior full scan), `195 >= 6,010 * 0.9 = 5,409` → **false** → protection did not kick in

### 2. Wipe-and-rebuild code deleted 217,318 good chunks (09:00:15, June 27)

The container image was built 2026-06-24 18:59, before commit `4dcc438` (safe smart sync, 2026-06-25 15:56). It still runs the old wipe-and-rebuild `_index_library_full`. The under-indexed detection triggered a full rescan which deleted all chunks before re-indexing:
```
INFO Deleted 217318 chunks for library 2829873
INFO Deleted 241 deduplication records for library 2829873
```

### 3. KISSKI API key expired (~06:54, June 27)

The container has `KISSKI_API_KEY=0c7b1a01e614faa4d5c04879d352f99b` (expired).  
The current `.env` and deploy file (`dev machine`) have the correct key: `d1569768d3c8ae5634b9f134ead87a46`.

The new full scan (started 09:12 June 27) fails to embed anything → will complete with 0 chunks stored.

### 4. Incremental mode never advances `last_indexed_version` past 46693

After the last full scan, incremental mode fetches 3 items since v46693 (versions 46694–46696: all parent items with no indexable attachments and no substantial abstract). Since none pass `_filter_indexed_attachments`, `items_with_attachments` is empty, the inner loop never runs, and `max_version_seen` stays at `metadata.last_indexed_version`.

Result: the same 3 items are re-fetched every hour forever, and any new items added after v46693 are never picked up.

**Bug location**: `backend/services/document_processor.py`, `_index_library_incremental`, line ~217:
```python
max_version_seen = metadata.last_indexed_version  # initialized here
for idx, item in enumerate(items_with_attachments):  # empty → loop never runs
    max_version_seen = max(max_version_seen, item_version)  # never executes
metadata.last_indexed_version = max_version_seen  # stays unchanged
```

---

## Hotfix Applied on Server (deploy machine)

### Patch image built

`document_processor.py` was patched to fix the incremental version tracking bug (fix #4 above).  
The fix initializes `max_version_seen` from **all** fetched items before filtering:

```python
# After fetching items, before filtering:
max_version_seen = max(
    (item.get("version", 0) for item in items),
    default=metadata.last_indexed_version,
)
```

Patch image built with:
```bash
cat > /tmp/Dockerfile.patch << 'EOF'
FROM localhost/zotero-rag:latest
COPY backend/services/document_processor.py /app/backend/services/document_processor.py
EOF
sudo podman build -f /tmp/Dockerfile.patch -t zotero-rag:latest /home/cloud/zotero-rag
```

The incremental version tracking fix is already committed to the dev branch (in `document_processor.py`).

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
- Current failing scan is killed
- New container starts with patched image + correct KISSKI key
- Next cron run detects `195/24127` under-indexed → triggers new full scan
- Full scan now uses **old wipe-and-rebuild code** (safe smart sync not yet in image) but with the correct key
- All ~6,010 items will be successfully embedded and stored (~28 hours)

---

## Remaining Bugs to Fix on Development Machine

These fixes should be merged and deployed via CI after the hotfix stabilizes the server.

### Bug 1: `count_indexed_items` returns wrong count [INVESTIGATE]

`count_indexed_items("2829873")` returned 195 when 217,318 chunks existed with 6,010 unique item keys. The function scrolls all chunks matching `library_id` and collects unique `item_key` values. Both `store_item_chunks` and `copy_chunks_cross_library` correctly set `item_key` in the payload.

**Possible causes to investigate:**
- Qdrant scroll pagination skipping results during large collection modifications (concurrent writes during scroll)
- Qdrant payload index inconsistency after a large upsert batch
- A timing issue where the scroll ran against an in-progress Qdrant index rebuild

**Suggested investigation:**
1. After the new full scan completes, call `reconcile-count` and verify it returns ~6,010
2. If it returns a wrong value, add logging to `count_indexed_items` to print the raw scroll batch sizes
3. Check Qdrant's consistency mode settings (WAL, indexing threshold)

**Risk if not fixed:** A future `reconcile-count` call (e.g., triggered by the plugin after an upload batch) could again set `total_items_indexed` to a wrong value and trigger an unnecessary full rescan that wipes good chunks.

### Bug 2: Old wipe-and-rebuild code still in production image [DEPLOY]

The container image (built 2026-06-24) predates the safe smart sync commit `4dcc438`. The next CI build and deploy will include this fix automatically. Until then, a failed or interrupted full scan will wipe all existing chunks before re-indexing.

**Fix**: CI rebuild and `DEPLOY_PULL=true` deploy, or local rebuild:
```bash
sudo podman build -t zotero-rag:latest /home/cloud/zotero-rag
sudo systemctl restart zotero-rag.service
```

### Bug 3: `total_items_indexed` counts attempted items, not successful ones

In `_index_library_full` (both old and new code), line 400:
```python
metadata.total_items_indexed = len(items_with_attachments)
```
This is set to the count of items that had indexable content, regardless of whether embedding succeeded. If the KISSKI key is expired during a full scan, this will be set to 6,010 even though 0 chunks were stored. The `_resolve_mode` scan_floor protection then prevents a corrective rescan.

**Risk**: Library appears "indexed" (count = 6,010) but has 0 searchable chunks.

**Fix location**: `backend/services/document_processor.py`, `_index_library_full` (both old and new):
```python
# Change:
metadata.total_items_indexed = len(items_with_attachments)
# To (counts items that successfully contributed at least one chunk):
metadata.total_items_indexed = items_added
```
Note: `items_added` is already tracked in the loop. This requires also verifying the `_resolve_mode` thresholds still make sense with a "successful" count rather than "attempted" count.

### Bug 4: `reconcile-count` can silently corrupt metadata [DEFENSIVE]

The `POST /libraries/{library_id}/reconcile-count` endpoint overwrites `total_items_indexed` with the result of `count_indexed_items`. If `count_indexed_items` returns a wrong value (Bug 1), this will corrupt the metadata and may trigger unnecessary full rescans.

**Fix**: Before overwriting, sanity-check the new value:
```python
# In reconcile_library_count (backend/api/libraries.py):
actual_count = vector_store.count_indexed_items(library_id)
# Only update if the count is plausible (not a large drop from a populated library)
if meta.last_full_scan_indexable > 0 and actual_count < meta.last_full_scan_indexable * 0.5:
    logger.warning(
        f"reconcile-count: suspiciously low count {actual_count} "
        f"(scan_floor={meta.last_full_scan_indexable}) — skipping update"
    )
    raise HTTPException(status_code=409, detail=f"Computed count {actual_count} is implausibly low; not updating")
meta.total_items_indexed = actual_count
```

---

## Open Questions

1. **Why did `count_indexed_items` return 195?**  
   At the time of the call, `delete_library_chunks` later confirmed 217,318 chunks existed (it reported "Deleted 217318 chunks"). If `count_indexed_items` used the same `library_id = "2829873"` filter, it should have found ~6,010 unique item keys, not 195.  
   One hypothesis: Qdrant scroll pagination returned a partial result set during concurrent write activity (the async upload subsystem was queuing 43 jobs at the same time). Another hypothesis: the scroll hit the Qdrant timeout and returned a partial page.

2. **Why were there 3 items at versions 46694–46696 with no indexable content?**  
   The 3 items are: `journalArticle "A Marxist Analysis of American Law"`, `book "The Ideology of Advocacy..."`, `journalArticle "Commodity Form and Legal Form..."`. They may be records with no attached PDF and an abstract shorter than the 100-word threshold. These will be re-fetched every hour by the incremental cron until fix #4 (version tracking) is deployed — after which `last_indexed_version` will advance past 46696.

---

## Files Changed in This Session

| File | Change | Status |
|------|--------|--------|
| `backend/services/document_processor.py` | Fixed incremental version tracking (Bug 4 above) | Committed to dev |
| `.local/.env.deploy.zotero-rag.panya.de` | Updated KISSKI key | Not committed (secrets) |

---

## Deployment Checklist After Hotfix

- [ ] Apply KISSKI key + restart on server (see commands above)
- [ ] Verify new full scan starts in cron log: `sudo podman logs -f zotero-rag-zotero-rag-panya-de`
- [ ] After ~28h, verify chunk count: `sudo podman exec zotero-rag-zotero-rag-panya-de python3 -c "from qdrant_client import QdrantClient; from qdrant_client.models import Filter, FieldCondition, MatchValue; c = QdrantClient('http://qdrant:6333'); print(c.count('document_chunks', count_filter=Filter(must=[FieldCondition(key='library_id', match=MatchValue(value='2829873'))]), exact=True).count)"`
- [ ] Investigate Bug 1 (count_indexed_items returning wrong value)
- [ ] Fix Bug 3 (total_items_indexed counts attempted not successful)
- [ ] Fix Bug 4 (reconcile-count sanity check)
- [ ] CI rebuild with safe smart sync + all fixes → deploy
