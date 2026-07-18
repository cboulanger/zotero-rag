# Metadata-Only Reindex Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Zotero item's version increases only because its own fields changed (title, creators, tags, date, itemType) — not its attachment content or fallback abstract text — skip the expensive delete-then-reextract-then-reembed cycle and instead patch the existing chunks' payload fields in place.

**Architecture:** A new `DocumentProcessor._try_metadata_only_update()` method decides, per item, whether the version bump is metadata-only by comparing the *current* Zotero attachment versions (or, for abstract-fallback items, a fresh hash of the current abstract text) against what's already stored on the item's existing chunks. If everything matches, it calls a new `VectorStore.update_item_bibliographic_metadata()` method (a thin wrapper around the existing `update_item_metadata()` payload-patch primitive) instead of `delete_item_chunks()` + `_index_item()`. The check is inserted into the three call sites that currently do blind delete-then-reindex on a version bump: `_index_library_incremental`, `_index_library_full`'s inline loop, and the subprocess worker (`_subprocess_index_batch`) that production actually runs under.

**Tech Stack:** Python 3.12 / `unittest` + `pytest` (backend only — no plugin or infra changes).

## Global Constraints

- Run tests with `uv run pytest`, never bare `pytest`/`python` (per CLAUDE.md).
- No behavior change for genuine content changes (attachment re-uploaded, added, or removed; abstract text edited) — those must still go through the full existing delete+reindex path unchanged.
- Standalone attachments (`itemType == "attachment"`, no parent item) are explicitly out of scope: Zotero bumps an attachment's own version for both a metadata-only edit (e.g. renaming it) and a real file re-upload, and there is no cheap signal (short of downloading and hashing the file) to tell those apart. `_try_metadata_only_update` must always return `False` for these, unconditionally falling through to the existing path.
- Catalog-only stub items (`has_content: False`, see `_add_catalog_stub`) are out of scope — that path is already a cheap payload-only rewrite with no extraction/embedding, so this optimization has nothing to add there. `_try_metadata_only_update` must return `False` whenever any existing chunk for the item has `has_content: False`.
- Every new function/method must keep the existing type hint and docstring conventions already present in its file.
- Comments explain "why", not "what" (CLAUDE.md).
- Commit after each task, not once at the end.
- Do not deploy to production as part of this plan — deployment (thin patch image + `systemctl restart`, per CLAUDE.md's hotfix workflow) requires separate, explicit confirmation after all tasks are complete and tests pass locally.

---

## Background

Confirmed in a live debugging conversation: editing only an item's title, authors, or tags in Zotero bumps that item's own `version` field (Zotero bumps an object's version on any field edit, not just content edits). The next incremental (or full) sync run then treats this exactly like a real content change — `_index_library_incremental`, `_index_library_full`, and the subprocess worker `_subprocess_index_batch` all do the same thing on `existing_version < item_version`: unconditionally `delete_item_chunks()` then `_index_item()`. `_index_item()` re-downloads every indexable attachment from Zotero, re-runs it through Kreuzberg extraction, and re-embeds every chunk — even when the attachment content is byte-for-byte identical to what's already indexed.

There already is a content-hash-based dedup fast path (`_handle_same_library_duplicate`) meant to catch "this exact content is already indexed," but it can never fire for this scenario: it only short-circuits when `get_item_version()` still finds existing chunks for the item, and by the time it runs, `delete_item_chunks()` has already wiped them (its own docstring calls this the "orphaned, no chunks left to reuse" case). Fixing the ordering there is not this plan's approach — instead, we add an earlier, explicit check that decides *before* any deletion happens whether the item's actual content changed at all.

**Two content-identity signals, one per indexing strategy:**
- **Attachment-backed items** (one or more real PDFs/HTML/DOCX/EPUB children): each stored chunk carries `attachment_key` and `attachment_version` in its payload. Compare the full `{attachment_key: attachment_version}` map from existing chunks against a freshly-fetched `get_item_children()` call, filtered to `INDEXABLE_MIME_TYPES`. Any difference (a version changed, an attachment was added, or one was removed) means content changed.
- **Abstract-fallback items** (no attachment, indexed from `abstractNote` — see `_index_from_abstract`): the stored `content_hash` on the chunks *is* `sha256(abstract_text)`. Recompute that hash from the item's current `abstractNote` and compare.

If neither signal shows a change, everything that changed is bibliographic metadata (title, creators → authors, tags, date → year, itemType) — patch those fields in place via `VectorStore.update_item_bibliographic_metadata()`, which builds the same derived fields (`author_lastnames`, `tags_lower`) that `add_chunk`/`add_chunks_batch` already compute, and delegates to the existing `update_item_metadata()` payload-patch primitive (today only used by the plugin's schema-backfill endpoint in `document_upload.py`).

---

## File Structure

| File | Change |
|---|---|
| `backend/db/vector_store.py` | New method `update_item_bibliographic_metadata()` (Task 1). |
| `backend/tests/test_vector_store.py` | Test for the new method (Task 1). |
| `backend/services/document_processor.py` | New method `_try_metadata_only_update()` (Task 2). Wired into `_index_library_incremental` (Task 3), `_index_library_full`'s inline loop (Task 4), and `_subprocess_index_batch` (Task 5). |
| `backend/tests/test_document_processor.py` | Tests for `_try_metadata_only_update()` (Task 2) and the three wiring points (Tasks 3–5); `get_item_chunks` default added to `TestDocumentProcessor.setUp` and `TestSubprocessBatchIndexing.setUp` (Task 2). |
| `backend/tests/test_incremental_indexing.py` | `get_item_chunks` default added to `TestIncrementalIndexing.asyncSetUp` (Task 2). |

---

## Task 1: `VectorStore.update_item_bibliographic_metadata()`

**Files:**
- Modify: `backend/db/vector_store.py` (add method near `update_item_metadata`, currently at line 595)
- Test: `backend/tests/test_vector_store.py`

**Interfaces:**
- Consumes: `self.update_item_metadata(library_id, item_key, fields) -> int` (existing, `vector_store.py:595`); module-level `_extract_lastnames(authors: list[str]) -> list[str]` and `_lower_all(values: list[str]) -> list[str]` (existing, `vector_store.py:42-58` and the one added alongside it).
- Produces: `VectorStore.update_item_bibliographic_metadata(library_id: str, item_key: str, *, title: Optional[str], authors: list[str], tags: list[str], year: Optional[int], item_type: Optional[str], item_version: int, zotero_modified: str) -> int` — returns the number of chunks patched (0 if the item has no existing chunks). Task 2 calls this.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_vector_store.py`, in the `TestVectorStore` class (after `test_get_stub_item_keys`, before `test_delete_library_chunks`):

```python
    def test_update_item_bibliographic_metadata_patches_all_chunks(self):
        """update_item_bibliographic_metadata must patch title/authors/tags/
        year/item_type/item_version on every existing chunk without touching
        text or embeddings — used for the metadata-only reindex fast path."""
        chunks = [
            DocumentChunk(
                text=f"Chunk {i}",
                metadata=ChunkMetadata(
                    chunk_id=f"chunk-{i}",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="ITEM1",
                        title="Old Title",
                        authors=["Old Author"],
                        year=2000,
                        item_type="book",
                    ),
                    page_number=1,
                    text_preview=f"Chunk {i}",
                    chunk_index=i,
                    content_hash=f"hash{i}",
                    item_version=1,
                ),
                embedding=[0.1] * 384,
            )
            for i in range(3)
        ]
        self.vector_store.add_chunks_batch(chunks)

        updated = self.vector_store.update_item_bibliographic_metadata(
            "1", "ITEM1",
            title="New Title", authors=["New Author"], tags=["Law"],
            year=2020, item_type="journalArticle", item_version=2,
            zotero_modified="2026-01-01T00:00:00Z",
        )

        self.assertEqual(updated, 3)
        results = self.vector_store.get_item_chunks("1", "ITEM1")
        self.assertEqual(len(results), 3)
        for r in results:
            payload = r["payload"]
            self.assertEqual(payload["title"], "New Title")
            self.assertEqual(payload["authors"], ["New Author"])
            self.assertEqual(payload["author_lastnames"], ["author"])
            self.assertEqual(payload["tags"], ["Law"])
            self.assertEqual(payload["tags_lower"], ["law"])
            self.assertEqual(payload["year"], 2020)
            self.assertEqual(payload["item_type"], "journalArticle")
            self.assertEqual(payload["item_version"], 2)
            self.assertEqual(payload["zotero_modified"], "2026-01-01T00:00:00Z")
            # Text and embedding must be untouched by the patch
            self.assertEqual(payload["text"], f"Chunk {payload['chunk_index']}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_vector_store.py -k test_update_item_bibliographic_metadata_patches_all_chunks -v`
Expected: FAIL with `AttributeError: 'VectorStore' object has no attribute 'update_item_bibliographic_metadata'`

- [ ] **Step 3: Write minimal implementation**

In `backend/db/vector_store.py`, add this method directly after `update_item_metadata` (which ends at line 633, just before `check_duplicate` at line 635):

```python
    def update_item_bibliographic_metadata(
        self,
        library_id: str,
        item_key: str,
        *,
        title: Optional[str],
        authors: list[str],
        tags: list[str],
        year: Optional[int],
        item_type: Optional[str],
        item_version: int,
        zotero_modified: str,
    ) -> int:
        """
        Patch bibliographic fields on all chunks of an item without re-embedding.

        Used for the metadata-only reindex fast path (DocumentProcessor.
        _try_metadata_only_update): the item's own fields changed (title,
        creators, tags, date, itemType) but its indexed content — attachment
        bytes, or the fallback abstract text — is unchanged, so there's no
        need to re-extract or re-embed anything.

        Returns:
            Number of chunks patched (0 if the item has no existing chunks).
        """
        fields = {
            "title": title,
            "authors": authors,
            "author_lastnames": _extract_lastnames(authors),
            "tags": tags,
            "tags_lower": _lower_all(tags),
            "year": year,
            "item_type": item_type,
            "item_version": item_version,
            "zotero_modified": zotero_modified,
        }
        return self.update_item_metadata(library_id, item_key, fields)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_vector_store.py -v`
Expected: all tests PASS (the new one plus all pre-existing ones in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/db/vector_store.py backend/tests/test_vector_store.py
git commit -m "feat: add update_item_bibliographic_metadata for cheap metadata patches"
```

---

## Task 2: `DocumentProcessor._try_metadata_only_update()`

**Files:**
- Modify: `backend/services/document_processor.py` (add method near `_add_catalog_stub`, currently at line 1500)
- Modify: `backend/tests/test_document_processor.py` (new tests; add `get_item_chunks` default to two `setUp` methods)
- Modify: `backend/tests/test_incremental_indexing.py` (add `get_item_chunks` default to `asyncSetUp`)

**Interfaces:**
- Consumes: `self.vector_store.get_item_chunks(library_id, item_key) -> list[dict]` (existing, returns `[{"id": ..., "payload": {...}}, ...]`); `self.vector_store.update_item_bibliographic_metadata(...)` (Task 1); `self.zotero_client.get_item_children(library_id, item_key, library_type) -> list[dict]` (existing); `self._extract_authors`, `self._extract_tags`, `self._extract_year` (existing, `document_processor.py:1461,1489,1475`); module-level `INDEXABLE_MIME_TYPES` (existing, `document_processor.py:56-61`).
- Produces: `DocumentProcessor._try_metadata_only_update(item: dict, library_id: str, library_type: str) -> bool` (async). Tasks 3–5 call this inside their `elif existing_version < item_version:` branch, before the existing `delete_item_chunks` call.

- [ ] **Step 1: Add the `get_item_chunks` test default (needed before any test below can pass cleanly)**

In `backend/tests/test_document_processor.py`, `TestDocumentProcessor.setUp` (around line 70), change:

```python
        # Default stubs for catalog-only stub records (non-indexable items)
        self.mock_vector_store.get_stub_item_keys.return_value = set()
```

to:

```python
        # Default stubs for catalog-only stub records (non-indexable items)
        self.mock_vector_store.get_stub_item_keys.return_value = set()

        # Default: no existing chunks, so _try_metadata_only_update returns
        # False immediately and every existing test keeps exercising the
        # normal delete+reindex path unless it explicitly overrides this.
        self.mock_vector_store.get_item_chunks.return_value = []
```

In the same file, `TestSubprocessBatchIndexing.setUp` (around line 1249), change:

```python
        self.mock_vector_store.get_stub_item_keys.return_value = set()
        self.mock_zotero_client.get_deleted_item_keys.return_value = []
```

to:

```python
        self.mock_vector_store.get_stub_item_keys.return_value = set()
        self.mock_vector_store.get_item_chunks.return_value = []
        self.mock_zotero_client.get_deleted_item_keys.return_value = []
```

In `backend/tests/test_incremental_indexing.py`, `TestIncrementalIndexing.asyncSetUp` (around line 34), change:

```python
        # Default stubs required by the smart-sync full mode
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {}
        self.mock_vector_store.get_stub_item_keys.return_value = set()
        self.mock_vector_store.get_item_version.return_value = None
```

to:

```python
        # Default stubs required by the smart-sync full mode
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {}
        self.mock_vector_store.get_stub_item_keys.return_value = set()
        self.mock_vector_store.get_item_version.return_value = None
        self.mock_vector_store.get_item_chunks.return_value = []
```

- [ ] **Step 2: Add `import hashlib` to the test file**

In `backend/tests/test_document_processor.py`, the top imports (currently lines 1-15) don't import `hashlib`. Add it:

```python
import unittest
import hashlib
from unittest.mock import AsyncMock, MagicMock, Mock, patch
```

- [ ] **Step 3: Write the failing tests**

Add these to `TestDocumentProcessor` in `backend/tests/test_document_processor.py`, placed after `test_extract_tags_empty` (before `test_extract_year_various_formats`):

```python
    async def test_metadata_only_update_skips_standalone_attachment(self):
        """Standalone attachments are out of scope — Zotero bumps their own
        version for both a metadata-only edit and a real file re-upload, and
        there's no cheap way to tell those apart."""
        item = {"version": 5, "data": {"key": "ATT1", "itemType": "attachment"}}

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)
        self.mock_vector_store.get_item_chunks.assert_not_called()

    async def test_metadata_only_update_returns_false_when_no_existing_chunks(self):
        item = {"version": 5, "data": {"key": "ITEM1", "itemType": "book"}}
        self.mock_vector_store.get_item_chunks.return_value = []

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_returns_false_for_catalog_stub(self):
        item = {"version": 5, "data": {"key": "ITEM1", "itemType": "book"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {"has_content": False}},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_abstract_fallback_unchanged(self):
        """Abstract-fallback item whose abstractNote text hasn't changed —
        must patch metadata in place, not re-chunk/re-embed the abstract."""
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "tags": [{"tag": "Law"}],
                "date": "2020",
                "abstractNote": abstract_text,
                "dateModified": "2026-01-01T00:00:00Z",
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertTrue(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=["Law"],
            year=2020, item_type="journalArticle", item_version=10,
            zotero_modified="2026-01-01T00:00:00Z",
        )
        self.mock_zotero_client.get_item_children.assert_not_called()

    async def test_metadata_only_update_abstract_fallback_changed(self):
        """If the abstract text itself changed, that's a content change —
        must fall through to the normal reindex path."""
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1", "itemType": "journalArticle",
                "abstractNote": "a completely different abstract " * 20,
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": "stale-hash-from-before",
                "has_content": True,
            }},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_not_called()

    async def test_metadata_only_update_attachment_unchanged(self):
        """Attachment-backed item whose attachment version(s) haven't
        changed — must patch metadata in place."""
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1", "itemType": "journalArticle", "title": "New Title",
                "creators": [], "tags": [],
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 7},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertTrue(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=[], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )

    async def test_metadata_only_update_attachment_version_changed(self):
        item = {"version": 10, "data": {"key": "ITEM1", "itemType": "journalArticle"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 8},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_attachment_added(self):
        """A new indexable attachment appeared — content changed, not just metadata."""
        item = {"version": 10, "data": {"key": "ITEM1", "itemType": "journalArticle"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 7},
            {"data": {"key": "PDF2", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 1},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_attachment_removed(self):
        """An indexed attachment is no longer present — content changed."""
        item = {"version": 10, "data": {"key": "ITEM1", "itemType": "journalArticle"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
            {"id": "p2", "payload": {
                "attachment_key": "PDF2", "attachment_version": 1, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 7},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_document_processor.py -k metadata_only_update -v`
Expected: FAIL with `AttributeError: 'DocumentProcessor' object has no attribute '_try_metadata_only_update'` (9 failures)

- [ ] **Step 5: Write minimal implementation**

In `backend/services/document_processor.py`, add this method directly after `_add_catalog_stub` (which ends at line 1517, the end of the file/class):

```python
    async def _try_metadata_only_update(
        self,
        item: dict,
        library_id: str,
        library_type: str,
    ) -> bool:
        """Attempt a cheap in-place metadata patch instead of a full reindex.

        Applies when an item's Zotero version increased but its indexed content
        (attachment bytes, or the fallback abstract text) is unchanged — i.e.
        only item-level fields (title, creators, tags, date, itemType) were
        edited. Standalone attachments and catalog-only stubs are out of scope
        (see the per-branch comments below) and always return False.

        Returns:
            True if the metadata patch was applied — caller must skip the
            delete-then-reindex path. False if a content change was detected,
            or the item isn't eligible, so the caller must fall through to
            the normal reindex path.
        """
        item_key = item["data"]["key"]

        # Standalone attachments (itemType == "attachment") ARE the indexable
        # unit, and Zotero bumps their own version for both a metadata-only
        # edit and a real file re-upload — no cheap signal distinguishes them
        # without downloading the file, so always fall through.
        if item["data"].get("itemType") == "attachment":
            return False

        existing_chunks = self.vector_store.get_item_chunks(library_id, item_key)
        if not existing_chunks:
            return False

        # Catalog-only stubs are already a cheap rewrite (see _add_catalog_stub);
        # this path is only for items with real indexed content.
        if any(not c["payload"].get("has_content", True) for c in existing_chunks):
            return False

        abstract_key = f"{item_key}:abstract"
        is_abstract_fallback = all(
            c["payload"].get("attachment_key") == abstract_key for c in existing_chunks
        )

        if is_abstract_fallback:
            abstract_text = item["data"].get("abstractNote", "")
            new_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
            if new_hash != existing_chunks[0]["payload"].get("content_hash"):
                return False
        else:
            stored_versions: dict[str, int] = {}
            for c in existing_chunks:
                att_key = c["payload"].get("attachment_key")
                if att_key is not None:
                    stored_versions[att_key] = c["payload"].get("attachment_version", 0)

            current_attachments = await self.zotero_client.get_item_children(
                library_id=library_id, item_key=item_key, library_type=library_type
            )
            current_indexable = {
                att["data"]["key"]: att.get("version", 0)
                for att in current_attachments
                if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
            }

            if set(stored_versions) != set(current_indexable):
                return False
            if any(current_indexable[key] != version for key, version in stored_versions.items()):
                return False

        self.vector_store.update_item_bibliographic_metadata(
            library_id,
            item_key,
            title=item["data"].get("title", "Untitled"),
            authors=self._extract_authors(item["data"]),
            tags=self._extract_tags(item["data"]),
            year=self._extract_year(item["data"]),
            item_type=item["data"].get("itemType"),
            item_version=item.get("version", 0),
            zotero_modified=item["data"].get("dateModified", ""),
        )
        logger.info(f"Metadata-only update for item {item_key} (version {item.get('version')})")
        return True
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_document_processor.py -k metadata_only_update -v`
Expected: all 9 tests PASS

- [ ] **Step 7: Run the full document-processor and incremental-indexing suites to confirm no regression**

Run: `uv run pytest backend/tests/test_document_processor.py backend/tests/test_incremental_indexing.py -v`
Expected: all tests PASS (the new default of `get_item_chunks.return_value = []` makes `_try_metadata_only_update` a no-op for every pre-existing test, since it's never called from production code yet — that's Tasks 3–5)

- [ ] **Step 8: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py backend/tests/test_incremental_indexing.py
git commit -m "feat: add _try_metadata_only_update decision logic (not yet wired in)"
```

---

## Task 3: Wire into `_index_library_incremental`

**Files:**
- Modify: `backend/services/document_processor.py:450-460` (the `elif existing_version < item_version:` branch inside `_index_library_incremental`)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Consumes: `self._try_metadata_only_update(item, library_id, library_type) -> bool` (Task 2).
- Produces: no new interface — `_index_library_incremental`'s returned stats dict gains no new keys; `items_updated` now also counts metadata-only patches, and `chunks_added` stays 0 for those.

- [ ] **Step 1: Write the failing test**

Add to `TestDocumentProcessor` in `backend/tests/test_document_processor.py`, placed after `test_incremental_writes_catalog_stub_for_non_indexable_item`:

```python
    async def test_incremental_metadata_only_update_skips_reindex(self):
        """When only item-level metadata changed (content untouched),
        incremental sync must patch payload fields in place instead of
        re-downloading and re-embedding the attachment."""
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        mock_item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "abstractNote": abstract_text,
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []
        self.mock_vector_store.get_item_version.return_value = 5
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.mock_vector_store.delete_item_chunks.assert_not_called()
        self.mock_extractor.extract_and_chunk.assert_not_called()
        self.mock_embedding_service.embed_batch.assert_not_called()
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["chunks_added"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_incremental_metadata_only_update_skips_reindex -v`
Expected: FAIL — `delete_item_chunks` was called (the old unconditional path still runs)

- [ ] **Step 3: Write minimal implementation**

In `backend/services/document_processor.py`, in `_index_library_incremental`, change (currently at line 450-460):

```python
                elif existing_version < item_version:
                    # Updated item - delete old chunks and reindex
                    logger.debug(f"Reindexing updated item {item_key} ({existing_version} -> {item_version})")
                    deleted = self.vector_store.delete_item_chunks(library_id, item_key)
                    chunk_count = await self._index_item(item, library_id, library_type)
                    chunks_deleted += deleted
                    if chunk_count == 0:
                        items_failed += 1
                    else:
                        items_updated += 1
                        chunks_added += chunk_count
```

to:

```python
                elif existing_version < item_version:
                    if await self._try_metadata_only_update(item, library_id, library_type):
                        items_updated += 1
                        continue
                    # Updated item - delete old chunks and reindex
                    logger.debug(f"Reindexing updated item {item_key} ({existing_version} -> {item_version})")
                    deleted = self.vector_store.delete_item_chunks(library_id, item_key)
                    chunk_count = await self._index_item(item, library_id, library_type)
                    chunks_deleted += deleted
                    if chunk_count == 0:
                        items_failed += 1
                    else:
                        items_updated += 1
                        chunks_added += chunk_count
```

(`continue` inside the enclosing `try` block still runs the loop's `finally` clause — which reports progress — before moving to the next item, exactly as a normal fall-through would.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_incremental_metadata_only_update_skips_reindex -v`
Expected: PASS

- [ ] **Step 5: Run the full incremental-related suites to confirm no regression**

Run: `uv run pytest backend/tests/test_document_processor.py backend/tests/test_incremental_indexing.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: skip reindex for metadata-only changes in incremental sync"
```

---

## Task 4: Wire into `_index_library_full`'s inline loop

**Files:**
- Modify: `backend/services/document_processor.py:783-793` (the `elif existing_version < item_version:` branch inside the inline processing loop of `_index_library_full`)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Consumes: `self._try_metadata_only_update(item, library_id, library_type) -> bool` (Task 2).
- Produces: no new interface — same stats semantics as Task 3, applied to the `settings.testing=True` inline path used by full sync.

- [ ] **Step 1: Write the failing test**

Add to `TestDocumentProcessor` in `backend/tests/test_document_processor.py`, placed after `test_full_sync_reindexes_item_that_gained_content_over_stub`:

```python
    async def test_full_sync_metadata_only_update_skips_reindex(self):
        """Full sync (inline path) must also take the metadata-only fast path."""
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        mock_item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "abstractNote": abstract_text,
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {"ITEM1": 5}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        result = await self.processor.index_library("test_lib")

        self.mock_vector_store.delete_item_chunks.assert_not_called()
        self.mock_extractor.extract_and_chunk.assert_not_called()
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["chunks_added"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_full_sync_metadata_only_update_skips_reindex -v`
Expected: FAIL — `delete_item_chunks` was called

- [ ] **Step 3: Write minimal implementation**

In `backend/services/document_processor.py`, in `_index_library_full`'s inline-processing loop, change (currently at line 783-793):

```python
                    elif existing_version < item_version:
                        logger.debug("Updated item %s (%s -> %s)", item_key, existing_version, item_version)
                        self.vector_store.delete_item_chunks(library_id, item_key)
                        chunk_count = await self._index_item(item, library_id, library_type)
                        if chunk_count == 0 and not await _item_has_indexed_content(
                            self.vector_store, library_id, item_key
                        ):
                            items_failed += 1
                        else:
                            items_updated += 1
                            chunks_added += chunk_count
```

to:

```python
                    elif existing_version < item_version:
                        if await self._try_metadata_only_update(item, library_id, library_type):
                            items_updated += 1
                            continue
                        logger.debug("Updated item %s (%s -> %s)", item_key, existing_version, item_version)
                        self.vector_store.delete_item_chunks(library_id, item_key)
                        chunk_count = await self._index_item(item, library_id, library_type)
                        if chunk_count == 0 and not await _item_has_indexed_content(
                            self.vector_store, library_id, item_key
                        ):
                            items_failed += 1
                        else:
                            items_updated += 1
                            chunks_added += chunk_count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_full_sync_metadata_only_update_skips_reindex -v`
Expected: PASS

- [ ] **Step 5: Run the full document-processor suite to confirm no regression**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: skip reindex for metadata-only changes in full sync inline path"
```

---

## Task 5: Wire into `_subprocess_index_batch` (the path production actually runs)

**Files:**
- Modify: `backend/services/document_processor.py:148-155` (the `elif existing < item_version:` branch inside `_subprocess_index_batch`'s inner `_run()`)
- Test: `backend/tests/test_document_processor.py`

**Interfaces:**
- Consumes: `processor._try_metadata_only_update(item, library_id, library_type) -> bool` (Task 2), called on the `DocumentProcessor` instance already constructed inside `_run()`.
- Produces: no new interface — same stats semantics as Tasks 3–4, applied to the subprocess-isolated path that `_index_library_full` dispatches to whenever `settings.testing=False` (i.e. always in production, per `SUBPROCESS_BATCH_SIZE > 0`).

- [ ] **Step 1: Write the failing test**

Add to `TestSubprocessIndexBatchFunction` in `backend/tests/test_document_processor.py`, placed after `test_reports_items_failed_for_zero_chunk_result` (before the `if __name__ == "__main__":` block):

```python
    def test_metadata_only_update_skips_reindex_in_subprocess_batch(self):
        """The subprocess worker must also take the metadata-only fast path —
        production always uses this code path for full sync
        (settings.testing=False)."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1", "itemType": "journalArticle", "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "abstractNote": abstract_text,
            },
        }
        mock_vector_store = MagicMock()
        mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=mock_vector_store):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None, testing=False, extractor_backend="kreuzberg",
                ocr_enabled=True, kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[item],
                library_id="test_lib",
                library_type="user",
                indexed_versions={"ITEM1": 5},
                zotero_api_key="fake-key",
                embedding_api_key="fake-embed-key",
            )

        mock_vector_store.delete_item_chunks.assert_not_called()
        mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["chunks_added"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_metadata_only_update_skips_reindex_in_subprocess_batch -v`
Expected: FAIL — `delete_item_chunks` was called

- [ ] **Step 3: Write minimal implementation**

In `backend/services/document_processor.py`, in `_subprocess_index_batch`'s inner `_run()`, change (currently at line 148-155):

```python
                    elif existing < item_version:
                        vector_store.delete_item_chunks(library_id, item_key)
                        n = await processor._index_item(item, library_id, library_type)
                        if n == 0 and not await _item_has_indexed_content(vector_store, library_id, item_key):
                            items_failed += 1
                        else:
                            chunks_added += n
                            items_updated += 1
```

to:

```python
                    elif existing < item_version:
                        if await processor._try_metadata_only_update(item, library_id, library_type):
                            items_updated += 1
                            continue
                        vector_store.delete_item_chunks(library_id, item_key)
                        n = await processor._index_item(item, library_id, library_type)
                        if n == 0 and not await _item_has_indexed_content(vector_store, library_id, item_key):
                            items_failed += 1
                        else:
                            chunks_added += n
                            items_updated += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_metadata_only_update_skips_reindex_in_subprocess_batch -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: skip reindex for metadata-only changes in subprocess batch worker"
```

---

## Task 6: Full regression run

**Files:** none (verification only)

- [ ] **Step 1: Run the entire backend test suite**

Run: `uv run pytest backend/tests/ -q --timeout=120 --deselect backend/tests/test_api_endpoints.py::TestLibraryAPIEndpoints::test_list_libraries`

(`test_list_libraries` is a pre-existing, unrelated failure confirmed via `git stash` before this plan's work started — not something this plan should fix.)

Expected: all tests pass (0 failures besides the deselected pre-existing one).

- [ ] **Step 2: If anything fails, fix forward with a new TDD cycle (new failing test → fix → pass) rather than editing a previous task's commit**

- [ ] **Step 3: Final commit if Step 2 produced any changes**

```bash
git add -A
git commit -m "fix: address regressions found in full test suite run"
```

---

## Deployment (explicit follow-up, not part of this plan's tasks)

Once all tasks pass locally, deploying to production follows CLAUDE.md's hotfix workflow: a thin patch `Dockerfile` copying only `backend/db/vector_store.py` and `backend/services/document_processor.py` onto the current `zotero-rag:latest` image, verified with `grep` for `_try_metadata_only_update` and `update_item_bibliographic_metadata` inside the built image, then `sudo systemctl restart zotero-rag.service`. This plan does not perform that step — get explicit confirmation first.
