# Surface Server-Side Download Failures to the Fix Unavailable Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a server-side full sync can't download an attachment from Zotero's cloud storage, capture which item/attachment failed and expose it to the plugin so the "Fix Unavailable" tool can attempt to recover it — without touching the separate, genuinely-unfixable "parse error" (binary data Kreuzberg can't read) category.

**Architecture:** Backend: `DocumentProcessor` gains an instance-level accumulator that `_index_item` appends `(item_key, attachment_key)` to whenever `get_attachment_file` returns nothing; full sync persists up to 100 of these into a new `LibraryIndexMetadata.last_full_scan_failed_downloads` field (already exposed for free via the existing `GET /api/libraries/{id}/index-status` endpoint, since it just returns the whole model). Plugin: `dialog.js` fetches that field alongside the rest of the library metadata it already polls, and merges the attachment keys into a new per-library JSON store in `zotero-rag.js` (parallel to the existing `parse-errors-*.json` / `skipped-server-*.json` stores) — but critically, these merged items get **no** `isParseError`/`skipReason` flag, so they fall into the Fix dialog's "imported file" retry bucket (Zotero sync download + cross-library search) rather than the "not indexable, delete or reindex" bucket parse errors and empty/timeout skips use. `fix-unavailable.js` gets a new "srv fail" type label for these rows so the user can tell them apart from a generic missing file.

**Tech Stack:** Python 3.12 / `unittest` + `pytest` (backend); plain JavaScript + Node's built-in `node:test` runner + `node:vm` sandboxing (plugin, same pattern as the existing `plugin/test/remote_indexer.test.js`).

## Global Constraints

- Run backend tests with `uv run pytest` (never bare `pytest`/`python`), per CLAUDE.md.
- Run plugin tests with `node --test "plugin/test/*.test.js"`.
- **Parse errors are explicitly out of scope.** Kreuzberg rejecting a file's content (corrupted PDF, wrong encoding, OCR failure) will recur regardless of how the bytes are obtained, so there is no client-side remedy — don't wire `parseErrors`/`skippedEmpty`/`skippedTimeout` into anything new here. Only "could not download attachment" failures are in scope.
- Every new function/method keeps the existing type hint (Python) / JSDoc (`@ts-check`) conventions already present in its file.
- Comments explain "why", not "what" (CLAUDE.md).
- Commit after each task, not once at the end.
- Do not deploy to production as part of this plan — that's a separate, explicit follow-up per CLAUDE.md's hotfix workflow.

---

## Background

A prior investigation found that a full re-index of "Legal Theory Knowledge Graph" (`groups/2829873`) reported 15 `items_failed`, splitting into two genuinely different categories by grepping the run's log:

- **6 "Could not download attachment X"** — the server failed to fetch the attachment's bytes from Zotero's cloud storage (dead link, transient error, etc.).
- **9 "Skipping attachment X (parse error — binary data): kreuzberg sidecar returned HTTP 422..."** — Kreuzberg rejected the file's actual content.

The plugin's "Fix Unavailable" dialog (`plugin/src/fix-unavailable.js`, driven by `plugin/src/zotero-rag.js`'s `_getUnavailableAttachments`) already has a working, three-tier candidate list:

1. A live SQL query against the *local* Zotero database for attachments with no local file (`zotero-rag.js:2153-2213`, `_getUnavailableAttachments`) — these get the real remedy: Zotero sync re-download, then cross-library MD5/filename/`owl:sameAs`/URL/DOI search (`fix-unavailable.js:397-484`, `searchAndFix`).
2. Parse-error keys, persisted via `storeParseErrorItems`/`_getParseErrorAttachments` (`zotero-rag.js:1960-1983`, `2088-2146`) — marked `isParseError: true`.
3. Server-skip keys (`skipped_empty`/`skipped_timeout`), persisted via `storeSkippedServerItems`/`_getSkippedServerAttachments` (`zotero-rag.js:2000-2086`) — marked `skipReason`.

Critically, `searchAndFix()`'s bucketing (`fix-unavailable.js:403-406`) treats **any** item with `isParseError` or `skipReason` set as **not retriable** — it's shown as "Not indexable — delete or reindex after upgrade" and never gets the actual download/search attempt. That's correct for parse errors and empty/timeout skips (retrying won't change Kreuzberg's answer), but wrong for download failures — those genuinely might succeed if retried (the local Zotero client may already have the file, or `owl:sameAs`/URL/DOI search might find a copy), exactly like tier-1 items do.

So the design here is deliberately **not** "add a third `skipReason` value" — that would put download failures in the wrong, no-retry bucket. Instead, a new tier is added that behaves like tier 1 (no `isParseError`, no `skipReason`) but is sourced from the server's report rather than a local DB scan, with only a new *display* flag (`serverDownloadFailed`) so the row still shows something more informative than a generic file-type guess.

Neither the server-reported "could not download" list, nor its client-side counterpart, exists anywhere yet — this plan adds both.

---

## File Structure

| File | Change |
|---|---|
| `backend/services/document_processor.py` | New instance attribute `self._download_failures`, appended to in `_index_item` (Task 1); reset + persisted at the end of `_index_library_full` (Task 2); threaded through `_subprocess_index_batch` (Task 3). |
| `backend/models/library.py` | New field `LibraryIndexMetadata.last_full_scan_failed_downloads` (Task 2). |
| `backend/tests/test_document_processor.py` | Tests for the capture (Task 1), the full-sync epilogue (Task 2), and the subprocess worker (Task 3). |
| `plugin/src/zotero-rag.js` | New `_downloadFailedFilePath`/`storeDownloadFailedItems`/`_getDownloadFailedAttachments`, merged into `_getUnavailableAttachments`; `UnavailableAttachmentInfo` typedef gains `serverDownloadFailed` (Task 4). |
| `plugin/test/zotero-rag.test.js` | **New file.** Tests for Task 4's three new functions and the dedup-on-merge behavior. |
| `plugin/src/fix-unavailable.js` | New `_typeLabelFor(info)` helper (extracted from the inline ternary in `getRowData`) with a `'srv fail'` branch (Task 5). |
| `plugin/test/fix-unavailable.test.js` | **New file.** Tests for `_typeLabelFor`'s branch priority. |
| `plugin/src/dialog.js` | New `mergeDownloadFailures(libraryId, metadata)` method, called from `fetchAndUpdateLibraryMetadata`; `LibraryIndexMetadata` JSDoc typedef gains `last_full_scan_failed_downloads` (Task 6). |
| `plugin/test/dialog.test.js` | **New file.** Tests for `mergeDownloadFailures`. |

