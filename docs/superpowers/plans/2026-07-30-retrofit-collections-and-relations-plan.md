# Retrofit-Link Collection Filing & Native Relations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in collection-filing to script 3 (retrofit-link), reusing script 4's existing collection helpers instead of duplicating them, and make every book↔chapter link written by either script also set a native Zotero "Related" connection.

**Architecture:** Relocate `author_year_label`/`ensure_target_collection` from `chapter_upload.py` to the shared `chapter_link_store.py` module (already home to every other cross-script helper for this linking scheme). Add two new pure helpers there (`zotero_item_uri`, `add_related_item`) for the native-relations feature. Wire relations (always-on) and collection-filing (opt-in via `target_collection`) into `chapter_retrofit.py`'s `commit_links()`, and relations (always-on) into `chapter_upload.py`'s `run()`. Thread `target_collection` through the CLI and API layers.

**Tech Stack:** Python 3.12, `uv run pytest` (unittest-style test classes), `pyzotero`, FastAPI (Pydantic request models).

**Spec:** `docs/superpowers/specs/2026-07-30-retrofit-collections-and-relations-design.md`

---

## File Structure

- Modify: `backend/services/chapter_link_store.py` — add `zotero_item_uri`, `add_related_item`; relocate `author_year_label`, `ensure_target_collection` here.
- Modify: `backend/services/chapter_upload.py` — import relocated functions instead of defining them; wire relations into `run()`.
- Modify: `backend/services/chapter_retrofit.py` — wire relations into `commit_links()`; add opt-in `target_collection` to `commit_links()`/`run()`.
- Modify: `scripts/retrofit_chapter_links.py` — add `--target-collection`.
- Modify: `backend/api/chapter_linking.py` — add `target_collection` to `RetrofitLinkRequest`.
- Modify: `docs/chapter-segmentation.md` — document both additions.
- Test: `backend/tests/test_chapter_link_store.py`, `backend/tests/test_chapter_upload.py`, `backend/tests/test_chapter_retrofit.py`, `backend/tests/test_chapter_linking_api.py`, `backend/tests/test_chapter_linking_e2e.py`.

---

### Task 1: `chapter_link_store.py` — native-relations helpers

**Files:**
- Modify: `backend/services/chapter_link_store.py`
- Test: `backend/tests/test_chapter_link_store.py`

- [ ] **Step 1: Write the failing tests**

Add to the top of `backend/tests/test_chapter_link_store.py`'s import block:

```python
from backend.services.chapter_link_store import (
    ChapterLinks,
    add_related_item,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
    zotero_item_uri,
)
```

Add two new test classes at the end of the file, before `if __name__ == "__main__":`:

```python
class TestZoteroItemUri(unittest.TestCase):
    def test_group_slug(self):
        self.assertEqual(
            zotero_item_uri("groups/6297749", "ABCD1234"),
            "http://zotero.org/groups/6297749/items/ABCD1234",
        )

    def test_user_slug(self):
        self.assertEqual(
            zotero_item_uri("users/12345", "WXYZ5678"),
            "http://zotero.org/users/12345/items/WXYZ5678",
        )


class TestAddRelatedItem(unittest.TestCase):
    def test_adds_to_empty_relations(self):
        result = add_related_item({}, "http://zotero.org/groups/1/items/AAAA1111")
        self.assertEqual(result, {"dc:relation": ["http://zotero.org/groups/1/items/AAAA1111"]})

    def test_idempotent_no_duplicate(self):
        once = add_related_item({}, "http://zotero.org/groups/1/items/AAAA1111")
        twice = add_related_item(once, "http://zotero.org/groups/1/items/AAAA1111")
        self.assertEqual(twice["dc:relation"], ["http://zotero.org/groups/1/items/AAAA1111"])

    def test_normalizes_existing_bare_string_to_list(self):
        result = add_related_item(
            {"dc:relation": "http://zotero.org/groups/1/items/OLD0000"},
            "http://zotero.org/groups/1/items/NEW1111",
        )
        self.assertEqual(
            result["dc:relation"],
            ["http://zotero.org/groups/1/items/OLD0000", "http://zotero.org/groups/1/items/NEW1111"],
        )

    def test_preserves_unrelated_relation_types(self):
        result = add_related_item(
            {"owl:sameAs": ["http://zotero.org/groups/1/items/DUPE0000"]},
            "http://zotero.org/groups/1/items/NEW1111",
        )
        self.assertEqual(result["owl:sameAs"], ["http://zotero.org/groups/1/items/DUPE0000"])
        self.assertEqual(result["dc:relation"], ["http://zotero.org/groups/1/items/NEW1111"])

    def test_does_not_mutate_input(self):
        original = {"dc:relation": ["http://zotero.org/groups/1/items/OLD0000"]}
        add_related_item(original, "http://zotero.org/groups/1/items/NEW1111")
        self.assertEqual(original["dc:relation"], ["http://zotero.org/groups/1/items/OLD0000"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_link_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'zotero_item_uri'`.

- [ ] **Step 3: Implement the helpers**

In `backend/services/chapter_link_store.py`, add this after `format_chapter_id` (before `def parse_links(`):

