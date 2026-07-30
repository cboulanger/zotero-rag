# Retrofit-Link Collection Filing & Native Related-Item Links — Design

## Overview

Two additions to the chapter-segmentation/book-linking pipeline
(`docs/chapter-segmentation.md`), touching script 3
(`backend/services/chapter_retrofit.py`, "retrofit-link existing
book/chapter pairs") and script 4 (`backend/services/chapter_upload.py`,
"segment & upload"):

1. **Opt-in collection-filing for retrofit-link.** Script 4 already files
   every newly-created chapter item into a `<target-collection>/<Author
   (Year)>` subcollection via `--target-collection` (default `"Book
   Chapters"`, always on). Script 3 currently does nothing with
   collections at all — it only links *existing*, already-catalogued items,
   which may already be organized however the user likes. This adds the
   same subcollection-filing capability to script 3, but **opt-in**
   (`--target-collection` defaults to `None` — off), since forcing existing
   items into a new collection structure by default would be a surprising
   change in behavior for current users of the script.

2. **Always-on Zotero-native "Related" links.** Both scripts currently
   express the book↔chapter relationship only via a project-specific
   `Extra`-field convention (`X-Contains`/`X-Contained-By`, read/written by
   `backend/services/chapter_link_store.py`). This is invisible in Zotero's
   own UI. This adds a second, independent representation: Zotero's native
   `relations.dc:relation` field (the "Related" tab in the Zotero client),
   set on both the book and the chapter whenever either script writes a
   link between them. This is **unconditional** — it happens in both
   scripts, every time a book↔chapter link is written, regardless of the
   `--target-collection` flag.

These two features are independent of each other but share enough
underlying reuse (both move/add logic to `chapter_link_store.py`, both
touch `commit_links()`) that they're specified and implemented together.

## Goals

- Let an admin running script 3 optionally organize linked chapters into a
  collection, exactly mirroring script 4's existing scheme, without
  duplicating `ensure_target_collection`/`author_year_label`.
- Make the book↔chapter relationship visible in Zotero's own UI (the
  Related tab), not just to this project's own retrieval/citation logic.
- Reuse existing, already-tested functions rather than duplicating them —
  the explicit reason this design relocates two functions out of
  `chapter_upload.py` into the shared `chapter_link_store.py` module.
- Preserve every existing behavior, test, and guarantee already established
  for scripts 3 and 4 (dry-run defaults, per-entry failure isolation, the
  single-combined-PATCH write pattern that avoids Zotero's
  `If-Unmodified-Since-Version` staleness conflict).

## Non-goals

- No change to how "already linked" is detected (`parse_links(...).
  contained_by`, based solely on the `Extra` field) — the native
  `relations` field is write-only from this pipeline's perspective, never
  read back to influence matching or linking logic.
- No change to script 4's existing default-on collection behavior — it
  keeps its own `--target-collection` default of `"Book Chapters"`.
- No UI/plugin-side changes — this is entirely backend/CLI/API.
- No collection-filing of the *book* side in script 3 — only the chapter
  is added to the target subcollection, mirroring script 4 exactly (the
  book's own collection membership is left untouched).

## Architecture

### `backend/services/chapter_link_store.py` (shared helpers module)

This module already holds every other piece of shared, cross-script logic
for the linking scheme (`parse_library_slug`, `format_chapter_id`,
`parse_links`, `write_links`). It gains four more:

```python
def zotero_item_uri(slug: str, item_key: str) -> str:
    """Zotero's native related-item URI: http://zotero.org/<slug>/items/<item_key>.
    `slug` is already in the exact "users/<id>" / "groups/<id>" form this
    URI scheme requires (see parse_library_slug) -- no extra API call
    needed to build it.
    """


def add_related_item(relations: dict, uri: str) -> dict:
    """Return a NEW relations dict with `uri` added to relations["dc:relation"],
    idempotently (no duplicate entries if called again with the same uri)
    and normalizing Zotero's string-or-list representation of a single
    relation to a list. Other relation types already present (e.g.
    owl:sameAs, used by Zotero's own duplicate-merge feature) are left
    untouched. Does not mutate the input dict.
    """


def author_year_label(authors: list[str], date: str) -> str:
    """[Relocated verbatim from chapter_upload.py, unchanged.]
    Build a short author-year label for a per-book subcollection name,
    e.g. "Miller (2023)" or "Smith et al. (1999)" (3+ authors).
    """


def ensure_target_collection(zotero_write_client, top_level_name: str, subcollection_name: str) -> tuple[str, str]:
    """[Relocated verbatim from chapter_upload.py, unchanged.]
    Find-or-create the top-level collection and its per-book
    subcollection, returning (top_level_key, subcollection_key).
    """
```

`add_related_item`'s idempotency mirrors `write_links`'s own idempotency
principle (already established for the `Extra`-field writes) — calling it
twice with the same URI is a no-op, which matters because `commit_links()`
can be re-run (e.g. after fixing an ambiguous match elsewhere) against
already-linked pairs.

### `backend/services/chapter_upload.py`

- Removes its own `author_year_label`/`ensure_target_collection`
  definitions; imports both from `chapter_link_store.py` instead. No
  behavior change — same functions, same call sites, same tests (just
  relocated).
- In `run()`'s per-chapter success path (where it currently sets
  `chapter_item["data"]["extra"]` via `write_links(..., contained_by=...)`
  and, separately, `book_item["data"]["extra"]` via `write_links(...,
  contains=...)`), add matching `relations` updates on both items, using
  `zotero_item_uri`/`add_related_item`:

  ```python
  chapter_item["data"]["relations"] = add_related_item(
      chapter_item["data"].get("relations", {}), zotero_item_uri(slug, book_key)
  )
  ...
  book_item["data"]["relations"] = add_related_item(
      book_item["data"].get("relations", {}), zotero_item_uri(slug, chapter_key)
  )
  ```

  Each is set on the same `item["data"]` dict that's about to be PATCHed
  via the single existing `update_item(...)` call for that item — zero
  extra network calls, zero risk of the two-sequential-PATCHes staleness
  bug already documented in this function for the extra-field/collection
  write.

