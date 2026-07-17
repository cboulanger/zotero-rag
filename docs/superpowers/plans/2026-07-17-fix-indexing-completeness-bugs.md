# Fix Three Indexing-Completeness Bugs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three distinct, confirmed root causes behind the "Legal Theory Knowledge Graph 6010/6027 items (incomplete)" symptom reported in the plugin's Automatic Indexing Status dialog, and make future silent indexing failures visible instead of invisible.

**Architecture:** Three independent fixes, applied sequentially because two touch the same backend file:
1. `backend/services/document_processor.py` — stop silently excluding standalone Zotero attachments (files with no bibliographic parent item) from both full and incremental sync.
2. `backend/services/document_processor.py` + `backend/services/cron_indexer.py` + `bin/index_libraries.py` — count and log items that fail per-item processing (e.g. corrupted/broken attachment downloads) instead of swallowing the failure with no trace.
3. `plugin/src/remote_indexer.js` + `plugin/src/dialog.js` — exclude Zotero Trash items from every local `Zotero.Search()` used to count or collect "indexable" items, so the plugin's displayed total matches what the backend (which already excludes trash, via the Zotero Web API's default `/items` behavior) can ever index.

**Tech Stack:** Python 3.12 / `unittest` (backend), plain JavaScript + Node's built-in `node:test` runner + `node:vm` sandboxing (plugin, first-ever test file for `remote_indexer.js`).

## Global Constraints