```python
_DC_RELATION = "dc:relation"


def zotero_item_uri(slug: str, item_key: str) -> str:
    """Zotero's native related-item URI (the "Related" tab in the Zotero
    client): http://zotero.org/<slug>/items/<item_key>. `slug` is already
    in the exact "users/<id>" / "groups/<id>" form this URI scheme
    requires -- no extra API call needed to build it. Distinct from this
    module's own X-Contains/X-Contained-By Extra-field convention above,
    which exists for RAG retrieval-suppression and is never derived from
    or cross-checked against `relations`.
    """
    return f"http://zotero.org/{slug}/items/{item_key}"


def add_related_item(relations: dict, uri: str) -> dict:
    """Return a NEW relations dict with `uri` added to
    relations["dc:relation"], idempotently (no duplicate entries if called
    again with the same uri) and normalizing Zotero's string-or-list
    representation of a single relation to a list. Other relation types
    already present (e.g. owl:sameAs, used by Zotero's own duplicate-merge
    feature) are left untouched. Does not mutate the input dict.
    """
    relations = dict(relations or {})
    existing = relations.get(_DC_RELATION, [])
    if isinstance(existing, str):
        existing = [existing] if existing else []
    if uri not in existing:
        existing = [*existing, uri]
    relations[_DC_RELATION] = existing
    return relations
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_link_store.py -v`
Expected: PASS — all tests, including the 7 new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_link_store.py backend/tests/test_chapter_link_store.py
git commit -m "feat: add zotero_item_uri/add_related_item helpers for native relations"
```

---

### Task 2: Relocate `author_year_label`/`ensure_target_collection` to `chapter_link_store.py`

**Files:**
- Modify: `backend/services/chapter_link_store.py`
- Modify: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_link_store.py`, `backend/tests/test_chapter_upload.py`

Pure relocation — no behavior change. `chapter_upload.py`'s `run()` keeps calling the exact same functions, just imported from their new home.

- [ ] **Step 1: Move the tests**

In `backend/tests/test_chapter_upload.py`, delete the `TestAuthorYearLabel` and `TestEnsureTargetCollection` classes entirely (currently between `TestBuildBookSectionItemData` and `TestUploadRun`), and remove this now-unused import line:

```python
from backend.services.chapter_upload import author_year_label, ensure_target_collection
```

In `backend/tests/test_chapter_link_store.py`, extend the import block (from Task 1) to also pull in the two relocated names:

```python
from backend.services.chapter_link_store import (
    ChapterLinks,
    add_related_item,
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
    zotero_item_uri,
)
```

Add these two test classes (moved verbatim from `test_chapter_upload.py`, unchanged) right after `TestAddRelatedItem` (before `if __name__ == "__main__":`):

```python
class TestAuthorYearLabel(unittest.TestCase):
    def test_single_author(self):
        self.assertEqual(author_year_label(["Jane Miller"], "2023"), "Miller (2023)")

    def test_two_or_more_authors_uses_et_al(self):
        self.assertEqual(author_year_label(["Jane Smith", "John Doe", "Amy Lee"], "1999"), "Smith et al. (1999)")

    def test_no_authors_falls_back_to_untitled(self):
        self.assertEqual(author_year_label([], "2020"), "Unknown (2020)")


class TestEnsureTargetCollection(unittest.TestCase):
    def test_creates_top_level_and_subcollection_when_absent(self):
        zot = MagicMock()
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        top_key, sub_key = ensure_target_collection(zot, "Book Chapters", "Miller (2023)")
        zot.create_collection.assert_called()
        self.assertEqual(top_key, "TOPKEY01")

    def test_reuses_existing_collections(self):
        zot = MagicMock()
        zot.collections.return_value = [{"key": "TOPKEY01", "data": {"name": "Book Chapters"}}]
        zot.collections_sub.return_value = [{"key": "SUBKEY01", "data": {"name": "Miller (2023)"}}]

        top_key, sub_key = ensure_target_collection(zot, "Book Chapters", "Miller (2023)")
        self.assertEqual(top_key, "TOPKEY01")
        self.assertEqual(sub_key, "SUBKEY01")
        zot.create_collection.assert_not_called()
```

`TestEnsureTargetCollection` uses `MagicMock`, which `test_chapter_link_store.py` doesn't currently import — add to its top imports:

```python
from unittest.mock import MagicMock
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_link_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'author_year_label'` (not yet moved).

Run: `uv run pytest backend/tests/test_chapter_upload.py -v`
Expected: PASS (the classes were removed, not broken — this just confirms the file is still valid Python after the deletion; `TestUploadRun` still passes since `chapter_upload.py` itself is untouched so far).

- [ ] **Step 3: Move the functions**

In `backend/services/chapter_upload.py`, delete the `author_year_label` and `ensure_target_collection` function definitions entirely (currently between `build_book_section_item_data` and `async def run(`), and change the import block from:

```python
from backend.db.vector_store import _extract_lastnames
from backend.services.chapter_link_store import (
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
)
```

to:

```python
from backend.services.chapter_link_store import (
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
)
```

(The `_extract_lastnames` import is no longer needed directly in this file — only `author_year_label`, now relocated, used it.)

In `backend/services/chapter_link_store.py`, add this import at the top of the file (after the existing `import re` / `from dataclasses import ...` lines):

```python
from backend.db.vector_store import _extract_lastnames
```

Then add these two functions at the end of the file (after `write_links`):

