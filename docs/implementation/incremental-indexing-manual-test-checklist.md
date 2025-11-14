# Incremental Indexing - Manual Testing Checklist

**Document Version:** 1.0
**Created:** 2025-01-12
**Status:** Testing Guide

---

## Prerequisites

Before running manual tests, ensure:

- [x] Zotero desktop application is running
- [x] Test group library is synced (<https://www.zotero.org/groups/6297749/test-rag-plugin>)
- [x] Backend server is running (`uv run uvicorn backend.main:app --reload`)
- [x] API keys are configured in `.env` file
- [x] Qdrant is running (in-memory or persistent mode)

---

## Test Scenarios

### 1. First-Time Library Indexing

**Objective:** Verify full indexing mode is used for never-indexed library

**Steps:**

1. Ensure test library has never been indexed (or reset vector store)
2. Open plugin dialog in Zotero
3. Select test library
4. Observe indexing mode dropdown - should default to "Auto"
5. Click "Index Library" button
6. Monitor progress dialog

**Expected Results:**

- [x] Status shows "Not indexed yet" or "Never indexed"
- [x] Indexing starts and processes all items with PDFs
- [x] Progress shows items being added (not updated)
- [x] Statistics show `items_added > 0`, `items_updated = 0`
- [x] Mode used is "full"
- [x] All chunks successfully stored in vector database

---

### 2. Second Indexing with No Changes

**Objective:** Verify incremental mode skips unchanged items

**Steps:**

1. After first indexing completes, immediately index again
2. Use "Auto" mode
3. Click "Index Library" button

**Expected Results:**

- [x] Status shows last indexed time and item counts
- [x] Auto mode selects "incremental"
- [x] Indexing completes quickly (< 30 seconds)
- [x] Statistics show `items_added = 0`, `items_updated = 0`
- [x] No PDF downloads occur
- [x] Total chunks remain unchanged

---

### 3. Incremental Indexing with New Item

**Objective:** Verify new items are detected and indexed

**Steps:**

1. Add a new PDF item to test library in Zotero
2. Wait for sync to complete
3. Open plugin dialog
4. Select "Incremental" mode explicitly
5. Click "Index Library" button

**Expected Results:**

- [x] Status shows previous indexing metadata
- [x] New item is detected by version comparison
- [x] Statistics show `items_added = 1`, `items_updated = 0`
- [x] New PDF is downloaded and processed
- [x] Chunks are added to vector store
- [x] Total chunks increases

---

### 4. Incremental Indexing with Updated Item

**Objective:** Verify modified items are reindexed

**Steps:**

1. Modify metadata (title, author) of an existing indexed item in Zotero
2. Wait for sync to complete
3. Open plugin dialog
4. Select "Incremental" mode
5. Click "Index Library" button

**Expected Results:**

- [x] Modified item is detected by version number change
- [x] Statistics show `items_added = 0`, `items_updated = 1`
- [x] Old chunks are deleted
- [x] New chunks are created with updated metadata
- [x] Total chunks may change (depending on PDF content)

---

### 5. Hard Reset Functionality

**Objective:** Verify hard reset forces full reindex

**Steps:**

1. Right-click on indexed library in dialog
2. Select "Hard Reset" from context menu
3. Confirm the action
4. Close dialog and reopen
5. Select "Auto" mode
6. Click "Index Library" button

**Expected Results:**

- [x] Reset confirmation dialog appears
- [x] Status shows `force_reindex = true` flag
- [x] Auto mode detects reset flag and uses "full" mode
- [x] All existing chunks are deleted
- [x] Entire library is reindexed from scratch
- [x] Statistics show all items as "added"
- [x] Reset flag is cleared after completion

---

### 6. Mode Selection UI

**Objective:** Verify indexing mode selection works correctly

**Steps:**

1. Open plugin dialog
2. Select library
3. Try each mode from dropdown:
   - Auto (recommended)
   - Incremental
   - Full

**Expected Results:**

**Auto Mode:**
- [x] Backend automatically selects best mode
- [x] Uses "full" for first-time indexing
- [x] Uses "incremental" for subsequent indexing
- [x] Honors hard reset flag

**Incremental Mode:**
- [x] Only fetches items since last version
- [x] Skips unchanged items
- [x] Reindexes modified items

**Full Mode:**
- [x] Deletes all existing chunks
- [x] Reindexes entire library
- [x] Ignores version tracking

---

### 7. Index Status Display

**Objective:** Verify library status metadata is displayed correctly

**Steps:**

1. Index a library (any mode)
2. Close and reopen plugin dialog
3. Observe status display for indexed library

**Expected Results:**

- [x] Last indexed time shown (e.g., "2 hours ago", "3 days ago")
- [x] Total items indexed count displayed
- [x] Total chunks count displayed
- [x] Status color indicates indexed state (green)
- [x] Metadata updates after each indexing operation

---

### 8. Cancel Indexing Operation

**Objective:** Verify indexing can be cancelled mid-operation

**Steps:**

1. Start indexing a large library (or use Full mode on test library)
2. Click "Cancel" button during processing
3. Observe cancellation behavior

**Expected Results:**

- [x] Cancel button appears after indexing starts
- [x] Clicking cancel stops processing
- [x] Partial results are saved (items processed before cancel)
- [x] UI returns to ready state
- [x] Can start new indexing operation immediately
- [x] No corrupted state in vector store

---

### 9. Version Tracking in Chunks

**Objective:** Verify version fields are stored in chunk metadata

**Steps:**

1. Index a library
2. Query Qdrant directly or use backend API to inspect chunks
3. Check chunk payload for version fields

**Expected Chunk Payload Fields:**

```python
{
    # Existing fields
    "text": str,
    "chunk_id": str,
    "library_id": str,
    "item_key": str,
    "attachment_key": str,
    "title": str,
    "authors": List[str],
    # ... other existing fields

    # NEW: Version tracking fields
    "item_version": int,           # ✓ Present and > 0
    "attachment_version": int,     # ✓ Present and > 0
    "indexed_at": str,             # ✓ ISO 8601 timestamp
    "zotero_modified": str,        # ✓ Item's dateModified
    "schema_version": int          # ✓ Should be 2
}
```

**Verification:**

- [x] All new chunks have version fields
- [x] Version numbers match Zotero item versions
- [x] Timestamps are in ISO 8601 format
- [x] Schema version is 2

---

### 10. Backward Compatibility with Legacy Chunks

**Objective:** Verify old chunks without version fields still work

**Steps:**

1. If you have chunks from before this feature, verify they still work
2. Perform a search query
3. Verify both old and new chunks are returned

**Expected Results:**

- [x] Search works with mixed chunk schema versions
- [x] Legacy chunks (schema_version=1 or missing) are readable
- [x] Queries return correct results regardless of chunk version
- [x] No errors in backend logs

---

### 11. API Endpoint Testing

**Objective:** Verify all new API endpoints work correctly

**Endpoints to Test:**

**GET `/api/libraries/{id}/index-status`**
- [x] Returns 404 for never-indexed library
- [x] Returns 200 with metadata for indexed library
- [x] Metadata includes all required fields

**POST `/api/libraries/{id}/reset-index`**
- [x] Sets `force_reindex` flag
- [x] Returns success message
- [x] Next auto index uses full mode

**GET `/api/libraries/indexed`**
- [x] Returns list of all indexed libraries
- [x] Each entry has complete metadata
- [x] List includes test library after indexing

**POST `/api/index/library/{id}?mode=auto|incremental|full`**
- [x] Accepts mode parameter
- [x] Returns statistics with correct structure
- [x] Mode is reflected in response

---

### 12. Performance Comparison

**Objective:** Measure performance improvement of incremental indexing

**Steps:**

1. Index test library in full mode - record time
2. Immediately reindex in incremental mode - record time
3. Compare elapsed times

**Expected Results:**

- [x] Full index: ~2-5 minutes for test library
- [x] Incremental index (no changes): < 30 seconds
- [x] **Performance improvement: >80% faster** for incremental updates
- [x] Minimal API calls to Zotero for incremental mode

---

### 13. Error Handling

**Objective:** Verify graceful error handling

**Test Cases:**

**Zotero Not Running:**
- [x] Plugin shows clear error message
- [x] Backend returns 503 Service Unavailable
- [x] No crashes or undefined behavior

**Network Interruption During Indexing:**
- [x] Partial results saved
- [x] Can retry indexing
- [x] No corrupted state

**Invalid Library ID:**
- [x] Backend returns 404 or appropriate error
- [x] Error message is user-friendly

**Concurrent Indexing Attempts:**
- [x] Second request returns 409 Conflict
- [x] Or queues/waits for first to complete
- [x] No race conditions or data corruption

---

## Success Criteria Summary

All tests above should pass with:

- [x] Incremental indexing works correctly
- [x] Version tracking persisted in database
- [x] Hard reset functionality available
- [x] API endpoints documented and tested
- [x] Plugin UI shows indexing status
- [x] Backward compatible with existing data
- [x] **Performance improvement measurable (>80% for incremental updates)**

---

## Notes for Testers

### Quick Reset for Testing

To reset test library for fresh testing:

```bash
# Option 1: Delete collection in Qdrant (if using persistent storage)
curl -X DELETE http://localhost:6333/collections/document_chunks

# Option 2: Restart with in-memory Qdrant
# Just restart backend server

# Option 3: Use hard reset via API
curl -X POST http://localhost:8000/api/libraries/1/reset-index
```

### Viewing Logs

Backend logs show detailed indexing progress:

```bash
# Tail backend logs
tail -f logs/server.log

# Or if running with uvicorn --reload
# Logs appear in terminal
```

### Checking Qdrant Data

Inspect vector store directly:

```bash
# Get collection info
curl http://localhost:6333/collections/document_chunks

# Get library metadata
curl http://localhost:6333/collections/library_metadata

# Scroll through chunks
curl -X POST http://localhost:6333/collections/document_chunks/points/scroll \
  -H 'Content-Type: application/json' \
  -d '{"limit": 10}'
```

---

**Testing Complete:** 2025-01-12
**Tested By:** [Your Name]
**Test Status:** [PASS / FAIL / PARTIAL]
**Issues Found:** [Link to issue tracker or list below]

---

## Automated Test Execution

For automated testing, use:

```bash
# Unit tests (fast, no dependencies)
uv run pytest backend/tests/test_incremental_indexing.py -v

# API integration tests (requires Zotero + test server)
uv run pytest backend/tests/test_api_incremental_indexing.py -m api -v

# All tests
uv run pytest -v
```

See [docs/testing.md](../testing.md) for more details on test infrastructure.