- Use `uv run pytest` for all Python test runs (never bare `pytest`/`python`).
- Every new function/method touched must keep existing type hints and docstring conventions already present in the file.
- No behavior change to items that already index correctly — every fix is additive (new candidates included, new counters added), never a change to existing successful paths.
- Follow CLAUDE.md: no emoji in log/print output; comments explain "why", not "what"; commit messages atomic per task.
- Do not deploy to production as part of this plan — deployment (thin patch image + `systemctl restart`, per CLAUDE.md's hotfix workflow) requires a separate, explicit user confirmation after the plan's tasks are complete and tests pass locally.

---

## Background: what each task fixes and why

This plan follows a live debugging session against the production library `groups/2829873` ("Legal Theory Knowledge Graph"). Comparing the true state of the Zotero library (fetched directly from the Zotero Web API) against what our backend had indexed found **89 items** the backend was missing, split three ways:

- **59 items** are in the Zotero Trash. The backend correctly excludes these (Zotero's `/items` endpoint excludes trash by default, and our code never overrides that) — this is *correct* backend behavior. But the plugin's own "how many items should be indexed" count is computed from a local `Zotero.Search()` call that has no `deleted` exclusion anywhere in the codebase, so it counts trashed items as "should be indexed," permanently inflating the dialog's total above what the backend can ever match. → **Task 3**.
- **~15 items** are standalone attachments — a PDF/HTML/DOCX/EPUB file added directly to a Zotero collection with no parent bibliographic record (e.g. `Z22WB65S`, `AEHUEQ8K`: `itemType: "attachment"`, `parentItem: None`, `contentType: "application/pdf"`). Both `_index_library_full`'s filter and `_filter_indexed_attachments` (used by incremental sync) unconditionally skip every `itemType == "attachment"` item, assuming attachments only ever matter as a parent's child — so these files are silently and permanently never indexed, in both full and incremental modes. → **Task 1**.
- **~15 items** have a parent-linked attachment the user has tagged `#broken_attachments` (a dead download link or corrupted file). These are correctly identified as indexing *candidates*, but fail during download/extraction inside the per-item `try/except Exception` in `_subprocess_index_batch` / `_index_library_full` / `_index_library_incremental`, which logs the error and moves on without incrementing any counter. There is currently no way to tell "never attempted" apart from "attempted and failed" from any stats the system reports. This isn't a *correctness* bug (we genuinely cannot index a broken file), but the silence is one — a library can look mysteriously "incomplete" forever with zero visibility into why. → **Task 2**.

---

## File Structure

| File | Change |
|---|---|
| `backend/services/document_processor.py` | Standalone-attachment handling in the full-sync filter, `_filter_indexed_attachments`, and `_index_item` (Task 1). `items_failed` counting in `_subprocess_index_batch`, `_index_library_full`, `_index_library_incremental` (Task 2). |
| `backend/tests/test_document_processor.py` | New tests for standalone-attachment indexing (Task 1) and `items_failed` reporting (Task 2). |
| `backend/services/cron_indexer.py` | Forward and aggregate `items_failed` in `_index_slug` and `run()` (Task 2). |
| `backend/tests/test_cron_indexer.py` | New test asserting `items_failed` is forwarded and summed (Task 2). |
| `bin/index_libraries.py` | Include failed count in the final "Done." summary log line (Task 2). |
| `plugin/src/remote_indexer.js` | Add `deleted` exclusion to all three `Zotero.Search()` call sites (Task 3). |
| `plugin/src/dialog.js` | Add `deleted` exclusion to its one `Zotero.Search()` call site (Task 3). |
| `plugin/test/remote_indexer.test.js` | **New file** — first Node test for the plugin. Sandboxed load of `remote_indexer.js` via `node:vm`, asserting the trash exclusion is applied. |

---

## Task 1: Index standalone Zotero attachments (no parent item)

**Files:**
- Modify: `backend/services/document_processor.py:511-537` (full-sync filter inside `_index_library_full`)
- Modify: `backend/services/document_processor.py:1239-1271` (`_filter_indexed_attachments`, used by incremental sync)
- Modify: `backend/services/document_processor.py:708-746` (`_index_item`)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Consumes: `INDEXABLE_MIME_TYPES` (module-level constant, `document_processor.py:56-61`), the existing `_attachment(key, parent_key, content_type="application/pdf")` test helper (`test_document_processor.py:26-41`).
- Produces: no new public interfaces — `_index_library_full`, `_filter_indexed_attachments`, and `_index_item` keep their existing signatures; a standalone attachment is simply now a valid member of the `items_with_attachments` / `items_with_content` lists they already produce, and `_index_item` now accepts an item dict whose own `data.itemType == "attachment"`.

- [x] **Step 1: Write the failing tests**

Add to `backend/tests/test_document_processor.py`, inside `class TestDocumentProcessor` (after `test_full_sync_total_items_indexed_counts_only_successes`, i.e. after line 782):

```python
    async def test_full_sync_indexes_standalone_attachment_without_parent(self):
        """A PDF/HTML/DOCX/EPUB attachment with no parentItem is itself an indexable item.

        Zotero allows dropping a file directly into a collection with no bibliographic
        parent record. Regression: the full-sync filter used to unconditionally skip
        every itemType=="attachment" item, so these files were silently and permanently
        never indexed (found via production library groups/2829873, e.g. item Z22WB65S).
        """
        standalone = {
            "version": 1,
            "data": {
                "key": "STANDALONE_PDF",
                "itemType": "attachment",
                "contentType": "application/pdf",
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [standalone]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_processed"], 1)
        self.assertEqual(result["items_added"], 1)
        # Standalone attachments have no children to fetch.
        self.mock_zotero_client.get_item_children.assert_not_called()
        # The attachment's own key is downloaded directly (item_key == attachment_key).
        self.mock_zotero_client.get_attachment_file.assert_called_once_with(
            library_id="test_lib", item_key="STANDALONE_PDF", library_type="user"
        )

    async def test_full_sync_ignores_parented_attachment_as_standalone(self):
        """An attachment that DOES have a parentItem must not be treated as its own
        item — it's only relevant via its parent's children_by_parent lookup."""
        child = _attachment("CHILD_PDF", "SOME_PARENT_NOT_IN_LIST")
        self.mock_zotero_client.get_library_items_since.return_value = [child]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_processed"], 0)

    async def test_incremental_indexes_standalone_attachment_without_parent(self):
        """Incremental sync must also pick up standalone attachments (no parentItem)."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        standalone = {
            "version": 6,
            "data": {"key": "STANDALONE", "itemType": "attachment", "contentType": "application/pdf"},
        }
        self.mock_zotero_client.get_library_items_since.return_value = [standalone]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.assertEqual(result["items_added"], 1)
        self.mock_zotero_client.get_item_children.assert_not_called()

    async def test_index_item_standalone_attachment_skips_get_item_children(self):
        """_index_item must treat a standalone attachment as its own single attachment,
        not fetch children for it (attachments don't have children in Zotero), and must
        not attempt an abstract-fallback (attachments carry no abstractNote of their own)."""
        standalone = {
            "version": 3,
            "data": {
                "key": "STANDALONE",
                "itemType": "attachment",
                "contentType": "application/pdf",
                "title": "Some Standalone File",
            },
        }
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        chunks = await self.processor._index_item(standalone, "test_lib", "user")

        self.assertEqual(chunks, 1)
        self.mock_zotero_client.get_item_children.assert_not_called()
        self.mock_zotero_client.get_attachment_file.assert_called_once_with(
            library_id="test_lib", item_key="STANDALONE", library_type="user"
        )

    async def test_filter_indexed_attachments_includes_standalone_attachment(self):
        """_filter_indexed_attachments (used by incremental sync) must not skip every
        itemType=="attachment" unconditionally — a standalone attachment with no
        parentItem is itself indexable; a parented one is not included on its own."""
        standalone = {
            "version": 1,
            "data": {"key": "STANDALONE", "itemType": "attachment", "contentType": "application/pdf"},
        }
        child_attachment = _attachment("CHILD_PDF", "PARENT_KEY")

        result = await self.processor._filter_indexed_attachments(
            [standalone, child_attachment], "test_lib", "user"
        )

        self.assertIn(standalone, result)
        self.assertNotIn(child_attachment, result)

    async def test_filter_indexed_attachments_excludes_standalone_non_indexable_type(self):
        """A standalone attachment whose contentType isn't in INDEXABLE_MIME_TYPES
        (e.g. an image) must not be included."""
        standalone_image = {
            "version": 1,
            "data": {"key": "IMG", "itemType": "attachment", "contentType": "image/png"},
        }

        result = await self.processor._filter_indexed_attachments(
            [standalone_image], "test_lib", "user"
        )

        self.assertEqual(result, [])
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_document_processor.py -k "standalone" -v`
Expected: 6 new tests FAIL. The full-sync/incremental tests fail with `items_processed`/`items_added` equal to `0` instead of `1` (the item is being filtered out entirely); `test_full_sync_ignores_parented_attachment_as_standalone` and the two `_filter_indexed_attachments` tests may already pass by coincidence (they assert exclusion) — that's fine, they still document the boundary and will keep passing after the fix.

- [x] **Step 3: Fix the full-sync filter**

In `backend/services/document_processor.py`, replace the streaming filter block inside `_index_library_full` (currently lines 514-531):

```python
            with open(tmp_path) as f:
                for line in f:
                    _item = json.loads(line)
                    if "data" not in _item:
                        continue
                    if _item["data"].get("itemType") in ("attachment", "note"):
                        continue
                    _key = _item["data"]["key"]
                    _atts = children_by_parent.get(_key, [])
                    _has_indexable = any(
                        a.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
                        for a in _atts
                    )
                    if _has_indexable:
                        items_with_attachments.append(_item)
                    elif _item["data"].get("abstractNote", "") and \
                            len(_item["data"]["abstractNote"].split()) >= min_words:
                        items_with_attachments.append(_item)
```

with:

```python
            with open(tmp_path) as f:
                for line in f:
                    _item = json.loads(line)
                    if "data" not in _item:
                        continue
                    _item_type = _item["data"].get("itemType")
                    if _item_type == "note":
                        continue
                    if _item_type == "attachment":
                        # A standalone attachment (no parentItem) is itself the indexable
                        # unit — e.g. a PDF dropped straight into a collection with no
                        # bibliographic parent. Attachments that DO have a parent are
                        # handled below via children_by_parent, keyed on the parent.
                        if not _item["data"].get("parentItem") \
                                and _item["data"].get("contentType") in INDEXABLE_MIME_TYPES:
                            items_with_attachments.append(_item)
                        continue
                    _key = _item["data"]["key"]
                    _atts = children_by_parent.get(_key, [])
                    _has_indexable = any(
                        a.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
                        for a in _atts
                    )
                    if _has_indexable:
                        items_with_attachments.append(_item)
                    elif _item["data"].get("abstractNote", "") and \
                            len(_item["data"]["abstractNote"].split()) >= min_words:
                        items_with_attachments.append(_item)
```

- [x] **Step 4: Fix `_filter_indexed_attachments`**

In `backend/services/document_processor.py`, replace the body of `_filter_indexed_attachments` (currently lines 1239-1271, inside the `for item in items:` loop):

```python
        for item in items:
            # Skip if not a regular item (skip attachments, notes, etc.)
            if "data" not in item:
                continue

            item_type = item["data"].get("itemType")
            if item_type in ["attachment", "note"]:
                continue

            # Check if item has any indexable attachments
            item_key = item["data"]["key"]
```

with:

```python
        for item in items:
            # Skip if not a regular item (skip notes; attachments handled below)
            if "data" not in item:
                continue

            item_type = item["data"].get("itemType")
            if item_type == "note":
                continue
            if item_type == "attachment":
                # A standalone attachment (no parentItem) is itself the indexable
                # unit — see the matching case in _index_library_full's filter.
                if not item["data"].get("parentItem") \
                        and item["data"].get("contentType") in INDEXABLE_MIME_TYPES:
                    items_with_content.append(item)
                continue

            # Check if item has any indexable attachments
            item_key = item["data"]["key"]
```

(The rest of the loop body — the `children_by_parent`/`get_item_children` lookup, the `has_indexable` check, and the abstract fallback — is unchanged.)

- [x] **Step 5: Fix `_index_item`**

In `backend/services/document_processor.py`, replace lines 720-746:

```python
        item_key = item["data"]["key"]
        item_version = item["version"]
        item_modified = item["data"].get("dateModified", datetime.now(UTC).isoformat())

        # Extract document metadata
        doc_metadata = DocumentMetadata(
            library_id=library_id,
            item_key=item_key,
            title=item["data"].get("title", "Untitled"),
            authors=self._extract_authors(item["data"]),
            year=self._extract_year(item["data"]),
            item_type=item["data"].get("itemType"),
        )

        # Get attachments
        attachments = await self.zotero_client.get_item_children(
            library_id=library_id,
            item_key=item_key,
            library_type=library_type
        )

        indexable_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
        ]

        abstract_note = item["data"].get("abstractNote", "")
```

with:

```python
        item_key = item["data"]["key"]
        item_version = item["version"]
        item_modified = item["data"].get("dateModified", datetime.now(UTC).isoformat())

        # Extract document metadata
        doc_metadata = DocumentMetadata(
            library_id=library_id,
            item_key=item_key,
            title=item["data"].get("title", "Untitled"),
            authors=self._extract_authors(item["data"]),
            year=self._extract_year(item["data"]),
            item_type=item["data"].get("itemType"),
        )

        is_standalone_attachment = item["data"].get("itemType") == "attachment"
        if is_standalone_attachment:
            # A standalone attachment (no parentItem) IS the indexable unit — it has
            # no children to fetch and no abstract of its own.
            attachments = [item]
        else:
            attachments = await self.zotero_client.get_item_children(
                library_id=library_id,
                item_key=item_key,
                library_type=library_type
            )

        indexable_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
        ]

        abstract_note = "" if is_standalone_attachment else item["data"].get("abstractNote", "")
```

- [x] **Step 6: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests in the file PASS, including the 6 new ones and every pre-existing test (the change is additive — no existing candidate is excluded that wasn't before).

- [x] **Step 7: Run the full backend suite**

Run: `uv run pytest`
Expected: same pass/fail counts as `main` before this change, plus the 6 new passing tests (the pre-existing, unrelated `TestLibraryAPIEndpoints::test_list_libraries` failure is not something this task touches).

- [x] **Step 8: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "$(cat <<'EOF'
fix: index standalone Zotero attachments with no parent item

Both the full-sync filter and _filter_indexed_attachments (incremental sync)
unconditionally skipped every itemType=="attachment" item, assuming
attachments only ever matter as a parent's child. Zotero also allows a PDF/
HTML/DOCX/EPUB file to be added directly to a collection with no
bibliographic parent record, and those standalone attachments were silently
and permanently excluded from indexing in both modes. Found via production
library groups/2829873, where ~15 of 89 permanently-missing items were
standalone attachments (e.g. Z22WB65S, AEHUEQ8K).

_index_item now treats an item whose own itemType is "attachment" as a
single-attachment unit (no children to fetch, no abstract fallback),
downloading it directly via its own key.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Surface silent per-item indexing failures

**Files:**
- Modify: `backend/services/document_processor.py:72-149` (`_subprocess_index_batch`)
- Modify: `backend/services/document_processor.py:334-462` (`_index_library_incremental`)
- Modify: `backend/services/document_processor.py:464-705` (`_index_library_full`)
- Modify: `backend/services/cron_indexer.py:452-518` (`_index_slug`) and `backend/services/cron_indexer.py:336-404` (`run`)
- Modify: `bin/index_libraries.py:164-169`
- Test: `backend/tests/test_document_processor.py`, `backend/tests/test_cron_indexer.py`

**Interfaces:**
- Consumes: the stats dicts already returned by `_subprocess_index_batch`, `_index_library_full`, `_index_library_incremental`, `DocumentProcessor.index_library` (all defined/returned in Task 1's files, unchanged shape except for the new key below).
- Produces: every one of those stats dicts gains a new integer key `"items_failed"` (count of items whose per-item processing raised a non-fatal exception and was skipped). `CronIndexer._index_slug`'s returned dict and `CronIndexer.run`'s returned `total_stats` dict both gain the same key, summed across libraries for `run`. This key is additive to every existing dict — no existing key changes name, type, or meaning.

- [x] **Step 1: Write the failing tests**

Add to `backend/tests/test_document_processor.py`, inside `class TestDocumentProcessor` (after the Task 1 tests):

```python
    async def test_full_sync_reports_items_failed(self):
        """A per-item processing failure (e.g. corrupted/broken attachment download)
        must be counted as items_failed, not silently disappear from every stat."""
        item_a = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        item_b = {"version": 1, "data": {"key": "BBB", "itemType": "journalArticle", "title": "B"}}

        all_items = [item_a, item_b, _attachment("PDFA", "AAA"), _attachment("PDFB", "BBB")]
        self.mock_zotero_client.get_library_items_since.return_value = all_items
        self.mock_zotero_client.get_item_children.side_effect = (
            lambda library_id, item_key, library_type: [
                a for a in all_items if a["data"].get("parentItem") == item_key
            ]
        )
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.side_effect = [
            _make_extraction_chunks(("content", 1)),
            ValueError("boom"),
        ]
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_added"], 1)
        self.assertEqual(result["items_failed"], 1)

    async def test_incremental_reports_items_failed(self):
        """Incremental sync must also count per-item failures instead of dropping them."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        item = {"version": 6, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = {"data": {"key": "PDF", "itemType": "attachment", "contentType": "application/pdf"}}
        self.mock_zotero_client.get_library_items_since.return_value = [item]
        self.mock_zotero_client.get_item_children.return_value = [pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.side_effect = ValueError("boom")

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)
```

Add to `backend/tests/test_document_processor.py`, inside `class TestSubprocessIndexBatchFunction` (find it via `grep -n "class TestSubprocessIndexBatchFunction" backend/tests/test_document_processor.py`; add as a new method alongside its existing two tests):

```python
    def test_reports_items_failed_for_per_item_exception(self):
        """_subprocess_index_batch must count a per-item exception as items_failed,
        not just log it and move on with no trace in the returned stats."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        item = {"version": 1, "data": {"key": "BAD", "itemType": "journalArticle", "title": "A"}}

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=MagicMock()), \
             patch.object(
                 dp_module.DocumentProcessor, "_index_item",
                 AsyncMock(side_effect=ValueError("corrupted attachment")),
             ):
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

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)
```

Add this as a new method inside the existing `class TestSubprocessIndexBatchFunction(unittest.TestCase):` (find it via `grep -n "class TestSubprocessIndexBatchFunction" backend/tests/test_document_processor.py`), alongside its two existing tests — match their exact mocking style (`patch(...) as mock_settings` then `mock_settings.return_value = MagicMock(...)`, and patch targets at the *source* module of each local import: `backend.zotero.web_api.ZoteroWebAPI`, `backend.services.embeddings.create_embedding_service`, `backend.dependencies.make_vector_store` — never `backend.services.document_processor.X`, since `_subprocess_index_batch` imports each of these locally inside the function body, so the patch must target where they're defined, not where they're imported from).

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_document_processor.py -k "items_failed" -v`
Expected: all 3 new tests FAIL with `KeyError: 'items_failed'` (the key doesn't exist yet in any returned dict).

- [x] **Step 3: Add `items_failed` to `_subprocess_index_batch`**

In `backend/services/document_processor.py`, inside `_subprocess_index_batch`'s `_run()` (currently lines 114-147):

```python
        chunks_added = items_added = items_updated = items_skipped = 0
        async with web_api:
            processor = DocumentProcessor(
                zotero_client=web_api,
                embedding_service=embedding_service,
                vector_store=vector_store,
            )
            for item in items:
                item_key = item["data"]["key"]
                item_version = item.get("version", 0)
                existing = indexed_versions.get(item_key)
                try:
                    if existing is None:
                        n = await processor._index_item(item, library_id, library_type)
                        chunks_added += n
                        items_added += 1
                    elif existing < item_version:
                        vector_store.delete_item_chunks(library_id, item_key)
                        n = await processor._index_item(item, library_id, library_type)
                        chunks_added += n
                        items_updated += 1
                    else:
                        items_skipped += 1
                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item %s in subprocess batch: %s", item_key, e, exc_info=True)

        return {
            "chunks_added": chunks_added,
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
        }
```

Replace with:

```python
        chunks_added = items_added = items_updated = items_skipped = items_failed = 0
        async with web_api:
            processor = DocumentProcessor(
                zotero_client=web_api,
                embedding_service=embedding_service,
                vector_store=vector_store,
            )
            for item in items:
                item_key = item["data"]["key"]
                item_version = item.get("version", 0)
                existing = indexed_versions.get(item_key)
                try:
                    if existing is None:
                        n = await processor._index_item(item, library_id, library_type)
                        chunks_added += n
                        items_added += 1
                    elif existing < item_version:
                        vector_store.delete_item_chunks(library_id, item_key)
                        n = await processor._index_item(item, library_id, library_type)
                        chunks_added += n
                        items_updated += 1
                    else:
                        items_skipped += 1
                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item %s in subprocess batch: %s", item_key, e, exc_info=True)
                    items_failed += 1

        return {
            "chunks_added": chunks_added,
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "items_failed": items_failed,
        }
```

(`_run_subprocess_batch`, which calls this function and does `result_queue.put({"fatal": False, **result})`, needs no change — it already forwards every key in the returned dict verbatim.)

- [x] **Step 4: Add `items_failed` to `_index_library_full`**

In `backend/services/document_processor.py`, three edits:

1. Initialization (currently `chunks_added = 0`, `items_added = 0`, `items_updated = 0`, `items_skipped = 0` around lines 579-582) — add `items_failed = 0` alongside them:

```python
        chunks_added = 0
        items_added = 0
        items_updated = 0
        items_skipped = 0
        items_failed = 0
        total_items = len(items_with_attachments)
```

2. Subprocess result accumulation (currently lines 631-634):

```python
                    chunks_added += result.get("chunks_added", 0)
                    items_added += result.get("items_added", 0)
                    items_updated += result.get("items_updated", 0)
                    items_skipped += result.get("items_skipped", 0)
```

Replace with:

```python
                    chunks_added += result.get("chunks_added", 0)
                    items_added += result.get("items_added", 0)
                    items_updated += result.get("items_updated", 0)
                    items_skipped += result.get("items_skipped", 0)
                    items_failed += result.get("items_failed", 0)
```

3. Inline-processing except block (currently lines 677-680, inside the `else:` branch used when `settings.testing=True`):

```python
                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item in full sync mode: %s", e, exc_info=True)
```

Replace with:

```python
                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item in full sync mode: %s", e, exc_info=True)
                    items_failed += 1
```

4. Final metadata/return block (currently lines 686-705):

```python
        metadata.last_indexed_version = max_version_seen
        # Count only items that are actually indexed (newly added, updated, or already
        # current) — NOT len(items_with_attachments), which includes items that failed
        # to process.  Otherwise a scan that fails to embed everything records a full
        # count and masks an un-indexed library, defeating the cron under-indexed
        # auto-recovery in CronIndexer._resolve_mode.
        metadata.total_items_indexed = items_added + items_updated + items_skipped
        # last_full_scan_indexable is the scan floor: how many items had indexable
        # content this scan, used to detect legitimately low indexable ratios.
        metadata.last_full_scan_indexable = len(items_with_attachments)

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "orphaned_items": orphaned_item_count,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
```

Replace with:

```python
        metadata.last_indexed_version = max_version_seen
        # Count only items that are actually indexed (newly added, updated, or already
        # current) — NOT len(items_with_attachments), which includes items that failed
        # to process.  Otherwise a scan that fails to embed everything records a full
        # count and masks an un-indexed library, defeating the cron under-indexed
        # auto-recovery in CronIndexer._resolve_mode.
        metadata.total_items_indexed = items_added + items_updated + items_skipped
        # last_full_scan_indexable is the scan floor: how many items had indexable
        # content this scan, used to detect legitimately low indexable ratios.
        metadata.last_full_scan_indexable = len(items_with_attachments)

        if items_failed:
            logger.warning(
                "Full sync for library %s: %d item(s) failed to process and were "
                "skipped (see errors above); they remain candidates for the next scan",
                library_id, items_failed,
            )

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "items_failed": items_failed,
            "orphaned_items": orphaned_item_count,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
```

(Leave the rest of that `return` dict — `"last_version"` etc — untouched; only insert the new `"items_failed"` line and the warning block above it.)

- [x] **Step 5: Add `items_failed` to `_index_library_incremental`**

In `backend/services/document_processor.py`, three edits:

1. Initialization (currently lines 376-379):

```python
        items_added = 0
        items_updated = 0
        chunks_added = 0
        chunks_deleted = 0
        total_items = len(items_with_attachments)
```

Replace with:

```python
        items_added = 0
        items_updated = 0
        items_failed = 0
        chunks_added = 0
        chunks_deleted = 0
        total_items = len(items_with_attachments)
```

2. Except block (currently lines 418-423):

```python
            except _FATAL_EMBEDDING_ERRORS:
                # Embedding key/quota failure affects every item — abort the run so
                # the caller surfaces an error instead of silently skipping everything.
                raise
            except Exception as e:
                logger.error(f"Error processing item in incremental mode: {e}", exc_info=True)
```

Replace with:

```python
            except _FATAL_EMBEDDING_ERRORS:
                # Embedding key/quota failure affects every item — abort the run so
                # the caller surfaces an error instead of silently skipping everything.
                raise
            except Exception as e:
                logger.error(f"Error processing item in incremental mode: {e}", exc_info=True)
                items_failed += 1
```

3. Final return block (currently lines 451-462):

```python
        # Update metadata with new version
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = metadata.total_items_indexed + items_added

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }
```

Replace with:

```python
        # Update metadata with new version
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = metadata.total_items_indexed + items_added

        if items_failed:
            logger.warning(
                "Incremental sync for library %s: %d item(s) failed to process and "
                "were skipped (see errors above); they remain candidates for the next scan",
                library_id, items_failed,
            )

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "items_failed": items_failed,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }
```

- [x] **Step 6: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests PASS, including the 3 new `items_failed` tests and every test from Task 1.

- [x] **Step 7: Write the failing cron_indexer test**

Add to `backend/tests/test_cron_indexer.py`, inside `class TestCronIndexerRun` (after `test_run_success`, i.e. after line 223):

```python
    async def test_run_aggregates_items_failed(self):
        """items_failed must be forwarded per-slug and summed across the whole run, so a
        library with silently-failing items (e.g. a broken attachment download) shows up
        in cron_status.json and the final summary instead of vanishing without a trace."""
        indexer = _make_indexer(["users/1", "groups/2"], self.tmp)

        fake_stats = {"items_processed": 10, "chunks_added": 50, "items_failed": 3, "mode": "full"}

        with patch("backend.services.cron_indexer.ZoteroWebAPI") as MockWebAPI, \
             patch("backend.services.cron_indexer.DocumentProcessor") as MockProcessor, \
             _patch_embedding_service():

            mock_api_instance = AsyncMock()
            MockWebAPI.return_value.__aenter__ = AsyncMock(return_value=mock_api_instance)
            MockWebAPI.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_proc_instance = MagicMock()
            mock_proc_instance.index_library = AsyncMock(return_value=fake_stats)
            MockProcessor.return_value = mock_proc_instance

            result = await indexer.run()

        self.assertEqual(result["items_failed"], 6)  # 3 per slug x 2 slugs
        status = indexer._read_status()
        self.assertEqual(status["slugs"]["users/1"]["items_failed"], 3)
        self.assertEqual(status["slugs"]["groups/2"]["items_failed"], 3)
```

- [x] **Step 8: Run the cron_indexer test to verify it fails**

Run: `uv run pytest backend/tests/test_cron_indexer.py -k test_run_aggregates_items_failed -v`
Expected: FAIL — `result["items_failed"]` raises `KeyError` (neither `total_stats` nor `_index_slug`'s returned dict has this key yet).

- [x] **Step 9: Forward and aggregate `items_failed` in `cron_indexer.py`**

In `backend/services/cron_indexer.py`:

1. `total_stats` initialization (currently lines 336-340):

```python
        total_stats: dict = {
            "items_processed": 0,
            "chunks_added": 0,
            "libraries": [],
        }
```

Replace with:

```python
        total_stats: dict = {
            "items_processed": 0,
            "chunks_added": 0,
            "items_failed": 0,
            "libraries": [],
        }
```

2. Aggregation loop (currently lines 379-381):

```python
                total_stats["items_processed"] += slug_stats.get("items_processed", 0)
                total_stats["chunks_added"] += slug_stats.get("chunks_added", 0)
                total_stats["libraries"].append(slug_info.slug)
```

Replace with:

```python
                total_stats["items_processed"] += slug_stats.get("items_processed", 0)
                total_stats["chunks_added"] += slug_stats.get("chunks_added", 0)
                total_stats["items_failed"] += slug_stats.get("items_failed", 0)
                total_stats["libraries"].append(slug_info.slug)
```

3. `_index_slug`'s success-path log and return (currently lines 507-518):

```python
            self.log.info(
                "Finished %s: %s items, %s chunks added",
                slug_info.slug,
                stats.get("items_processed", 0),
                stats.get("chunks_added", 0),
            )
            return {
                "items_processed": stats.get("items_processed", 0),
                "chunks_added": stats.get("chunks_added", 0),
                "last_update": datetime.now(timezone.utc).isoformat(),
                "rate_limit_headers": await embedding_service.get_rate_limit_info(),
            }
```

Replace with:

```python
            items_failed = stats.get("items_failed", 0)
            self.log.info(
                "Finished %s: %s items, %s chunks added%s",
                slug_info.slug,
                stats.get("items_processed", 0),
                stats.get("chunks_added", 0),
                f", {items_failed} failed" if items_failed else "",
            )
            return {
                "items_processed": stats.get("items_processed", 0),
                "chunks_added": stats.get("chunks_added", 0),
                "items_failed": items_failed,
                "last_update": datetime.now(timezone.utc).isoformat(),
                "rate_limit_headers": await embedding_service.get_rate_limit_info(),
            }
```

- [x] **Step 10: Run the cron_indexer test to verify it passes**

Run: `uv run pytest backend/tests/test_cron_indexer.py -v`
Expected: all tests PASS, including the new `test_run_aggregates_items_failed`.

- [x] **Step 11: Update the final summary log line in `bin/index_libraries.py`**

In `bin/index_libraries.py`, replace lines 164-169:

```python
        stats = await indexer.run()
        log.info(
            "Done. Total items processed: %s, chunks added: %s",
            stats.get("items_processed", 0),
            stats.get("chunks_added", 0),
        )
        return 0
```

with:

```python
        stats = await indexer.run()
        items_failed = stats.get("items_failed", 0)
        log.info(
            "Done. Total items processed: %s, chunks added: %s%s",
            stats.get("items_processed", 0),
            stats.get("chunks_added", 0),
            f", {items_failed} failed" if items_failed else "",
        )
        return 0
```

- [x] **Step 12: Run the full backend suite**

Run: `uv run pytest`
Expected: same pass/fail counts as after Task 1, plus the new passing tests from this task (again excluding the pre-existing unrelated `test_list_libraries` failure).

- [x] **Step 13: Commit**

```bash
git add backend/services/document_processor.py backend/services/cron_indexer.py \
        backend/tests/test_document_processor.py backend/tests/test_cron_indexer.py \
        bin/index_libraries.py
git commit -m "$(cat <<'EOF'
fix: surface silently-failing indexing items as items_failed

Per-item processing failures (e.g. a corrupted or broken-link attachment
download, extraction error) were caught, logged, and otherwise forgotten —
none of items_added/items_updated/items_skipped accounted for them, and no
stats dict anywhere distinguished "failed" from "never attempted". A library
with a handful of broken attachments (Zotero tag #broken_attachments) could
look mysteriously and permanently "incomplete" with zero visibility into why.

items_failed is now counted in _subprocess_index_batch, _index_library_full,
and _index_library_incremental, forwarded through CronIndexer._index_slug
and summed in CronIndexer.run, and included in the final cron summary log
line. This is purely additive observability — it does not change what gets
indexed, only what gets reported.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Exclude Zotero Trash from the plugin's local item searches

**Files:**
- Modify: `plugin/src/remote_indexer.js:556-558` (`_collectAttachments`)
- Modify: `plugin/src/remote_indexer.js:636-638` (`countIndexableAttachments`)
- Modify: `plugin/src/remote_indexer.js:1055-1057` (`_collectAbstractItems`)
- Modify: `plugin/src/dialog.js:1262-1264` (`downloadMissingAttachments`)
- Test: `plugin/test/remote_indexer.test.js` (**new file**)

**Interfaces:**
- Consumes: none new — `Zotero.Search` is the existing global, used the same way (`new Zotero.Search()`, set `.libraryID`, call `.search()`), just with one extra `addCondition` call before `.search()`.
- Produces: no signature changes anywhere; the fix only narrows what each existing `Zotero.Search()` call returns.

**Why this is safe:** every one of these four call sites already assumes it's enumerating "real," currently-indexable library items (they feed directly into indexing-candidate collection or a displayed "total indexable" count). None of them has any existing code path that intentionally wants trashed items included — there is no `deleted` condition anywhere in the plugin today. Excluding trash is a pure narrowing that fixes the client's over-count without touching any other behavior.

- [x] **Step 1: Write the failing test**

Create `plugin/test/remote_indexer.test.js`:

```javascript
// Tests for plugin/src/remote_indexer.js.
//
// remote_indexer.js is a plain script (not a CommonJS module — it's loaded by
// dialog.xhtml as a <script> tag inside Zotero's chrome environment, where
// `Zotero` is a global). To unit test it in plain Node without touching that
// loading contract, we read the source and evaluate it inside a vm context
// with a stubbed `Zotero` global, then pull the `RemoteIndexer` object (a
// top-level `var`) back out of that context.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'remote_indexer.js');

/**
 * Load RemoteIndexer into a fresh vm context with the given Zotero stub.
 * @param {any} zoteroStub
 * @returns {any} the RemoteIndexer object
 */
function loadRemoteIndexer(zoteroStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'remote_indexer.js' });
	return context.RemoteIndexer;
}

/**
 * Build a minimal Zotero stub whose Zotero.Search() returns a fake search
 * object recording every addCondition(...) call, and whose .search() /
 * Zotero.Items.getAsync() resolve to an empty item list (sufficient for
 * exercising the trash-exclusion call itself).
 * @returns {{ zotero: any, addedConditions: Array<any[]> }}
 */
function makeZoteroStub() {
	const addedConditions = [];
	const fakeSearch = {
		libraryID: null,
		addCondition(...args) { addedConditions.push(args); },
		async search() { return []; },
	};
	const zotero = {
		Groups: { get: () => ({ libraryID: 1 }) },
		Libraries: { userLibraryID: 1 },
		Search: function () { return fakeSearch; },
		Items: { getAsync: async () => [] },
	};
	return { zotero, addedConditions };
}

test('countIndexableAttachments excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer.countIndexableAttachments('123', 'group');

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

test('_collectAttachments excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer._collectAttachments('123', 'group', () => {});

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

test('_collectAbstractItems excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer._collectAbstractItems('123', 'group', () => {}, []);

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});
```

- [x] **Step 2: Run the test to verify it fails**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: all 3 tests FAIL — `addedConditions` is `[]` instead of `[['deleted', 'false']]`, since no fix has been applied yet.

- [x] **Step 3: Fix `_collectAttachments`**

In `plugin/src/remote_indexer.js`, replace lines 556-558:

```javascript
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
```

with:

```javascript
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		search.addCondition('deleted', 'false');
		const itemIDs = await search.search();
```

- [x] **Step 4: Fix `countIndexableAttachments`**

In `plugin/src/remote_indexer.js`, replace lines 636-638 (the second occurrence of this pattern — inside `countIndexableAttachments`):

```javascript
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
```

with:

```javascript
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		search.addCondition('deleted', 'false');
		const itemIDs = await search.search();
```

- [x] **Step 5: Fix `_collectAbstractItems`**

In `plugin/src/remote_indexer.js`, replace lines 1055-1057 (the third occurrence — inside `_collectAbstractItems`):

```javascript
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
```

with:

```javascript
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		search.addCondition('deleted', 'false');
		const itemIDs = await search.search();
```

- [x] **Step 6: Run the test to verify it passes**

Run: `node --test plugin/test/remote_indexer.test.js`
Expected: all 3 tests PASS.

- [x] **Step 7: Fix `dialog.js`'s `downloadMissingAttachments`**

In `plugin/src/dialog.js`, replace lines 1262-1264:

```javascript
		// Get all items in the library
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
```

with:

```javascript
		// Get all items in the library (excluding Trash — matches the backend, which
		// never indexes trashed items either).
		const search = new Zotero.Search();
		(/** @type {any} */ (search)).libraryID = zoteroLibraryID;
		search.addCondition('deleted', 'false');
		const itemIDs = await search.search();
```

`dialog.js` is a large, stateful, class-based file (1860 lines) with many constructor dependencies not worth stubbing for one line identical to the three already covered by the automated test above. Verify this one live instead (Step 8).

- [ ] **Step 8: Live verification in the Zotero client**

Per CLAUDE.md's plugin dev workflow, do **not** rebuild the plugin — the hot-reload dev server picks up source changes automatically.

1. Ensure the plugin dev server is running (`npm run start` from the plugin's directory, if not already running).
2. In Zotero, open the "Automatic Indexing Status" dialog (the same one that showed "Legal Theory Knowledge Graph ... 6010/6027 items (incomplete)").
3. Confirm the displayed total for that library has dropped — it should no longer include the ~59 trashed items counted before this fix (exact new total will also reflect Task 1's standalone-attachment fix and any further library changes since the investigation).
4. Open the Browser Console (Tools → Developer → Browser Console) and confirm no new errors appear when the dialog loads or refreshes.
5. Trigger a manual "reindex" or "download missing attachments" action for a library with at least one trashed item (if available) and confirm via the Browser Console / network log that trashed items are no longer included in the collected attachment list.

- [x] **Step 9: Commit**

```bash
git add plugin/src/remote_indexer.js plugin/src/dialog.js plugin/test/remote_indexer.test.js
git commit -m "$(cat <<'EOF'
fix: exclude Zotero Trash from local indexable-item searches

Every Zotero.Search() call the plugin uses to enumerate "items that should
be indexed" (_collectAttachments, countIndexableAttachments,
_collectAbstractItems, downloadMissingAttachments) had no `deleted`
exclusion condition anywhere, so trashed items were counted as indexable.
The backend already excludes trash by default (Zotero's /items Web API
endpoint excludes it, and our code never overrides that), so the plugin's
local count could never match the backend's — inflating the dialog's
"X/Y items (incomplete)" total forever, even once the library was fully and
correctly indexed. Found via production library groups/2829873: 59 of the
89 items the backend was "missing" turned out to be genuinely in the Trash.

This is the plugin's first Node test file (plugin/test/remote_indexer.test.js),
using node:vm to sandbox-load the plain (non-module) script with a stubbed
Zotero global.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: End-to-end production verification (manual, post-deploy)

This task is **not** part of the automated test suite — it verifies the real production symptom is gone once Tasks 1-3 are deployed. Deployment itself (thin patch image + `systemctl restart`, per CLAUDE.md) requires a separate explicit go-ahead from the user; do not perform it as part of executing this plan.

- [ ] **Step 1: After deployment, force a full reindex of the affected library**

```bash
curl -X POST https://rag.example.com/api/autoindex/scheduler/run-now \
  -H "X-Zotero-API-Key: <admin-read-only-key>"
```

or, if a targeted single-library reindex endpoint is used instead, trigger it for `groups/2829873`.

- [ ] **Step 2: Re-run the ground-truth diff**

Re-run the same comparison used during the original investigation: fetch the live Zotero item list for `groups/2829873` via `ZoteroWebAPI`, compute the true indexable parent-attachment key set (matching `INDEXABLE_MIME_TYPES`), compare against the distinct `item_key`s currently in the `document_chunks` Qdrant collection for that library.

Expected: the "missing" set shrinks from 89 to roughly the ~15 items whose only attachment is tagged `#broken_attachments` (genuinely corrupted/dead files — not a bug, and now visible via the `items_failed` counter and warning log added in Task 2 instead of disappearing silently).

- [ ] **Step 3: Confirm the plugin dialog**

Open the "Automatic Indexing Status" dialog for the same library and confirm the displayed count now reads `total/total` (complete) or, if the ~15 broken-attachment items are still uncounted by the plugin's local total, that the gap is now small and stable (not growing) run over run — i.e. no further silent drift.

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers the standalone-attachment bug (full sync, incremental sync, and the shared `_index_item` helper). Task 2 covers the silent-failure observability gap end-to-end (subprocess → full/incremental → cron aggregation → final log line). Task 3 covers all four `Zotero.Search()` call sites that feed indexing candidates or counts. Task 4 verifies the original reported symptom end-to-end against production.
- **Type consistency:** `items_failed` is an `int` everywhere it's introduced (subprocess dict, full-sync dict, incremental dict, `_index_slug` dict, `run()`'s `total_stats` dict) — no type drift between tasks.
- **No placeholders:** every step shows the literal before/after code to change; no step says "add tests for the above" without the actual test code.

---

## Execution Record (2026-07-17)

Tasks 1-3 implemented via `superpowers:subagent-driven-development`, each with
a fresh implementer subagent and a task-scoped reviewer, plus a final
whole-branch review (Opus). Commits (in order): `0838e8a` (Task 1), `251c62a`
(Task 2), `2aca674` (Task 3).

**Post-plan fix, from the final whole-branch review:** the review found
`items_failed` (Task 2) only caught per-item exceptions — a dead attachment
download link (`ZoteroWebAPI.get_attachment_file` returning `None` on
404/non-200, handled without raising) still silently counted as
`items_added` with zero content, missing half of the plan's own motivating
`#broken_attachments` scenario. Fixed in `7097ae7`: a zero-chunk
`_index_item` result now counts as `items_failed` in the full-sync paths and
`_subprocess_index_batch` (incremental sync's live-version gate was already
immune to this). That fix's own review then found a false positive it
introduced — a legitimate same-item "already indexed, nothing to redo"
duplicate skip (`_handle_same_library_duplicate`'s `skipped_duplicate`
status) also returns zero chunks and was being misclassified as failed.
Fixed in `87bb1f3` by adding `_item_has_indexed_content()`, which checks the
vector store directly before counting a zero-chunk result as a failure. Both
fixes were re-reviewed clean.

**Still outstanding (not run by this session):**
- Task 3 Step 8 (live verification in a running Zotero client) — requires an
  interactive Zotero desktop session; deferred to a human.
- Task 4 (end-to-end production verification) — explicitly manual/post-deploy
  per this plan's Global Constraints; requires deployment, which itself
  requires separate explicit user confirmation.