```python
def author_year_label(authors: list[str], date: str) -> str:
    """Build a short author-year label for a per-book subcollection name,
    e.g. "Miller (2023)" or "Smith et al. (1999)" (3+ authors). Reuses the
    existing lastname-extraction helper already used for Qdrant author
    filtering, for consistency with how author names are normalized
    elsewhere in this codebase.
    """
    lastnames = _extract_lastnames(authors)
    year = date.strip().split("-")[0] if date else "n.d."
    if not lastnames:
        return f"Unknown ({year})"
    first = lastnames[0].capitalize()
    label = first if len(lastnames) == 1 else f"{first} et al."
    return f"{label} ({year})"


def ensure_target_collection(zotero_write_client, top_level_name: str, subcollection_name: str) -> tuple[str, str]:
    """Find-or-create the top-level collection and its per-book
    subcollection, returning (top_level_key, subcollection_key). Idempotent:
    re-running against an already-processed book reuses both collections.
    """
    top_matches = [c for c in zotero_write_client.collections() if c["data"]["name"] == top_level_name]
    if top_matches:
        top_key = top_matches[0]["key"]
    else:
        resp = zotero_write_client.create_collection([{"name": top_level_name}])
        top_key = list(resp["successful"].values())[0]["key"]

    sub_matches = [c for c in zotero_write_client.collections_sub(top_key) if c["data"]["name"] == subcollection_name]
    if sub_matches:
        sub_key = sub_matches[0]["key"]
    else:
        resp = zotero_write_client.create_collection([{"name": subcollection_name, "parentCollection": top_key}])
        sub_key = list(resp["successful"].values())[0]["key"]

    return top_key, sub_key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_link_store.py backend/tests/test_chapter_upload.py -v`
Expected: PASS — every test in both files, including the relocated ones. `TestUploadRun` in `test_chapter_upload.py` must still pass unmodified — it exercises `chapter_upload.run()`, which still calls `author_year_label`/`ensure_target_collection` (now imported, not defined locally), so its existing mocks (`zot.collections`, `zot.create_collection`, `zot.collections_sub`) still apply identically.

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_link_store.py backend/services/chapter_upload.py backend/tests/test_chapter_link_store.py backend/tests/test_chapter_upload.py
git commit -m "refactor: relocate author_year_label/ensure_target_collection to chapter_link_store.py"
```

---

### Task 3: `chapter_upload.py` — wire native relations into `run()` (always-on)

**Files:**
- Modify: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_upload.py`

- [ ] **Step 1: Write the failing test**

Add to `class TestUploadRun` in `backend/tests/test_chapter_upload.py`, after `test_commit_downloads_book_pdf_once_for_multiple_chapters`:

```python
    def test_commit_sets_native_relations_on_chapter_and_book(self):
        book_item, analysis = self._book_and_analysis()
        zot = MagicMock()
        zot.item.return_value = book_item

        def item_template_side_effect(item_type, **kwargs):
            if item_type == "attachment":
                return {"itemType": "attachment", "linkMode": kwargs.get("linkmode", ""), "title": "",
                        "filename": "", "contentType": "", "parentItem": ""}
            return {"itemType": "bookSection", "title": "", "bookTitle": "",
                    "publisher": "", "place": "", "date": "", "ISBN": "", "language": "",
                    "pages": "", "creators": []}

        zot.item_template.side_effect = item_template_side_effect
        zot.create_items.side_effect = [
            {"successful": {"0": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}}},
            {"successful": {"0": {"key": "ATT1", "data": {"key": "ATT1"}}}},
        ]
        zot.upload_attachments.return_value = {"success": [{"key": "ATT1"}], "failure": [], "unchanged": []}
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []
        read_client = MagicMock()
        read_client.get_attachment_file = unittest.mock.AsyncMock(return_value=b"%PDF-1.4 fake")

        with patch("backend.services.chapter_upload.slice_pdf_range", return_value=b"sliced bytes"):
            result = asyncio.run(upload_run(
                zotero_write_client=zot, zotero_read_client=read_client,
                slug="groups/1", analyses=[analysis], commit=True, confidence_threshold=0.8,
                target_collection="Book Chapters", max_items=None,
            ))

        chapter_key = result["created"][0]["chapter_key"]
        chapter_update_relations = zot.update_item.call_args_list[0][0][0]["data"]["relations"]
        book_update_relations = zot.update_item.call_args_list[1][0][0]["data"]["relations"]
        self.assertIn("http://zotero.org/groups/1/items/BOOK1", chapter_update_relations["dc:relation"])
        self.assertIn(f"http://zotero.org/groups/1/items/{chapter_key}", book_update_relations["dc:relation"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_upload.py::TestUploadRun::test_commit_sets_native_relations_on_chapter_and_book -v`