### `backend/services/chapter_retrofit.py`

`commit_links()` gains a `target_collection: str | None = None` parameter:

```python
def commit_links(
    zotero_write_client, slug: str, would_link: list[dict],
    target_collection: str | None = None,
) -> dict:
```

Inside the existing per-entry `try` block, after building the `extra`
writes for both `book_item` and `chapter_item` (as today):

1. **Relations (always-on, unconditional):** set
   `book_item["data"]["relations"]` to include `zotero_item_uri(slug,
   chapter_key)`, and `chapter_item["data"]["relations"]` to include
   `zotero_item_uri(slug, book_key)` — same pattern as script 4 above.
2. **Collection (only if `target_collection` is not `None`):** call
   `ensure_target_collection(zotero_write_client, target_collection,
   author_year_label(...))` using `book_item["data"]`'s already-fetched
   `creators`/`date` (no extra fetch), and add the *chapter's* key to the
   resulting subcollection's membership on `chapter_item["data"]
   ["collections"]` (append-if-absent, same pattern as script 4). The
   book's own `collections` are untouched.

Both the relations write and the collection resolution/membership update
land in the *same* `update_item(chapter_item)` / `update_item(book_item)`
calls the function already makes — no new PATCH calls are introduced.

A failure anywhere in this sequence (link write, relations write,
collection resolution) is caught by the existing per-entry `except
Exception` and reported as a `failed` entry for that pair, exactly as
today — the batch continues.

`run()` gains a matching `target_collection: str | None = None` parameter,
threaded to whichever `commit_links()` call fires — both the fresh
`find_matches()` → `commit_links()` path and the `would_link`-replay
(`--input`) path get the feature for free, since both already funnel
through the same `commit_links()`.

### CLI (`scripts/retrofit_chapter_links.py`)

Adds `--target-collection` (default `None` — opt-in, unlike script 4's
`upload_chapters.py --target-collection` which defaults to `"Book
Chapters"`). No new validation needed (no interaction with `--input`/
`--commit`).

### API (`backend/api/chapter_linking.py`)

`RetrofitLinkRequest` gains `target_collection: str | None = None`,
threaded to `retrofit_run(..., target_collection=request.target_collection)`.

### Documentation (`docs/chapter-segmentation.md`)

Script 3's section documents `--target-collection` (mirroring how script
4's is documented) and the always-on native-relations behavior (a short
note — this isn't a flag, just documented behavior).

## Data flow

For a single book/chapter pair being linked (either via a fresh match or a
replayed `would_link` entry), `commit_links()`'s per-entry sequence
becomes:

1. Fetch `book_item`, `chapter_item` (unchanged).
2. Compute new `X-Contains` value for the book; write to
   `book_item["data"]["extra"]` (unchanged).
3. **New:** compute new `relations` value for the book (add chapter's
   URI); write to `book_item["data"]["relations"]`.
4. `update_item(book_item)` — one PATCH, both `extra` and `relations`
   changes included (unchanged call, extended payload).
5. Compute new `X-Contained-By` value for the chapter; write to
   `chapter_item["data"]["extra"]` (unchanged).
6. **New:** compute new `relations` value for the chapter (add book's
   URI); write to `chapter_item["data"]["relations"]`.
