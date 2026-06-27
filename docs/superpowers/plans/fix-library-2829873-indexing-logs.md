# Log Excerpts: Library 2829873 Indexing Failure

Supporting evidence for `fix-library-2829873-indexing.md`.  
All timestamps CEST (UTC+2) unless noted. Log lines are verbatim from the production server.

---

## 1. Full scan completion (cron_indexer.log, Jun 26 18:58)

The June 25 15:00 full scan ran for ~28 hours and completed successfully:

```
2026-06-26 18:58:44,039 INFO [backend.services.document_processor] Indexing complete: library=2829873 mode=full items=6010 added=6010 updated=0 chunks=215399 elapsed=100709.1s
2026-06-26 18:58:44,041 INFO [cron_indexer] Finished groups/2829873: 6010 items, 215399 chunks added
```

Metadata written: `total_items_indexed=6010, last_full_scan_indexable=6010, last_indexed_version=46693`

---

## 2. Incremental runs stuck at version 46693 (cron_indexer.log, Jun 27 00:00)

Every hourly cron run from 19:00 Jun 26 through 08:00 Jun 27 showed the same pattern —
3 items fetched since v46693, 0 indexable, version never advances:

```
2026-06-27 00:00:09,681 INFO [backend.zotero.web_api] Fetching items since version 46693
2026-06-27 00:00:10,032 INFO [backend.zotero.web_api] Retrieved 3 items from library 2829873
2026-06-27 00:00:10,769 INFO [backend.services.document_processor] Indexing complete: library=2829873 mode=incremental items=0 added=0 updated=0 chunks=0 elapsed=1.1s
2026-06-27 00:00:10,770 INFO [cron_indexer] Finished groups/2829873: 0 items, 0 chunks added
```

This repeats identically for 01:00, 02:00, 03:00, 04:00, 05:00, 06:00, 07:00, 08:00.

---

## 3. CRITICAL: check-indexed returns "not_indexed" for all items 11h after full scan (server.log, Jun 27 06:14)

The plugin sent batches of 25 items to `check-indexed` for library 2829873.  
At 06:14 — **11 hours after** the full scan stored 215,399 chunks — virtually all items were
reported as `not_indexed`. Only items being processed by concurrent async uploads showed as
`up_to_date`, and only after their upload completed:

```
2026-06-27 06:14:01,241 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=25
2026-06-27 06:14:01,274 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=25
2026-06-27 06:14:05,059 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=23 up_to_date=2
2026-06-27 06:14:17,124 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=18 up_to_date=7
2026-06-27 06:14:18,015 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=22 up_to_date=3
2026-06-27 06:14:45,325 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=11 up_to_date=14
2026-06-27 06:14:45,504 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=25
2026-06-27 06:14:45,542 - backend.api.document_upload - INFO - [DIAG] check-indexed reasons: library=2829873 not_indexed=25
```

**Interpretation**: The `up_to_date` count increases within the session (2 → 7 → 14) because
async uploads were completing concurrently. Items uploaded via the async path became findable.
Items supposedly indexed by the full cron scan remained permanently `not_indexed`. This means
the 215,399 chunks from the full scan were **not findable by item_key** — even though they were
findable by `library_id` filter (the DELETE later confirmed 217,318 existed).

This is the **primary evidence** that chunks from the full scan were stored with item_key values
that do not match the parent item keys the plugin checks.

---

## 4. KISSKI key first fails on async upload (server.log, Jun 27 06:54–06:55)

```
2026-06-27 06:54:51,516 - backend.api.document_upload - INFO - Async upload request: library=2829873 user=39226 item=9L5IIVSI attachment=IYB3QM2V mime=application/pdf size=411.03 KB
2026-06-27 06:54:54,272 - backend.services.document_processor - INFO - [TIMING] IYB3QM2V: extraction=2.7s chunks=7 size_bytes=420896
2026-06-27 06:54:54,272 - backend.services.embeddings - INFO - [TIMING] embed_batch: 7 texts → 1 API call(s) (batch_size=256, model=multilingual-e5-large-instruct)
2026-06-27 06:54:54,328 - backend.api.document_upload - ERROR - Error processing upload for IYB3QM2V: Error code: 401 - {'message': 'Unauthorized', 'request_id': '03b11959c626da045da96822226c446a'}
```

All subsequent async upload embedding calls fail with 401 through 07:53.

---

## 5. Reconcile-count call (server.log, Jun 27 08:20)

Called ~27 minutes after the last upload 401, during the diagnostic session:

```
2026-06-27 08:20:18,889 - backend.api.libraries - INFO - Reconciled total_items_indexed for library=2829873: 195
```

`count_indexed_items("2829873")` returned **195** unique item_key values. At this moment,
217,318 chunks existed with `library_id="2829873"` (confirmed by DELETE at 09:00:15).
The 195 likely correspond to the items successfully indexed by async uploads during the
06:14 session (before KISSKI expired at 06:54). The 215,399 full-scan chunks existed but
had non-matching item_key values, so they were not counted.

---

## 6. Cascade: under-indexed detection → wipe → new scan (cron_indexer.log, Jun 27 09:00)

```
2026-06-27 09:00:10,910 WARNING [cron_indexer] Library groups/2829873 under-indexed: 195/24127 items (1% < 25%); forcing full re-index
2026-06-27 09:00:10,911 INFO [backend.services.document_processor] Starting indexing for library 2829873 (mode=full)
2026-06-27 09:00:10,914 INFO [backend.services.document_processor] Full reindex for library 2829873
2026-06-27 09:00:15,117 INFO [httpx] HTTP Request: POST http://qdrant:6333/collections/document_chunks/points/delete?wait=true "HTTP/1.1 200 OK"
2026-06-27 09:00:15,117 INFO [backend.db.vector_store] Deleted 217318 chunks for library 2829873
2026-06-27 09:00:17,000 INFO [backend.db.vector_store] Deleted 241 deduplication records for library 2829873
2026-06-27 09:12:24,975 INFO [backend.zotero.web_api] Retrieved 24127 items from library 2829873
```

`_resolve_mode` computed: `indexed=195`, `zotero_total=24127`, `ratio=1% < 25%`.
Scan-floor check: `195 >= 6010 * 0.9 = 5409` → **false** → protection did not engage.

The DELETE confirms: those 217,318 chunks (including the 215,399 from the full scan)
all had `library_id = "2829873"` — yet `count_indexed_items` only found 195 unique item_keys.

---

## Key Contradiction Summary

| Fact | Source |
|------|--------|
| Full scan stored 215,399 chunks for library 2829873 at Jun 26 18:58 | cron_indexer.log |
| At Jun 27 06:14 (11h later), items were `not_indexed` via check-indexed | server.log |
| At Jun 27 08:20, count_indexed_items("2829873") returned 195 unique item_keys | server.log |
| At Jun 27 09:00:15, delete_library_chunks("2829873") deleted 217,318 chunks | cron_indexer.log |

**Conclusion**: The 215,399+ chunks stored by the full scan had `library_id = "2829873"` but
**item_key values that do not match parent Zotero item keys**. Chunks from async uploads (195 items)
had correct item_keys. The check-indexed and count_indexed_items functions both look up items by
`(library_id, item_key)` — so the full-scan chunks were invisible to both, while delete (which
filters only on library_id) found them all.

The most likely cause: the container code (pre-`4dcc438`, built Jun 24) stored chunks from the
cross-library copy path with `item_key` set to the **source** item key (from u39226) rather than
the **target** item key (for 2829873). Or: `store_item_chunks` in that code version omitted
`item_key` from the payload. Check commit `da7eebf` (the vector_store version in the container).