Expected: FAIL — `KeyError: 'relations'` (neither item's `data` dict has a `relations` key yet).

- [ ] **Step 3: Wire relations into `run()`**

In `backend/services/chapter_upload.py`, extend the import block (from Task 2's resulting state) to add the two new helpers:

```python
from backend.services.chapter_link_store import (
    add_related_item,
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
    zotero_item_uri,
)
```

Initialize a new list alongside the existing two, at the top of the per-book loop body (immediately before `for chapter in confident_chapters:`):

```python
        new_chapter_ids: list[str] = []
        new_chapter_keys: list[str] = []
        pdf_ranges: dict[str, tuple[int, int]] = {}
```

Replace the chapter-side extra write:

```python
                # Write the chapter side (X-Contained-By) from the CHAPTER's own
                # extra — never the book's extra.
                chapter_item = created_item
                chapter_id = format_chapter_id(slug, chapter_key)
                book_id = format_chapter_id(slug, book_key)
                chapter_extra = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
                chapter_item["data"]["extra"] = chapter_extra
```

with:

```python
                # Write the chapter side (X-Contained-By) from the CHAPTER's own
                # extra — never the book's extra.
                chapter_item = created_item
                chapter_id = format_chapter_id(slug, chapter_key)
                book_id = format_chapter_id(slug, book_key)
                chapter_extra = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
                chapter_item["data"]["extra"] = chapter_extra
                # Also set a native Zotero "relations" (dc:relation) connection
                # -- the "Related" tab in the Zotero client -- independent of
                # the Extra-field convention above and never read back by this
                # pipeline's own logic (see chapter_link_store.add_related_item).
                chapter_item["data"]["relations"] = add_related_item(
                    chapter_item["data"].get("relations", {}), zotero_item_uri(slug, book_key)
                )
```

Replace:

```python
                new_chapter_ids.append(chapter_id)
                pdf_ranges[chapter_id] = (chapter["pdf_start_index"], chapter["pdf_end_index"])
```

with:

```python
                new_chapter_ids.append(chapter_id)
                new_chapter_keys.append(chapter_key)
                pdf_ranges[chapter_id] = (chapter["pdf_start_index"], chapter["pdf_end_index"])
```

Replace the book-side write at the end of the per-book loop:

```python
        if new_chapter_ids:
            existing = parse_links(book_item["data"].get("extra", ""))
            merged_contains = list(dict.fromkeys([*existing.contains, *new_chapter_ids]))
            merged_ranges = {**existing.pdf_ranges, **pdf_ranges}
            book_item["data"]["extra"] = write_links(
                book_item["data"].get("extra", ""), contains=merged_contains, pdf_ranges=merged_ranges
            )
            zotero_write_client.update_item(book_item)
```

with:

```python
        if new_chapter_ids:
            existing = parse_links(book_item["data"].get("extra", ""))
            merged_contains = list(dict.fromkeys([*existing.contains, *new_chapter_ids]))
            merged_ranges = {**existing.pdf_ranges, **pdf_ranges}
            book_item["data"]["extra"] = write_links(
                book_item["data"].get("extra", ""), contains=merged_contains, pdf_ranges=merged_ranges
            )
            book_relations = book_item["data"].get("relations", {})
            for new_chapter_key in new_chapter_keys:
                book_relations = add_related_item(book_relations, zotero_item_uri(slug, new_chapter_key))
            book_item["data"]["relations"] = book_relations
            zotero_write_client.update_item(book_item)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_upload.py -v`
Expected: PASS — all tests, including the new one. `test_commit_creates_and_links` and `test_commit_downloads_book_pdf_once_for_multiple_chapters` must still pass unmodified (they don't assert on `relations`, so the new field being present doesn't affect them).

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "feat: set native Zotero relations on newly-uploaded chapters and their book"
```

---

### Task 4: `chapter_retrofit.py` — wire native relations into `commit_links()` (always-on)

**Files:**
- Modify: `backend/services/chapter_retrofit.py`
- Test: `backend/tests/test_chapter_retrofit.py`

- [ ] **Step 1: Write the failing test**

Add to `class TestCommitLinks` in `backend/tests/test_chapter_retrofit.py`, after `test_malformed_entry_is_isolated_as_failure`:

```python
    def test_sets_native_relations_on_both_items(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        self.assertIn(
            "http://zotero.org/groups/1/items/CHAP1",
            book_item["data"]["relations"]["dc:relation"],
        )
        self.assertIn(
            "http://zotero.org/groups/1/items/BOOK1",
            chapter_item["data"]["relations"]["dc:relation"],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py::TestCommitLinks::test_sets_native_relations_on_both_items -v`
Expected: FAIL — `KeyError: 'relations'`.

- [ ] **Step 3: Wire relations into `commit_links()`**

In `backend/services/chapter_retrofit.py`, change the import line from:

```python
from backend.services.chapter_link_store import format_chapter_id, parse_links, write_links
```

to:

```python
from backend.services.chapter_link_store import add_related_item, format_chapter_id, parse_links, write_links, zotero_item_uri
```

Replace `commit_links()`'s full body with:

```python
def commit_links(zotero_write_client, slug: str, would_link: list[dict]) -> dict:
    """Writes X-Contains/X-Contained-By links for an already-computed
    would_link list (find_matches()'s output, or a prior dry run's saved
    JSON replayed via the CLI's --input / the API's would_link field).
    Re-fetches only the two specific items involved in EACH link -- never
    the whole library -- which is what makes replaying a prior dry run's
    matches fast (design spec §7's write ordering/self-healing behavior is
    unchanged: book side written first, so a chapter-write failure after a
    successful book write just re-writes a no-op X-Contains on retry).

    Every written link also sets a native Zotero "relations" (dc:relation)
    connection between the two items -- the "Related" tab in the Zotero
    client -- independent of the Extra-field convention above and never
    read back by this pipeline's own link-detection logic (see
    chapter_link_store.add_related_item).
    """
    linked: list[dict] = []
    failed: list[dict] = []

    for entry in would_link:
        try:
            chapter_key = entry["chapter_key"]
            book_key = entry["book_key"]
            score = entry["score"]

            book_item = zotero_write_client.item(book_key)
            chapter_item = zotero_write_client.item(chapter_key)

            existing_links = parse_links(book_item["data"].get("extra", ""))
            chapter_id = format_chapter_id(slug, chapter_key)
            book_id = format_chapter_id(slug, book_key)
            new_contains = list(dict.fromkeys([*existing_links.contains, chapter_id]))
            book_item["data"]["extra"] = write_links(book_item["data"].get("extra", ""), contains=new_contains)
            book_item["data"]["relations"] = add_related_item(
                book_item["data"].get("relations", {}), zotero_item_uri(slug, chapter_key)
            )
            zotero_write_client.update_item(book_item)

            chapter_item["data"]["extra"] = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
            chapter_item["data"]["relations"] = add_related_item(
                chapter_item["data"].get("relations", {}), zotero_item_uri(slug, book_key)
            )
            zotero_write_client.update_item(chapter_item)
        except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
            failed.append({
                "chapter_key": entry.get("chapter_key", "?"),
                "book_key": entry.get("book_key", "?"),
                "error": str(exc),
            })
            continue

        linked.append({"chapter_key": chapter_key, "book_key": book_key, "score": score})

    return {"linked": linked, "failed": failed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v`
Expected: PASS — all tests, including the new one. Every pre-existing `TestCommitLinks`/`TestRetrofitRun` test must still pass (none assert on `relations`, so its presence doesn't interfere; `update_item.call_count` assertions stay at 2 since no new PATCH calls were added).

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: set native Zotero relations when retrofit-linking a book and chapter"
```

---

### Task 5: `chapter_retrofit.py` — opt-in `target_collection` for `commit_links()`/`run()`

**Files:**
- Modify: `backend/services/chapter_retrofit.py`
- Test: `backend/tests/test_chapter_retrofit.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestCommitLinks` in `backend/tests/test_chapter_retrofit.py`, after `test_sets_native_relations_on_both_items`:

```python
    def test_target_collection_none_by_default_no_collection_calls(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        zot.collections.assert_not_called()
        zot.create_collection.assert_not_called()
        self.assertNotIn("collections", chapter_item["data"])

    def test_target_collection_given_files_chapter_into_subcollection(self):
        zot = MagicMock()
        book_item = {
            "key": "BOOK1",
            "data": {"key": "BOOK1", "extra": "", "creators": [{"lastName": "Miller"}], "date": "2020"},
        }
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        commit_links(
            zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
            target_collection="My Collection",
        )

        # ensure_target_collection calls create_collection twice: once for
        # the top-level "My Collection", once for the "Miller (2020)" sub.
        self.assertEqual(zot.create_collection.call_count, 2)
        self.assertIn("collections", chapter_item["data"])
        self.assertNotIn("collections", book_item["data"])
        # Extra + relations + collections all folded into the SAME single
        # update_item(chapter_item) call -- confirmed by the call count
        # staying at 2 total (one for the book, one for the chapter).
        self.assertEqual(zot.update_item.call_count, 2)

    def test_collection_resolution_failure_isolated_as_per_entry_failure(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": "", "creators": [], "date": "2020"}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.collections.side_effect = Exception("network error")

        result = commit_links(
            zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
            target_collection="My Collection",
        )

        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("network error", result["failed"][0]["error"])
```

Add to `class TestRetrofitRun`, after `test_commit_with_empty_would_link_list_short_circuits_to_noop`:

```python
    def test_target_collection_threaded_through_to_commit_links(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        retrofit_run(
            zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None,
            commit=True, would_link=[{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
            target_collection="My Collection",
        )

        self.assertIn("collections", chapter_item["data"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v -k target_collection`
Expected: FAIL — `TypeError: commit_links() got an unexpected keyword argument 'target_collection'`.

- [ ] **Step 3: Add `target_collection` to `commit_links()` and `run()`**

In `backend/services/chapter_retrofit.py`, change the import line from:

```python
from backend.services.chapter_link_store import add_related_item, format_chapter_id, parse_links, write_links, zotero_item_uri
```

to:

```python
from backend.services.chapter_link_store import (
    add_related_item,
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_links,
    write_links,
    zotero_item_uri,
)
```

Replace `commit_links()`'s full body (from Task 4's resulting state) with:

```python
def commit_links(
    zotero_write_client, slug: str, would_link: list[dict], target_collection: str | None = None,
) -> dict:
    """Writes X-Contains/X-Contained-By links for an already-computed
    would_link list (find_matches()'s output, or a prior dry run's saved
    JSON replayed via the CLI's --input / the API's would_link field).
    Re-fetches only the two specific items involved in EACH link -- never
    the whole library -- which is what makes replaying a prior dry run's
    matches fast (design spec §7's write ordering/self-healing behavior is
    unchanged: book side written first, so a chapter-write failure after a
    successful book write just re-writes a no-op X-Contains on retry).

    Every written link also sets a native Zotero "relations" (dc:relation)
    connection between the two items -- the "Related" tab in the Zotero
    client -- independent of the Extra-field convention above and never
    read back by this pipeline's own link-detection logic (see
    chapter_link_store.add_related_item).

    If `target_collection` is given, the CHAPTER side of each written link
    is additionally filed into a `<target_collection>/<Author (Year)>`
    subcollection (created if absent, reused otherwise), mirroring
    chapter_upload.py's own collection-filing scheme -- the book's own
    collection membership is left untouched. Off by default: a retrofit
    run links items the caller has already organized themselves, so
    moving them into a new collection structure is opt-in only.
    """
    linked: list[dict] = []
    failed: list[dict] = []

    for entry in would_link:
        try:
            chapter_key = entry["chapter_key"]
            book_key = entry["book_key"]
            score = entry["score"]

            book_item = zotero_write_client.item(book_key)
            chapter_item = zotero_write_client.item(chapter_key)

            existing_links = parse_links(book_item["data"].get("extra", ""))
            chapter_id = format_chapter_id(slug, chapter_key)
            book_id = format_chapter_id(slug, book_key)
            new_contains = list(dict.fromkeys([*existing_links.contains, chapter_id]))
            book_item["data"]["extra"] = write_links(book_item["data"].get("extra", ""), contains=new_contains)
            book_item["data"]["relations"] = add_related_item(
                book_item["data"].get("relations", {}), zotero_item_uri(slug, chapter_key)
            )
            zotero_write_client.update_item(book_item)

            chapter_item["data"]["extra"] = write_links(chapter_item["data"].get("extra", ""), contained_by=book_id)
            chapter_item["data"]["relations"] = add_related_item(
                chapter_item["data"].get("relations", {}), zotero_item_uri(slug, book_key)
            )
            if target_collection is not None:
                label = author_year_label(
                    [c.get("lastName", "") for c in book_item["data"].get("creators", [])],
                    book_item["data"].get("date", ""),
                )
                _, sub_key = ensure_target_collection(zotero_write_client, target_collection, label)
                existing_collections = chapter_item["data"].get("collections", [])
                if sub_key not in existing_collections:
                    chapter_item["data"]["collections"] = [*existing_collections, sub_key]
            zotero_write_client.update_item(chapter_item)
        except Exception as exc:  # noqa: BLE001 - report and continue with other chapters
            failed.append({
                "chapter_key": entry.get("chapter_key", "?"),
                "book_key": entry.get("book_key", "?"),
                "error": str(exc),
            })
            continue

        linked.append({"chapter_key": chapter_key, "book_key": book_key, "score": score})

    return {"linked": linked, "failed": failed}
```

Replace `run()`'s full body with:

```python
def run(
    *,
    zotero_write_client,
    slug: str,
    item_keys: list[str] | None,
    max_items: int | None,
    commit: bool = False,
    would_link: list[dict] | None = None,
    target_collection: str | None = None,
) -> dict:
    """Core logic for script 3 (retrofit_chapter_links). Synchronous --
    pyzotero's client is itself synchronous. Defaults to dry-run -- `commit`
    must be explicitly True to write the `X-Contains`/`X-Contained-By`
    links to Zotero (mirrors chapter_upload.py's script 4 convention). See
    design spec §7.

    If `would_link` is given (e.g. a prior dry run's output, replayed via
    the CLI's --input flag or the API's `would_link` request field) AND
    commit=True, this skips the full-library fetch and matching pass
    entirely and goes straight to commit_links() -- this is what makes a
    commit run after a dry run fast: the full-library fetch is what
    dominates a fresh run's cost, not the fuzzy matching itself.
    `would_link` is ignored when commit=False; a dry run always matches
    fresh (there is nothing to preview if it just replayed a prior
    preview).

    Note `would_link=[]` (an empty list) still counts as "given" here --
    it takes the same fast path as a non-empty list, short-circuiting to
    an all-empty no-op result via commit_links(..., []) rather than
    falling back to a fresh full match. This is intentional: an empty
    would_link legitimately means "a prior dry run already determined
    there's nothing to link" and replaying that is correct. Only
    would_link=None triggers a fresh match.

    `target_collection`, when given, is passed straight through to
    commit_links() -- see its docstring for the opt-in collection-filing
    behavior. Has no effect when commit=False (a dry run never writes
    anything, including collection membership).
    """
    if commit and would_link is not None:
        result = commit_links(zotero_write_client, slug, would_link, target_collection)
        return {**result, "would_link": [], "ambiguous": [], "no_match": []}

    all_items = zotero_write_client.everything(zotero_write_client.items())
    matches = find_matches(all_items, item_keys, max_items)

    if not commit:
        return {
            "linked": [], "would_link": matches["would_link"],
            "ambiguous": matches["ambiguous"], "no_match": matches["no_match"], "failed": [],
        }

    result = commit_links(zotero_write_client, slug, matches["would_link"], target_collection)
    return {**result, "would_link": [], "ambiguous": matches["ambiguous"], "no_match": matches["no_match"]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_retrofit.py -v`
Expected: PASS — all tests, including the 4 new ones.

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: add opt-in target_collection filing to chapter_retrofit.commit_links()/run()"
```

---

### Task 6: CLI — add `--target-collection` to `scripts/retrofit_chapter_links.py`

**Files:**
- Modify: `scripts/retrofit_chapter_links.py`

No dedicated CLI test file exists for this script (established pattern — see the caching plan's Task 7).

- [ ] **Step 1: Add the flag and wire it through**

In `scripts/retrofit_chapter_links.py`, add this argument right after `--input`:

```python
    parser.add_argument(
        "--target-collection", default=None,
        help="Optional collection name to file linked chapters into (a "
             "<name>/<Author (Year)> subcollection is created/reused per "
             "book, mirroring upload_chapters.py's --target-collection). "
             "Off by default -- existing items' collection membership is "
             "left untouched unless this is given.",
    )
```

Add `target_collection=args.target_collection` to the `retrofit_run(...)` call:

```python
    result = retrofit_run(
        zotero_write_client=zot,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
        commit=args.commit,
        would_link=would_link,
        target_collection=args.target_collection,
    )
```

- [ ] **Step 2: Smoke-test locally (no network)**

Run: `uv run python scripts/retrofit_chapter_links.py --help`
Expected: exits 0, shows the new `--target-collection` flag with its help text alongside the existing flags, no crash.

- [ ] **Step 3: Commit**

```bash
git add scripts/retrofit_chapter_links.py
git commit -m "feat: add --target-collection to retrofit_chapter_links.py"
```

---

### Task 7: API — add `target_collection` to `RetrofitLinkRequest`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `class TestRetrofitEndpoint` in `backend/tests/test_chapter_linking_api.py`:

```python
    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_passes_target_collection_through(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={
                "library_slug": "groups/1", "api_key": "fake-write-key",
                "committed": True, "target_collection": "My Collection",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_run.call_args.kwargs["target_collection"], "My Collection")

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_target_collection_defaults_to_none(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "would_link": [], "ambiguous": [], "no_match": [], "failed": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(mock_run.call_args.kwargs["target_collection"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py::TestRetrofitEndpoint -v -k target_collection`
Expected: FAIL — `KeyError: 'target_collection'`.

- [ ] **Step 3: Add the field and thread it through**

In `backend/api/chapter_linking.py`, replace the `RetrofitLinkRequest` model with:

```python
class RetrofitLinkRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    max_items: int | None = None
    committed: bool = False
    # A prior dry run's `would_link` list (this endpoint's own response
    # shape, see JobStatusResponse.result). When given together with
    # committed=True, chapter_retrofit.run() replays it instead of
    # re-fetching and re-matching the whole library -- see run()'s
    # docstring in backend/services/chapter_retrofit.py.
    would_link: list[dict] | None = None
    # Optional collection name to file linked chapters into (mirrors the
    # CLI's --target-collection and upload_chapters.py's own
    # target_collection field). None (default) leaves existing items'
    # collection membership untouched.
    target_collection: str | None = None
```

Update the `_task()` closure inside `start_retrofit_link`:

```python
            result = await asyncio.to_thread(
                retrofit_run,
                zotero_write_client=zot,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                commit=request.committed,
                would_link=request.would_link,
                target_collection=request.target_collection,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS — all tests, including the 2 new ones.

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: accept target_collection in the retrofit-link API endpoint"
```

---

### Task 8: E2E coverage — native relations and `--target-collection`

**Files:**
- Modify: `backend/tests/test_chapter_linking_e2e.py`

- [ ] **Step 1: Add `zotero_item_uri` import and extend the existing dummy-entries test**

Add to the top imports of `backend/tests/test_chapter_linking_e2e.py`:

```python
from backend.services.chapter_link_store import zotero_item_uri
```

In `test_retrofit_link_dummy_entries`, insert these two assertions right after the existing `assert f"{LIBRARY_SLUG}:{matching_key}" in book_item["data"]["extra"]` line (before the `unmatched_item` checks):

```python
    chapter_uri = zotero_item_uri(LIBRARY_SLUG, matching_key)
    book_uri = zotero_item_uri(LIBRARY_SLUG, book_key)
    assert book_uri in chapter_item["data"].get("relations", {}).get("dc:relation", [])
    assert chapter_uri in book_item["data"].get("relations", {}).get("dc:relation", [])
```

- [ ] **Step 2: Add a new E2E test for `--target-collection`**

Add this test function right after `test_retrofit_link_replays_dry_run_via_input`:

```python
@pytest.mark.timeout(90)  # overrides pyproject.toml's global 30s -- real network + subprocess calls
def test_retrofit_link_with_target_collection(zot, cleanup, tmp_path):
    """--target-collection files the linked CHAPTER (not the book) into a
    <target>/<Author (Year)> subcollection, mirroring script 4's own
    collection-filing scheme -- exercised through the real CLI + a real
    Zotero library, including real collection create/cleanup."""
    item_keys, collection_keys = cleanup

    book_title = _unique("Dummy Collection Book")
    book_template = zot.item_template("book")
    book_template["title"] = book_title
    book_template["date"] = "2020"
    book_template["creators"] = [{"creatorType": "author", "firstName": "Jane", "lastName": "Miller"}]
    book_template["tags"] = [{"tag": _TEST_TAG}]
    book_resp = zot.create_items([book_template])
    book_key = list(book_resp["successful"].values())[0]["key"]
    item_keys.append(book_key)

    chapter_title = _unique("Dummy Collection Chapter")
    chapter_template = zot.item_template("bookSection")
    chapter_template["title"] = chapter_title
    chapter_template["bookTitle"] = book_title
    chapter_template["date"] = "2020"
    chapter_template["tags"] = [{"tag": _TEST_TAG}]
    chapter_resp = zot.create_items([chapter_template])
    chapter_key = list(chapter_resp["successful"].values())[0]["key"]
    item_keys.append(chapter_key)

    target_collection = _unique("Retrofit Chapters")
    commit_output = tmp_path / "retrofit_collection_commit.json"
    _run_script(
        "retrofit_chapter_links.py",
        [
            "--item-keys", chapter_key,
            "--commit",
            "--target-collection", target_collection,
            "--output", str(commit_output),
        ],
    )
    commit_result = json.loads(commit_output.read_text())
    assert not commit_result["failed"], commit_result["failed"]
    assert chapter_key in {e["chapter_key"] for e in commit_result["linked"]}

    top_matches = [c for c in zot.collections() if c["data"]["name"] == target_collection]
    assert top_matches, f"target collection {target_collection!r} was not created"
    top_key = top_matches[0]["key"]
    sub_collections = zot.collections_sub(top_key)
    collection_keys.extend(c["key"] for c in sub_collections)
    collection_keys.append(top_key)

    assert any(c["data"]["name"] == "Miller (2020)" for c in sub_collections), \
        f"expected a 'Miller (2020)' subcollection, got {[c['data']['name'] for c in sub_collections]}"
    sub_key = next(c["key"] for c in sub_collections if c["data"]["name"] == "Miller (2020)")

    chapter_item = zot.item(chapter_key)
    assert sub_key in chapter_item["data"].get("collections", [])

    book_item = zot.item(book_key)
    assert sub_key not in book_item["data"].get("collections", [])
```

- [ ] **Step 3: Run both against the real test library**

Run: `uv run pytest backend/tests/test_chapter_linking_e2e.py::test_retrofit_link_dummy_entries backend/tests/test_chapter_linking_e2e.py::test_retrofit_link_with_target_collection -v -s -m integration`
Expected: PASS — requires a write-scoped `ZOTERO_API_KEY` in `.env` for the `test-rag-plugin` group (see the file's own module docstring). Report the actual result honestly; if credentials aren't available in this environment, say so rather than assuming success.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_chapter_linking_e2e.py
git commit -m "test: add E2E coverage for native relations and --target-collection"
```

---

### Task 9: Documentation — update `docs/chapter-segmentation.md`

**Files:**
- Modify: `docs/chapter-segmentation.md`

- [ ] **Step 1: Document collection-filing and native relations in script 3's section**

In `docs/chapter-segmentation.md`, immediately after the existing paragraph that begins "`--input` re-fetches only the two specific items..." and ends "...a dry run always matches fresh." (right before the paragraph starting "The output reports four buckets"), insert:

```markdown
Every written link also sets a native Zotero "Related" connection between
the two items (visible in the Zotero client's Related tab), independent of
the `Extra`-field convention above. `--target-collection <name>`
optionally also files the *chapter* side of each written link into a
`<name>/<Author (Year)>` subcollection (created if it doesn't exist yet,
reused otherwise) — the same scheme Script 4 uses for its own newly
created chapters below, but off by default here, since Script 3 links
items you've already organized yourself.
```

- [ ] **Step 2: Document `target_collection` in the API section**

In the same file's "Running via the API" section, replace:

```markdown
...the retrofit-link endpoint's body
takes a `committed` flag, mirroring the CLI's `--commit` and defaulting to
the same dry-run behavior, plus an optional `would_link` field mirroring
the CLI's `--input`: pass a prior dry run's `would_link` response array
alongside `committed: true` to skip straight to writing those matches
instead of re-fetching and re-matching the whole library) — useful for
driving this from an external scheduler or admin tool instead of a shell.
```

with:

```markdown
...the retrofit-link endpoint's body
takes a `committed` flag, mirroring the CLI's `--commit` and defaulting to
the same dry-run behavior; an optional `would_link` field mirroring the
CLI's `--input` (pass a prior dry run's `would_link` response array
alongside `committed: true` to skip straight to writing those matches
instead of re-fetching and re-matching the whole library); and an optional
`target_collection` field mirroring the CLI's `--target-collection`) —
useful for driving this from an external scheduler or admin tool instead
of a shell.
```

- [ ] **Step 3: Commit**

```bash
git add docs/chapter-segmentation.md
git commit -m "docs: document retrofit-link --target-collection and native relations"
```

---

### Task 10: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full default test suite**

Run: `uv run pytest`
Expected: PASS — every test file under `backend/tests/`, excluding `integration`/`api`/`container`-marked tests.

- [ ] **Step 2: Run the Node test suites (unaffected by this plan, but confirm no accidental breakage)**

Run: `npm run test:node && npm run test:plugin`
Expected: PASS — this plan touches no `bin/` or `plugin/` files.

- [ ] **Step 3: Confirm the two live E2E tests from Task 8 still pass together with the rest of the integration suite**

Run: `uv run pytest backend/tests/test_chapter_linking_e2e.py -v -s -m integration`
Expected: PASS — all four tests in the file (`test_analyze_and_upload_chapters`, `test_ocr_attachments`, `test_retrofit_link_dummy_entries`, `test_retrofit_link_replays_dry_run_via_input`, `test_retrofit_link_with_target_collection`), against the dedicated `test-rag-plugin` group — safe to mutate freely per CLAUDE.md, no confirmation needed before running (unlike a real personal library).