7. **New, only if `target_collection` given:** resolve/create the target
   subcollection; add chapter's key to `chapter_item["data"]
   ["collections"]` if absent.
8. `update_item(chapter_item)` — one PATCH, `extra` + `relations` +
   (optionally) `collections` changes included (unchanged call, extended
   payload).

Step ordering (book before chapter) is unchanged from today, preserving
the existing self-healing-on-partial-failure property.

## Error handling

Unchanged failure-isolation model: any exception anywhere in the
per-entry sequence above (including a `create_collection` network error,
or any relations-handling bug) is caught by `commit_links()`'s existing
`except Exception as exc:` and reported as a `failed` entry for that one
`chapter_key`/`book_key` pair; the rest of the batch continues
unaffected.

## Testing plan

- **`chapter_link_store.py`** (new unit tests):
  - `zotero_item_uri`: format matches `http://zotero.org/<slug>/items/<key>`
    for both `users/...` and `groups/...` slugs.
  - `add_related_item`: round-trip; calling twice with the same URI
    produces no duplicate; normalizes a pre-existing bare-string
    `dc:relation` value to a list before appending; leaves an unrelated
    relation type (e.g. `owl:sameAs`) untouched; does not mutate the input
    dict (returns a new one).
  - `author_year_label`/`ensure_target_collection`: existing tests move
    from `test_chapter_upload.py` to `test_chapter_link_store.py` (or a
    new shared location — decided at plan-writing time), unchanged
    assertions.
- **`chapter_retrofit.py`** (`test_chapter_retrofit.py`, extends
  `TestCommitLinks`):
  - `target_collection=None` (default): no `collections()`/
    `create_collection()` calls, `chapter_item["data"]["collections"]`
    unchanged — confirms the opt-in default is truly off.
  - `target_collection="Some Collection"`: `ensure_target_collection` is
    called with the right label; chapter's `collections` gains the
    subcollection key; book's `collections` is untouched; both writes
    (extra + collections) land in a single `update_item(chapter_item)`
    call, not two.
  - Relations are set on both `book_item`/`chapter_item` regardless of
    `target_collection` (always-on assertion).
  - A `create_collection` failure lands in `failed` for that entry, not an
    uncaught exception (mirrors the existing malformed-entry isolation
    test).
- **`chapter_upload.py`** (`test_chapter_upload.py`, extends
  `TestUploadRun`):
  - After a committed upload, both the new chapter's and the book's
    `relations["dc:relation"]` contain each other's `zotero_item_uri`.
- **CLI/API plumbing tests** for `--target-collection`/`target_collection`,
  mirroring the `would_link` plumbing-test pattern already established for
  `--input`/`would_link` in the prior caching plan.
- **One live/E2E check** (extending `test_chapter_linking_e2e.py`, or a
  manual MCP-bridge check against the `test-rag-plugin` group) confirming
  the `http://zotero.org/...` URI format genuinely round-trips through the
  real Zotero API and appears as a Related item — not just that the field
  was set to *some* value.

## Decisions log

(For a future reader wondering "why this instead of that.")

- **Opt-in for collection-filing, always-on for relations** — different
  defaults because collection-filing changes a user-visible organizational
  structure (surprising if silently turned on), while native relations are
  purely additive metadata with no risk of surprising a script-3 user who
  never asked for it to move their items anywhere.
- **Relocate to `chapter_link_store.py` rather than cross-import from
  `chapter_upload.py`** — keeps script-3/script-4 independent of each
  other (neither currently imports from the other), consistent with the
  existing pattern of putting cross-script-shared logic in
  `chapter_link_store.py`.
- **Relations helpers as siblings to `write_links()`, not merged into
  it** — `write_links()`'s contract is deliberately a pure string-in/
  string-out transform of the `Extra` field text; folding in `relations`
  (a dict on the wider `data` object, not the `Extra` string) would break
  that contract and its existing tests. Calling both alongside each other
  at each call site keeps each function single-purpose.
- **Relations is write-only, never read back** — keeps `find_matches()`'s
  "already linked" detection logic (and its test suite) completely
  unaffected by this change; the two link representations
  (`Extra`-field and native `relations`) are deliberately redundant, not
  cross-validated.

## Addendum: implementation deviated from "write both sides" (2026-07-30)

Live E2E testing (the real `test-rag-plugin` group, not the mocked unit
suite) surfaced a Zotero API behavior this design didn't account for:
**PATCHing item A's `relations` to add a `dc:relation` pointing at item B
makes Zotero's server auto-mirror the reverse relation onto item B**,
bumping B's version as a side effect of A's own PATCH — confirmed by a
standalone script that wrote only to A and then re-fetched B, which
already carried the mirrored link with no write of its own. The design
above (and its exact-code snippets in "New relations-writing logic," steps
3-8) call for computing and PATCHing `relations` explicitly on **both**
`book_item` and `chapter_item` for every link. Implemented literally, this
races the server's own mirrored write: the second explicit PATCH is built
from an item dict fetched *before* the first PATCH's side effect landed,
so it carries a stale `version` and gets rejected with a 412 ("Item has
been modified since specified version") — deterministically, on every real
link, not intermittently. The mocked unit tests never caught this because
mocks don't reproduce server-side auto-mirroring.

The actual implementation (`chapter_upload.py`'s `run()`,
`chapter_retrofit.py`'s `commit_links()`) writes `relations` explicitly on
only **one** side of each pair — the side whose PATCH happens first — and
re-fetches the other item immediately before *its* PATCH, picking up both
the current version and the already-mirrored relation for free. The net
result for a Zotero user is identical to the original design (both items
end up cross-linked in the Related tab); only the write strategy changed.
See the code comments at each re-fetch site for the mechanical detail.