---

## Task 1: Capture download failures as they happen

**Files:**
- Modify: `backend/services/document_processor.py:268` (end of `DocumentProcessor.__init__`)
- Modify: `backend/services/document_processor.py:930-932` (`_index_item`'s download-failure branch)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Produces: `DocumentProcessor._download_failures: list[dict]`, each entry `{"item_key": str, "attachment_key": str}`. Task 2 reads and resets this list.

- [ ] **Step 1: Write the failing test**

Add to `TestDocumentProcessor` in `backend/tests/test_document_processor.py`, placed after `test_index_library_pdf_download_failure` (currently ending at line 805):

```python
    async def test_index_item_records_download_failure(self):
        """_index_item must append a record to self._download_failures when an
        attachment can't be downloaded, so full sync can later surface these to
        the plugin as potentially-fixable (unlike parse errors, which recur
        regardless of how the bytes are obtained and are never recorded here)."""
        mock_item = {
            "version": 1,
            "data": {"key": "ITEM123", "itemType": "journalArticle", "title": "Test Paper"},
        }
        mock_pdf_attachment = {
            "data": {"key": "PDF123", "itemType": "attachment", "contentType": "application/pdf"},
        }
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = None  # Download failed

        await self.processor._index_item(mock_item, "test_lib", "user")

        self.assertEqual(self.processor._download_failures, [
            {"item_key": "ITEM123", "attachment_key": "PDF123"},
        ])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_index_item_records_download_failure -v`
Expected: FAIL with `AttributeError: 'DocumentProcessor' object has no attribute '_download_failures'`

- [ ] **Step 3: Write minimal implementation**

In `backend/services/document_processor.py`, at the end of `__init__` (currently):

```python
        self.document_extractor = document_extractor

        logger.debug("Initialized DocumentProcessor")
```

change to:

```python
        self.document_extractor = document_extractor

        # Attachments _index_item couldn't download this run — see _index_library_full,
        # which persists up to 100 of these so the plugin's Fix Unavailable tool can
        # attempt to recover them (unlike parse errors, these may be fixable client-side).
        self._download_failures: list[dict] = []

        logger.debug("Initialized DocumentProcessor")
```

Then, in `_index_item`, change (currently at line 930-932):

```python
                if not file_bytes:
                    logger.warning(f"Could not download attachment {attachment_key}")
                    continue
```

to:

```python
                if not file_bytes:
                    logger.warning(f"Could not download attachment {attachment_key}")
                    self._download_failures.append({"item_key": item_key, "attachment_key": attachment_key})
                    continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_index_item_records_download_failure -v`
Expected: PASS

- [ ] **Step 5: Run the full document-processor suite to confirm no regression**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests PASS (a new instance attribute that nothing else reads yet can't break existing behavior)

- [ ] **Step 6: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: record per-attachment download failures on DocumentProcessor"
```

---

## Task 2: Persist download failures on `LibraryIndexMetadata` (full-sync inline path)

**Files:**
- Modify: `backend/models/library.py` (new field, after `last_full_scan_items_failed`)
- Modify: `backend/services/document_processor.py:558` (reset at the start of `_index_library_full`)
- Modify: `backend/services/document_processor.py:846` (persist at the epilogue of `_index_library_full`)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Consumes: `self._download_failures` (Task 1).
- Produces: `LibraryIndexMetadata.last_full_scan_failed_downloads: list[dict]`. Task 3 also writes into this via an aggregated list from subprocess batches. Task 4 (plugin) reads it from the `GET /api/libraries/{id}/index-status` JSON response — no backend API code changes needed there, since the endpoint already returns the whole `LibraryIndexMetadata` model (`backend/api/libraries.py:138`, `response_model=LibraryIndexMetadata`).

- [ ] **Step 1: Write the failing test**

Add to `TestDocumentProcessor` in `backend/tests/test_document_processor.py`, placed right after `test_index_item_records_download_failure` (added in Task 1):

```python
    async def test_full_sync_persists_download_failures_on_metadata(self):
        """Full sync must persist up to 100 download-failure records onto
        LibraryIndexMetadata.last_full_scan_failed_downloads, capped, and reset
        cleanly between runs."""
        mock_item = {
            "version": 1,
            "data": {"key": "ITEM123", "itemType": "journalArticle", "title": "Test Paper"},
        }
        mock_pdf_attachment = {
            "data": {"key": "PDF123", "itemType": "attachment", "contentType": "application/pdf"},
            "version": 1,
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = None  # Download failed

        result = await self.processor.index_library("test_lib")

        self.assertIn("mode", result)
        saved_metadata = self.mock_vector_store.update_library_metadata.call_args.args[0]
        self.assertEqual(saved_metadata.last_full_scan_failed_downloads, [
            {"item_key": "ITEM123", "attachment_key": "PDF123"},
        ])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_full_sync_persists_download_failures_on_metadata -v`
Expected: FAIL with `AttributeError` or `pydantic.ValidationError`-style error — `LibraryIndexMetadata` has no `last_full_scan_failed_downloads` attribute yet

- [ ] **Step 3: Write minimal implementation**

In `backend/models/library.py`, add this field directly after `last_full_scan_items_failed` (which ends at line 73, just before `schema_version` at line 75):

```python
    last_full_scan_items_failed: int = Field(
        default=0,
        description=(
            "Items that failed per-item processing (e.g. dead attachment download "
            "link, extraction error) in the last completed full scan. Only a full "
            "scan re-examines every candidate, so incremental syncs never update "
            "this — it stays the authoritative floor of currently un-indexable "
            "items until the next full scan."
        )
    )

    last_full_scan_failed_downloads: list[dict] = Field(
        default_factory=list,
        description=(
            "Up to 100 {item_key, attachment_key} pairs whose attachment could not "
            "be downloaded from Zotero in the last completed full scan. Unlike a "
            "generic parse error (which recurs regardless of how the bytes are "
            "obtained), these may be fixable client-side — e.g. the file exists on "
            "the user's Zotero desktop even though the server's cloud-storage fetch "
            "failed. Surfaced to the plugin's Fix Unavailable tool. Only a full "
            "scan updates this; incremental syncs leave it stale."
        )
    )

    schema_version: int = Field(default=1)
```

In `backend/services/document_processor.py`, in `_index_library_full`, change the start of the method (currently):

```python
        logger.info(f"Full sync for library {library_id}")

        # Fetch all items from Zotero
```

to:

```python
        logger.info(f"Full sync for library {library_id}")
        # Reset per-run: this list is instance-level so _index_item can append to it
        # without threading a new parameter through every one of its callers.
        self._download_failures = []

        # Fetch all items from Zotero
```

Then, in the epilogue, change (currently):

```python
        metadata.last_full_scan_items_failed = items_failed

        if items_failed:
```

to:

```python
        metadata.last_full_scan_items_failed = items_failed
        # Cap at 100 to keep the metadata payload small — these are surfaced to the
        # plugin's Fix Unavailable tool as potentially fixable. (Task 3 extends this
        # line to also include failures from subprocess-dispatched batches, which
        # this instance's own _download_failures can't see — a production run always
        # goes through that path, so this task alone only covers the inline path
        # settings.testing=True uses, i.e. what the test below exercises.)
        metadata.last_full_scan_failed_downloads = self._download_failures[:100]

        if items_failed:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_full_sync_persists_download_failures_on_metadata -v`
Expected: PASS

- [ ] **Step 5: Run the full document-processor suite to confirm no regression**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/models/library.py backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: persist download-failure records on LibraryIndexMetadata (full sync inline path)"
```

---

## Task 3: Thread download failures through the subprocess batch worker (the path production actually runs)

**Files:**
- Modify: `backend/services/document_processor.py:126-170` (`_subprocess_index_batch`'s inner `_run()` and its return dict)
- Modify: `backend/services/document_processor.py:731-747` (the subprocess-dispatch loop inside `_index_library_full`)
- Modify: `backend/services/document_processor.py:846` (the epilogue line from Task 2, now using the aggregated list)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Consumes: `DocumentProcessor._download_failures` (Task 1, populated on the fresh `processor` instance each batch worker constructs).
- Produces: `_subprocess_index_batch`'s returned dict gains a `"failed_downloads": list[dict]` key. `_index_library_full`'s subprocess-dispatch branch accumulates these into a new local `subprocess_download_failures: list[dict]`, referenced by Task 2's epilogue line.

- [ ] **Step 1: Write the failing test**

Add to `TestSubprocessIndexBatchFunction` in `backend/tests/test_document_processor.py`, placed after `test_reports_items_failed_for_zero_chunk_result` (before the `if __name__ == "__main__":` block):

```python
    def test_reports_failed_downloads_in_result(self):
        """_subprocess_index_batch must return the batch's own DocumentProcessor
        instance's _download_failures so the parent process can aggregate them
        across all batches of a full sync."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        item = {"version": 1, "data": {"key": "DEADLINK", "itemType": "journalArticle", "title": "A"}}
        mock_vector_store = MagicMock()
        mock_vector_store.get_item_version.return_value = None  # no existing content either

        def fake_index_item(self, item, library_id, library_type):
            # Simulates what the real _index_item does on a download failure
            # (Task 1) — append to the instance's own accumulator and return 0.
            self._download_failures.append({"item_key": "DEADLINK", "attachment_key": "ATT1"})
            return 0

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=mock_vector_store), \
             patch.object(dp_module.DocumentProcessor, "_index_item", side_effect=fake_index_item, autospec=True):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None,
                testing=False,
                extractor_backend="kreuzberg",
                ocr_enabled=True,
                kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[item],
                library_id="test_lib",
                library_type="user",
                indexed_versions={},
                zotero_api_key="fake-key",
                embedding_api_key="fake-embed-key",
            )

        self.assertEqual(result["failed_downloads"], [
            {"item_key": "DEADLINK", "attachment_key": "ATT1"},
        ])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_reports_failed_downloads_in_result -v`
Expected: FAIL with `KeyError: 'failed_downloads'`

- [ ] **Step 3: Write minimal implementation**

In `backend/services/document_processor.py`, in `_subprocess_index_batch`'s inner `_run()`, change the returned dict (currently at line 164-170):

```python
        return {
            "chunks_added": chunks_added,
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "items_failed": items_failed,
        }
```

to:

```python
        return {
            "chunks_added": chunks_added,
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "items_failed": items_failed,
            "failed_downloads": processor._download_failures[:100],
        }
```

Then, in `_index_library_full`, add a new accumulator alongside the other counters (currently at line 690-694):

```python
        # Index items: skip unchanged, update outdated, add new
        chunks_added = 0
        items_added = 0
        items_updated = 0
        items_skipped = 0
        items_failed = 0
        total_items = len(items_with_attachments)
```

to:

```python
        # Index items: skip unchanged, update outdated, add new
        chunks_added = 0
        items_added = 0
        items_updated = 0
        items_skipped = 0
        items_failed = 0
        # Aggregated across subprocess batches (each batch's own DocumentProcessor
        # instance's _download_failures never reaches this process directly).
        subprocess_download_failures: list[dict] = []
        total_items = len(items_with_attachments)
```

Then, in the subprocess-dispatch loop, change (currently at line 743-747):

```python
                    chunks_added += result.get("chunks_added", 0)
                    items_added += result.get("items_added", 0)
                    items_updated += result.get("items_updated", 0)
                    items_skipped += result.get("items_skipped", 0)
                    items_failed += result.get("items_failed", 0)
```

to:

```python
                    chunks_added += result.get("chunks_added", 0)
                    items_added += result.get("items_added", 0)
                    items_updated += result.get("items_updated", 0)
                    items_skipped += result.get("items_skipped", 0)
                    items_failed += result.get("items_failed", 0)
                    subprocess_download_failures.extend(result.get("failed_downloads", []))
```

Finally, replace Task 2's temporary epilogue line:

```python
        metadata.last_full_scan_failed_downloads = self._download_failures[:100]
```

with:

```python
        metadata.last_full_scan_failed_downloads = (self._download_failures + subprocess_download_failures)[:100]
```

(One of the two lists is always empty for a given run — `use_subprocess` is a single boolean deciding which branch processes every item — so concatenating both unconditionally is safe and needs no extra branching.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_reports_failed_downloads_in_result -v`
Expected: PASS

- [ ] **Step 5: Run the full document-processor suite to confirm no regression**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: aggregate download failures across subprocess batches"
```

---

## Task 4: Plugin — persist and load server-reported download failures

**Files:**
- Modify: `plugin/src/zotero-rag.js` (new functions, placed after `_getParseErrorAttachments`, which currently ends at line 2146, and before `_getUnavailableAttachments` at line 2153; typedef extension at line 1932-1942)
- Modify: `plugin/src/zotero-rag.js:2196-2212` (`_getUnavailableAttachments`'s merge section)
- Test: `plugin/test/zotero-rag.test.js` (new file)

**Interfaces:**
- Produces: `ZoteroRAGPlugin.storeDownloadFailedItems(backendLibraryId: string, newKeys: string[]) -> Promise<number>` (returns count of newly-added keys). `ZoteroRAGPlugin._getDownloadFailedAttachments(libraryID: number) -> Promise<Array<UnavailableAttachmentInfo>>`, each entry with `serverDownloadFailed: true` and neither `isParseError` nor `skipReason` set. Task 5 reads `serverDownloadFailed` off `UnavailableAttachmentInfo`. Task 6 calls `storeDownloadFailedItems`.

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/zotero-rag.test.js`:

```js
// Tests for plugin/src/zotero-rag.js's download-failure storage and merge logic.
//
// zotero-rag.js defines `class ZoteroRAGPlugin` and instantiates a singleton at
// the bottom (`ZoteroRAG = new ZoteroRAGPlugin();`). The constructor does no
// Zotero-global work, so — same technique as plugin/test/remote_indexer.test.js —
// we evaluate the source inside a vm context with a stubbed `Zotero`/`IOUtils`/
// `PathUtils`, then construct a fresh instance per test from the context's
// `ZoteroRAGPlugin` class (not the singleton, so each test starts clean).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'zotero-rag.js');

/**
 * Build a minimal Zotero/IOUtils/PathUtils stub backed by an in-memory fake
 * filesystem (a plain object keyed by path) and a map of library attachments.
 * @param {Record<string, any>} attachmentsByKey - key -> fake attachment object
 * @returns {{ zotero: any, ioUtils: any, pathUtils: any, files: Record<string, string> }}
 */
function makeStubs(attachmentsByKey = {}) {
	const files = {};
	const ioUtils = {
		async readUTF8(filePath) {
			if (!(filePath in files)) throw new Error('ENOENT');
			return files[filePath];
		},
		async writeUTF8(filePath, text) { files[filePath] = text; },
		async makeDirectory() {},
	};
	const pathUtils = { join: (...parts) => parts.join('/') };
	const zotero = {
		DataDirectory: { dir: '/fake/zotero/data' },
		Items: {
			async getByLibraryAndKeyAsync(_libraryID, key) {
				return attachmentsByKey[key] || null;
			},
			async getAsync(id) {
				return attachmentsByKey[`__parent_${id}`] || null;
			},
		},
	};
	return { zotero, ioUtils, pathUtils, files };
}

/**
 * Load a fresh ZoteroRAGPlugin instance into a vm context with the given stubs.
 * @param {any} zoteroStub
 * @param {any} ioUtilsStub
 * @param {any} pathUtilsStub
 * @returns {any} a new ZoteroRAGPlugin instance
 */
function loadPlugin(zoteroStub, ioUtilsStub, pathUtilsStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, IOUtils: ioUtilsStub, PathUtils: pathUtilsStub, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'zotero-rag.js' });
	return new context.ZoteroRAGPlugin();
}

test('storeDownloadFailedItems merges keys and returns the count of new ones', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	const firstAdded = await plugin.storeDownloadFailedItems('u1', ['ATT1', 'ATT2']);
	assert.strictEqual(firstAdded, 2);

	const secondAdded = await plugin.storeDownloadFailedItems('u1', ['ATT2', 'ATT3']);
	assert.strictEqual(secondAdded, 1); // ATT2 already stored, only ATT3 is new
});

test('_getDownloadFailedAttachments resolves stored keys with serverDownloadFailed set, no skipReason/isParseError', async () => {
	const fakeAttachment = {
		deleted: false,
		parentItemID: null,
		key: 'ATT1',
		getCreators: () => [{ lastName: 'Doe' }],
		getField: (f) => (f === 'title' ? 'A Paper' : f === 'date' ? '2020' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ ATT1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeDownloadFailedItems('u1', ['ATT1']);
	const results = await plugin._getDownloadFailedAttachments(1);

	assert.strictEqual(results.length, 1);
	assert.strictEqual(results[0].serverDownloadFailed, true);
	assert.strictEqual(results[0].isParseError, undefined);
	assert.strictEqual(results[0].skipReason, undefined);
	assert.strictEqual(results[0].authors, 'Doe');
	assert.strictEqual(results[0].year, '2020');
	assert.strictEqual(results[0].title, 'A Paper');
});

test('_getDownloadFailedAttachments drops keys whose Zotero item no longer exists', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs({}); // ATT1 resolves to null
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeDownloadFailedItems('u1', ['ATT1']);
	const results = await plugin._getDownloadFailedAttachments(1);

	assert.deepStrictEqual(results, []);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test "plugin/test/*.test.js"`
Expected: FAIL with `TypeError: plugin.storeDownloadFailedItems is not a function` (3 new failures; the 3 pre-existing `remote_indexer.test.js` tests still pass)

- [ ] **Step 3: Write minimal implementation**

In `plugin/src/zotero-rag.js`, extend the `UnavailableAttachmentInfo` typedef (currently at lines 1931-1942):

```js
	/**
	 * @typedef {Object} UnavailableAttachmentInfo
	 * @property {*} parentItem - Parent Zotero item
	 * @property {*} attachmentItem - Attachment Zotero item
	 * @property {string} authors - Comma-separated author last names
	 * @property {string} year - Publication year (4 digits) or empty string
	 * @property {string} title - Item title
	 * @property {string} zoteroID - Parent item key
	 * @property {boolean} isLinked - True for linked files (linkMode=2); can't be auto-downloaded
	 * @property {boolean} [isParseError] - True when the file exists but kreuzberg cannot parse it (binary data)
	 * @property {'no text'|'timeout'} [skipReason] - Set for items skipped by the server (skipped_empty / skipped_timeout)
	 */
```

to:

```js
	/**
	 * @typedef {Object} UnavailableAttachmentInfo
	 * @property {*} parentItem - Parent Zotero item
	 * @property {*} attachmentItem - Attachment Zotero item
	 * @property {string} authors - Comma-separated author last names
	 * @property {string} year - Publication year (4 digits) or empty string
	 * @property {string} title - Item title
	 * @property {string} zoteroID - Parent item key
	 * @property {boolean} isLinked - True for linked files (linkMode=2); can't be auto-downloaded
	 * @property {boolean} [isParseError] - True when the file exists but kreuzberg cannot parse it (binary data)
	 * @property {'no text'|'timeout'} [skipReason] - Set for items skipped by the server (skipped_empty / skipped_timeout)
	 * @property {boolean} [serverDownloadFailed] - True when the server couldn't download this attachment
	 *   from Zotero (dead link, transient error). Deliberately does NOT set isParseError or skipReason —
	 *   unlike those, this may be fixable by retrying (the local file may already exist, or search/download
	 *   may find a copy), so it must fall into the "imported"/"linked" retry buckets in fix-unavailable.js,
	 *   not the "not indexable" one.
	 */
```

Then, directly after `_getParseErrorAttachments` (which currently ends at line 2146, right before the JSDoc comment for `_getUnavailableAttachments` at line 2148), add:

```js
	/**
	 * @param {number} zoteroLibraryID
	 * @returns {string}
	 */
	_downloadFailedFilePath(zoteroLibraryID) {
		// @ts-ignore
		return PathUtils.join(Zotero.DataDirectory.dir, 'zotero-rag', `download-failed-${zoteroLibraryID}.json`);
	}

	/**
	 * Merge new server-reported download-failure attachment keys into the
	 * persistent per-library store. Unlike parse errors, these may be fixable
	 * client-side — the server failed to fetch the file from Zotero's cloud
	 * storage, but the local Zotero client may already have it, or the Fix
	 * Unavailable tool's search/download strategies may recover it.
	 * @param {string} backendLibraryId - Backend library ID
	 * @param {string[]} newKeys - Attachment keys the server could not download
	 * @returns {Promise<number>} Number of keys newly added (not already stored)
	 */
	async storeDownloadFailedItems(backendLibraryId, newKeys) {
		if (!newKeys || newKeys.length === 0) return 0;
		const zoteroLibraryID = this._resolveZoteroLibraryID(backendLibraryId);
		if (!zoteroLibraryID) return 0;
		const filePath = this._downloadFailedFilePath(zoteroLibraryID);
		/** @type {string[]} */
		let existing = [];
		try {
			// @ts-ignore
			const text = await IOUtils.readUTF8(filePath);
			existing = JSON.parse(text);
		} catch (_) {}
		const added = newKeys.filter(k => !existing.includes(k));
		const merged = [...new Set([...existing, ...newKeys])];
		try {
			// @ts-ignore
			const dir = PathUtils.join(Zotero.DataDirectory.dir, 'zotero-rag');
			// @ts-ignore
			await IOUtils.makeDirectory(dir, { createAncestors: true, ignoreExisting: true });
			// @ts-ignore
			await IOUtils.writeUTF8(filePath, JSON.stringify(merged));
		} catch (e) {
			this.log(`[storeDownloadFailedItems] Failed to write download-failed file: ${e}`);
		}
		return added.length;
	}

	/**
	 * Load server-reported download-failure attachment keys and resolve them to
	 * UnavailableAttachmentInfo objects. Deliberately does NOT set isParseError
	 * or skipReason (see the typedef) — these items must fall into the Fix
	 * Unavailable dialog's "imported"/"linked" retry buckets, not the
	 * "not indexable" one parse errors and server-skips use.
	 * Silently drops keys where the Zotero item no longer exists.
	 * @param {number} libraryID - Zotero internal library ID
	 * @returns {Promise<Array<UnavailableAttachmentInfo>>}
	 */
	async _getDownloadFailedAttachments(libraryID) {
		const filePath = this._downloadFailedFilePath(libraryID);
		/** @type {string[]} */
		let keys = [];
		try {
			// @ts-ignore
			const text = await IOUtils.readUTF8(filePath);
			keys = JSON.parse(text);
		} catch (_) {
			return [];
		}
		/** @type {Array<UnavailableAttachmentInfo>} */
		const result = [];
		/** @type {string[]} */
		const validKeys = [];
		for (const key of keys) {
			// @ts-ignore
			const attachment = await Zotero.Items.getByLibraryAndKeyAsync(libraryID, key);
			if (!attachment || attachment.deleted) continue;
			validKeys.push(key);
			const parentItem = attachment.parentItemID
				// @ts-ignore
				? await Zotero.Items.getAsync(attachment.parentItemID)
				: null;
			const sourceItem = parentItem ?? attachment;
			const creators = sourceItem.getCreators ? sourceItem.getCreators() : [];
			const authors = creators
				.map((/** @type {any} */ c) => c.lastName || c.name || '')
				.filter((/** @type {string} */ s) => s.length > 0)
				.join(', ');
			const dateField = (sourceItem.getField ? sourceItem.getField('date') : '') || '';
			const yearMatch = dateField.match(/\b(\d{4})\b/);
			result.push({
				parentItem: parentItem ?? attachment,
				attachmentItem: attachment,
				authors,
				year: yearMatch ? yearMatch[1] : '',
				title: sourceItem.getField ? (sourceItem.getField('title') || '') : '',
				zoteroID: (parentItem ?? attachment).key,
				isLinked: false,
				serverDownloadFailed: true,
			});
		}
		if (validKeys.length !== keys.length) {
			try {
				// @ts-ignore
				await IOUtils.writeUTF8(filePath, JSON.stringify(validKeys));
			} catch (_) {}
		}
		return result;
	}

```

Finally, in `_getUnavailableAttachments`, change (currently at lines 2207-2212, the end of the method):

```js
		// Append server-skipped items (skipped_empty / skipped_timeout), deduplicating by key
		const skippedServerItems = await this._getSkippedServerAttachments(libraryID);
		for (const item of skippedServerItems) {
			if (!missingKeys.has(item.attachmentItem.key)) result.push(item);
		}
		return result;
	}
```

to:

```js
		// Append server-skipped items (skipped_empty / skipped_timeout), deduplicating by key
		const skippedServerItems = await this._getSkippedServerAttachments(libraryID);
		for (const item of skippedServerItems) {
			if (!missingKeys.has(item.attachmentItem.key)) {
				missingKeys.add(item.attachmentItem.key);
				result.push(item);
			}
		}
		// Append server-reported download failures, deduplicating by key — a file
		// missing locally may already be in `result` from the SQL-based tier above.
		const downloadFailedItems = await this._getDownloadFailedAttachments(libraryID);
		for (const item of downloadFailedItems) {
			if (!missingKeys.has(item.attachmentItem.key)) {
				missingKeys.add(item.attachmentItem.key);
				result.push(item);
			}
		}
		return result;
	}
```

(Note: the existing `skippedServerItems` loop didn't add to `missingKeys` before this change — fixed here too, since without it a download-failed item that happened to share a key with a skipped-server item could be double-counted. This is a pre-existing latent bug this task's dedup logic depends on being correct.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test "plugin/test/*.test.js"`
Expected: all tests PASS (3 new + 3 pre-existing)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat: persist and merge server-reported download failures into Fix Unavailable candidates"
```

---

## Task 5: Plugin — distinguish download failures in the Fix Unavailable table

**Files:**
- Modify: `plugin/src/fix-unavailable.js:223` (the inline type-label ternary inside `getRowData`)
- Test: `plugin/test/fix-unavailable.test.js` (new file)

**Interfaces:**
- Consumes: `UnavailableAttachmentInfo.serverDownloadFailed` (Task 4).
- Produces: `ZoteroFixUnavailableDialog._typeLabelFor(info: UnavailableAttachmentInfo) -> string`, called from `getRowData`.

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/fix-unavailable.test.js`:

```js
// Tests for plugin/src/fix-unavailable.js's row type-label logic.
//
// ZoteroFixUnavailableDialog auto-initializes at the bottom of the file
// (`ZoteroFixUnavailableDialog.init()`), but init() checks `window.arguments`
// first and returns immediately if it's missing — so a `window` stub with no
// `.arguments` property is enough to make loading the file side-effect-free.
// A `console` global must exist too, or the file's own console-shim IIFE
// would try to reference `Services`/`Cc`/`Ci`, which aren't stubbed here.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'fix-unavailable.js');

/** @returns {any} a fresh ZoteroFixUnavailableDialog object */
function loadDialog() {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { window: {}, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'fix-unavailable.js' });
	return context.ZoteroFixUnavailableDialog;
}

test('_typeLabelFor prioritizes skipReason over everything else', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ skipReason: 'no text', isParseError: true, serverDownloadFailed: true }), 'empty');
	assert.strictEqual(dialog._typeLabelFor({ skipReason: 'timeout', isParseError: true }), 'timeout');
});

test('_typeLabelFor returns "parse err" for parse errors (when no skipReason)', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ isParseError: true, serverDownloadFailed: true }), 'parse err');
});

test('_typeLabelFor returns "srv fail" for server download failures (when no skipReason/parse error)', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ serverDownloadFailed: true, isLinked: true }), 'srv fail');
});

test('_typeLabelFor returns "linked" for linked files with no failure reason', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ isLinked: true }), 'linked');
});

test('_typeLabelFor falls back to the file type label', () => {
	const dialog = loadDialog();
	dialog.getFileTypeLabel = () => 'PDF';
	assert.strictEqual(dialog._typeLabelFor({ attachmentItem: {} }), 'PDF');
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test "plugin/test/*.test.js"`
Expected: FAIL with `TypeError: dialog._typeLabelFor is not a function` (5 new failures)

- [ ] **Step 3: Write minimal implementation**

In `plugin/src/fix-unavailable.js`, add a new method directly before `getRowData` is used — insert it right after the JSDoc comment block that currently starts `_initTable()` (i.e., as a new standalone method placed just before `_initTable() {` at line 121):

```js
	/**
	 * Return a short "type" label for a row: why the attachment is
	 * unavailable/unreadable, or its file type when there's no failure reason.
	 * Priority matches searchAndFix()'s bucketing: skipReason and isParseError
	 * both mean "not indexable, no retry" and take priority for display, even
	 * though only one of them would ever be set on real data.
	 * @param {AttachmentInfo} info
	 * @returns {string}
	 */
	_typeLabelFor(info) {
		if (info.skipReason === 'no text') return 'empty';
		if (info.skipReason === 'timeout') return 'timeout';
		if (info.isParseError) return 'parse err';
		if (info.serverDownloadFailed) return 'srv fail';
		if (info.isLinked) return 'linked';
		return this.getFileTypeLabel(info.attachmentItem);
	},

```

Then, in `getRowData` (inside `_initTable()`'s `columns` config), change (currently at line 223):

```js
						type:   info.skipReason === 'no text' ? 'empty' : info.skipReason === 'timeout' ? 'timeout' : info.isParseError ? 'parse err' : (info.isLinked ? 'linked' : this.getFileTypeLabel(info.attachmentItem)),
```

to:

```js
						type:   this._typeLabelFor(info),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test "plugin/test/*.test.js"`
Expected: all tests PASS (5 new + 3 + 3 pre-existing = 11 total)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat: label server-download-failure rows distinctly in Fix Unavailable table"
```

---

## Task 6: Plugin — fetch and merge on every library metadata poll

**Files:**
- Modify: `plugin/src/dialog.js:22-37` (the `LibraryIndexMetadata` JSDoc typedef)
- Modify: `plugin/src/dialog.js:579-604` (`fetchAndUpdateLibraryMetadata`)
- Test: `plugin/test/dialog.test.js` (new file)

**Interfaces:**
- Consumes: `metadata.last_full_scan_failed_downloads` (Task 2/3, arriving over the wire via `fetchLibraryMetadata`'s existing `GET /api/libraries/{id}/index-status` call — no changes needed to that call itself). `this.plugin.storeDownloadFailedItems` (Task 4). `this.onUnavailableCountUpdated` (existing, `dialog.js:1792-1798`).
- Produces: `ZoteroRAGDialog.mergeDownloadFailures(libraryId: string, metadata: LibraryIndexMetadata|null) -> Promise<void>`, called from `fetchAndUpdateLibraryMetadata`.

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/dialog.test.js`:

```js
// Tests for plugin/src/dialog.js's mergeDownloadFailures.
//
// dialog.js calls `ZoteroRAGDialog.init()` at the bottom, either immediately or
// on DOMContentLoaded depending on `document.readyState`. Providing a
// `document.addEventListener` that just records the callback (never invokes
// it) means init() never actually runs during load, regardless of
// `readyState` — so no further DOM stubbing is needed for this file.
//
// mergeDownloadFailures is called with an explicit `this` (via `.call()`)
// bound to a plain fake object, rather than through a real ZoteroRAGDialog
// instance — this file has no constructor/class to instantiate a fresh copy
// from (it's a single `var ZoteroRAGDialog = {...}` object), so binding a
// fake `this` is how each test gets an isolated instance's worth of state.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'dialog.js');

/** @returns {any} the ZoteroRAGDialog object (methods only — no live state) */
function loadDialogMethods() {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {},
		console,
	};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'dialog.js' });
	return context.ZoteroRAGDialog;
}

/**
 * Build a fake `this` for mergeDownloadFailures: a plugin stub recording
 * storeDownloadFailedItems calls, plus the count/callback state the real
 * ZoteroRAGDialog object carries.
 * @param {number} addedCount - What storeDownloadFailedItems should report as newly added
 * @param {number} currentCount - Pre-existing libraryMissingFilesCount for the library
 */
function makeFakeThis(addedCount, currentCount) {
	const storeCalls = [];
	const countUpdates = [];
	return {
		fakeThis: {
			plugin: {
				async storeDownloadFailedItems(libraryId, keys) {
					storeCalls.push({ libraryId, keys });
					return addedCount;
				},
			},
			libraryMissingFilesCount: new Map([['lib1', currentCount]]),
			onUnavailableCountUpdated(libraryId, count) { countUpdates.push({ libraryId, count }); },
		},
		storeCalls,
		countUpdates,
	};
}

test('mergeDownloadFailures does nothing when metadata has no failed downloads', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(0, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', { last_full_scan_failed_downloads: [] });
	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', null);

	assert.deepStrictEqual(storeCalls, []);
	assert.deepStrictEqual(countUpdates, []);
});

test('mergeDownloadFailures stores keys and bumps the count when new ones were added', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(2, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', {
		last_full_scan_failed_downloads: [
			{ item_key: 'A', attachment_key: 'ATT1' },
			{ item_key: 'B', attachment_key: 'ATT2' },
		],
	});

	assert.deepStrictEqual(storeCalls, [{ libraryId: 'lib1', keys: ['ATT1', 'ATT2'] }]);
	assert.deepStrictEqual(countUpdates, [{ libraryId: 'lib1', count: 7 }]); // 5 existing + 2 new
});

test('mergeDownloadFailures does not bump the count when nothing new was added', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(0, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', {
		last_full_scan_failed_downloads: [{ item_key: 'A', attachment_key: 'ATT1' }],
	});

	assert.deepStrictEqual(storeCalls, [{ libraryId: 'lib1', keys: ['ATT1'] }]);
	assert.deepStrictEqual(countUpdates, []);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test "plugin/test/*.test.js"`
Expected: FAIL with `TypeError: ZoteroRAGDialog.mergeDownloadFailures is not a function` (3 new failures)

- [ ] **Step 3: Write minimal implementation**

In `plugin/src/dialog.js`, extend the `LibraryIndexMetadata` JSDoc typedef (currently at lines 22-37):

```js
/**
 * @typedef {Object} LibraryIndexMetadata
 * @property {string} library_id - Library ID
 * @property {string} library_type - Library type (user/group)
 * @property {string} library_name - Library name
 * @property {number} last_indexed_version - Last indexed Zotero version
 * @property {string} last_indexed_at - ISO timestamp of last indexing
 * @property {number} total_items_indexed - Total items indexed
 * @property {number} total_chunks - Total chunks in vector store
 * @property {string} indexing_mode - Last indexing mode (full/incremental)
 * @property {boolean} force_reindex - Whether hard reset is pending
 * @property {number} last_full_scan_items_failed - Items that failed per-item
 *   processing (dead download link, extraction error) in the last completed
 *   full scan — the server's authoritative floor of currently un-indexable
 *   items; incremental syncs never update it.
 */
```

to:

```js
/**
 * @typedef {Object} LibraryIndexMetadata
 * @property {string} library_id - Library ID
 * @property {string} library_type - Library type (user/group)
 * @property {string} library_name - Library name
 * @property {number} last_indexed_version - Last indexed Zotero version
 * @property {string} last_indexed_at - ISO timestamp of last indexing
 * @property {number} total_items_indexed - Total items indexed
 * @property {number} total_chunks - Total chunks in vector store
 * @property {string} indexing_mode - Last indexing mode (full/incremental)
 * @property {boolean} force_reindex - Whether hard reset is pending
 * @property {number} last_full_scan_items_failed - Items that failed per-item
 *   processing (dead download link, extraction error) in the last completed
 *   full scan — the server's authoritative floor of currently un-indexable
 *   items; incremental syncs never update it.
 * @property {Array<{item_key: string, attachment_key: string}>} [last_full_scan_failed_downloads] -
 *   Up to 100 attachments that failed to download from Zotero in the last full
 *   scan. Unlike a parse error, these may be fixable client-side (see
 *   mergeDownloadFailures).
 */
```

Then, add a new method — placed directly before `fetchAndUpdateLibraryMetadata` (currently starting at line 579):

```js
	/**
	 * Merge a freshly-fetched library's server-reported download failures into
	 * the local Fix Unavailable store, and bump the displayed "N unavailable"
	 * count by however many were actually new. Unlike parse errors, these may
	 * be fixable client-side — but nothing else ever surfaces them, since the
	 * server only ever reports them via index-status.
	 * @param {string} libraryId - Library ID
	 * @param {LibraryIndexMetadata|null} metadata - Freshly-fetched library metadata
	 * @returns {Promise<void>}
	 */
	async mergeDownloadFailures(libraryId, metadata) {
		if (!metadata || !metadata.last_full_scan_failed_downloads || metadata.last_full_scan_failed_downloads.length === 0) {
			return;
		}
		const keys = metadata.last_full_scan_failed_downloads.map(f => f.attachment_key);
		const added = await this.plugin.storeDownloadFailedItems(libraryId, keys);
		if (added > 0) {
			const newTotal = (this.libraryMissingFilesCount.get(libraryId) || 0) + added;
			this.onUnavailableCountUpdated(libraryId, newTotal);
		}
	},

```

Finally, in `fetchAndUpdateLibraryMetadata`, change (currently at lines 589-593):

```js
			const metadata = await this.fetchLibraryMetadata(libraryId);
			this.libraryMetadata.set(libraryId, metadata);

			// Update the UI
			this.updateLibraryStatusIcon(libraryId, metadata);
```

to:

```js
			const metadata = await this.fetchLibraryMetadata(libraryId);
			this.libraryMetadata.set(libraryId, metadata);

			if (this.plugin) await this.mergeDownloadFailures(libraryId, metadata);

			// Update the UI
			this.updateLibraryStatusIcon(libraryId, metadata);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test "plugin/test/*.test.js"`
Expected: all tests PASS (3 new + 5 + 3 + 3 pre-existing = 14 total)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "feat: fetch and merge server-reported download failures on every library metadata poll"
```

---

## Task 7: Full regression run

**Files:** none (verification only)

- [ ] **Step 1: Run the entire backend test suite**

Run: `uv run pytest backend/tests/ -q --timeout=120 --deselect backend/tests/test_api_endpoints.py::TestLibraryAPIEndpoints::test_list_libraries`

(`test_list_libraries` is a pre-existing, unrelated failure confirmed via `git stash` in an earlier session — not something this plan should fix.)

Expected: all tests pass (0 failures besides the deselected pre-existing one).

- [ ] **Step 2: Run the entire plugin test suite**

Run: `node --test "plugin/test/*.test.js"`
Expected: all tests pass (14 total, per Task 6's count).

- [ ] **Step 3: If anything fails, fix forward with a new TDD cycle (new failing test → fix → pass) rather than editing a previous task's commit**

- [ ] **Step 4: Final commit if Step 3 produced any changes**

```bash
git add -A
git commit -m "fix: address regressions found in full test suite run"
```

---

## Deployment (explicit follow-up, not part of this plan's tasks)

Once all tasks pass locally, deploying to production follows CLAUDE.md's hotfix workflow: a thin patch `Dockerfile` copying `backend/models/library.py` and `backend/services/document_processor.py` onto the current `zotero-rag:latest` image, verified with `grep` for `last_full_scan_failed_downloads` and `_download_failures` inside the built image, then `sudo systemctl restart zotero-rag.service`. The plugin-side files (`zotero-rag.js`, `dialog.js`, `fix-unavailable.js`) are picked up by the existing hot-reload development server per CLAUDE.md — no rebuild needed for local testing, but a real release still needs `scripts/build_plugin.py` for distribution. This plan does not perform any of that — get explicit confirmation first.
