# Per-Item Book/Chapter Menu Actions

## Problem

`docs/chapter-segmentation.md` describes an admin-triggered, whole-library
batch pipeline for detecting chapter boundaries and linking books to
chapters. A first live test showed the automated detection/matching
quality is too unreliable (bad OCR/PDF text extraction, low-confidence
matches that are often wrong even at high reported confidence) to run
unattended across a whole library. What remains useful is *selective,
manually-reviewed* segmentation: a user picks one book they're willing to
spend time correcting, and a separate small tool to link (or create) a
parent book for a chapter that's catalogued standalone.

This feature adds two per-item actions, reachable from both the items-pane
right-click menu and the Tools menu, each under a "Zotero RAG: Tools"
submenu:

- **Segment Book** — runs the existing analyze → segment-upload pipeline
  against one selected `book` item, then opens the existing `/admin/review`
  web page to resolve the results.
- **Match Chapter** — for one selected `bookSection` item with no existing
  book link, shows a small native dialog with up to 5 candidate books
  ranked by title/year similarity, lets the user pick one (or create a new
  book item from the chapter's own metadata), and writes the link.

Both actions operate on a single selected item; batch/whole-library
operation is out of scope here (that's what the existing CLI/`/admin/run`
pipeline already does, and is precisely the workflow this feature avoids
depending on being reliable).

## Menu registration

New module `plugin/src/chapterActions.js`, wired up from the same
bootstrap entry point that registers the existing Tools-menu item
(`zotero-rag.js:404-418`).

Two registration points, both building the same submenu structure via
`ui.createElement` (the pattern already used for the Tools-menu item and
toolbar button at `zotero-rag.js:404-433`):

- **Items-pane context menu** (`zotero-itemmenu`) — not currently used
  anywhere in this plugin; this is a new registration point. A submenu
  (`menupopup` with `id="zotero-rag-itemmenu-tools"`) is appended as a
  child `menu` of the item context menu, containing two `menuitem`s:
  "Segment Book…" and "Match Chapter…".
- **Tools menu** (`menu_ToolsPopup`) — same submenu structure, appended
  alongside the existing single Tools-menu entry.

Both entry points share one `buildSubmenu(doc)` helper and one pair of
command handlers, so there is exactly one implementation of each action's
logic regardless of which menu triggered it. Both operate on
`ZoteroPane.getSelectedItems()`.

**Enablement**, evaluated on menu `popupshowing` (context menu) / on
demand (Tools menu, since Zotero's Tools menu doesn't fire a per-open
popupshowing scoped to selection the same way — reuse the same check
function either way):

- "Segment Book…" enabled only when exactly one item is selected and its
  item type is `book`. (The page-count check happens after invocation, not
  at menu-enablement time — see below.)
- "Match Chapter…" enabled only when exactly one item is selected, its
  item type is `bookSection`, and its Extra field has no `X-Contained-By`
  line already (reuse `parse_links`'s convention — see
  `backend/services/chapter_link_store.py:81-114` for the format this
  needs to recognize; the plugin-side check only needs to detect presence
  of the `X-Contained-By:` line, not fully parse it).

Both use `Zotero.getActiveZoteroPane()` / `storeAddedElement()` for
lifecycle cleanup, consistent with existing plugin UI registration.

## "Segment Book" flow

1. Resolve the selected `book` item's PDF attachment(s)
   (`item.getAttachments()` → filter to `contentType === 'application/pdf'`).
   No PDF attachment at all → `Zotero.alert(win, "Zotero RAG", "No
   book-type attachment found")` and stop.

2. **Page-count check, client-side only** (no backend round-trip, no
   request parameter — this is a local gate before any server call):

   ```js
   async function getPageCount(attachment) {
     const pages = await Zotero.FullText.getPages(attachment.id);
     if (pages?.total != null) return pages.total;
     const { totalPages } = await Zotero.PDFWorker.getFullText(attachment.id, 1);
     return totalPages;
   }
   ```

   `Zotero.FullText.getPages()` is a cheap SQLite read
   (`fulltextItems.totalPages`, populated by local indexing or full-text
   sync) and returns `undefined` for a never-indexed/never-synced item —
   in that case, fall back to `Zotero.PDFWorker.getFullText(attachment.id,
   1)`, which parses only enough of the PDF (via the bundled pdf.js worker)
   to report `document.numPages` accurately without extracting all pages'
   text.

   Threshold: new **Advanced Preferences** pref
   `extensions.zotero-rag.minBookPages` (default `50`), added to the
   existing declarative numeric-pref table pattern at
   `zotero-rag.js:107-131`. This value is never sent to the backend — it's
   a purely local decision about whether to bother invoking the pipeline
   at all.

   Below threshold (or no attachment clears it) → same "No book-type
   attachment found" alert and stop.

3. Show a Zotero progress window (`new Zotero.ProgressWindow()`, the
   pattern already used elsewhere for long-running plugin operations) and:
   - `POST /chapter-linking/analyze` with `{library_slug, api_key,
     item_keys: [key], enable_llm_fallback: true}` (LLM fallback default
     ON, per the request). Poll `GET /chapter-linking/jobs/{job_id}` until
     `status !== "processing"`.
   - On `status: "error"` → show the error in the progress window and
     stop.
   - On `status: "done"`, take the `result` (same shape `--output` writes
     for the CLI) and immediately `POST /chapter-linking/segment-upload`
     with `{library_slug, api_key, analyses: result, committed: false}`
     (dry run — this is what populates the review/commit queues; nothing
     is written to Zotero yet). Poll the resulting job to completion the
     same way.

4. On success, open the default browser at
   `<backend_url>/admin/review?library_slug=<slug>` (existing query-param
   pre-fill support, `admin_review.html:301-304`). No item-scoped
   filtering exists on that page yet — reimplementing the review UI (or
   adding item-scoped filtering to it) is explicitly deferred; the user
   will see the whole library's queue, which now includes this book's
   freshly-queued chapters.

`library_slug` and `api_key` are obtained the same way existing backend
calls in the plugin do: `Zotero.Prefs.get('extensions.zotero-rag.zoteroApiKey',
true)` for the key (`zotero-rag.js:241`); the slug needs the `users/<id>` /
`groups/<id>` form (see the new shared slug helper below — the existing
`getBackendLibraryId()` at `zotero-rag.js:813-819` produces a different
format, `u<id>`/`<id>`, meant for backend library-ID lookups, not this
Extra-field/URI convention).

## "Match Chapter" flow — fully client-side, no backend calls

Everything in this flow — candidate search, scoring, and link writing —
runs against the local Zotero database. No backend request, no API key
needed. This is a deliberate choice: the point of this tool is a fast,
always-available manual-correction path independent of the same
server-side matching logic that produced unreliable automated results
during the initial live test.

1. Read the selected `bookSection` item's `bookTitle`, `date`, and
   creators locally.

2. **Candidate search**: query the local library for all `book`-type items
   via `Zotero.Search`:

   ```js
   const s = new Zotero.Search();
   s.libraryID = item.libraryID;
   s.addCondition('itemType', 'is', 'book');
   const ids = await s.search();
   const books = Zotero.Items.get(ids);
   ```

3. **Scoring**: a new small utility module, `plugin/src/fuzzyMatch.js`,
   implementing a token-sort-ratio-style similarity (sort each title's
   words, join, normalized edit-distance ratio between the two strings) —
   deliberately mirroring the *shape* of `find_best_book_match`'s approach
   in `backend/services/chapter_retrofit.py` (title similarity + ±1-year
   tolerance) without sharing code, since no cross-runtime code sharing
   exists between the Python backend and plugin JS. No new npm dependency;
   this is ~20-30 lines. Score every candidate book, sort descending, take
   the top 5.

4. **Dialog** (new XUL/HTML dialog, `plugin/src/ui/matchChapterDialog.js`,
   following the project's existing dialog conventions — XUL `<dialog>`
   root with `html:` children, per CLAUDE.md's UI guidance):
   - Header showing the chapter's own title and its `bookTitle` field
     value, for visual comparison against the candidates.
   - A table (5 rows max) of candidates: book title, creators, year, score.
     Radio-selectable.
   - Actions: **Link Selected**, **Reject All** (closes with no write),
     **Create New Book Item** (see step 6).

5. **Link Selected** → write the link locally (new shared module
   `plugin/src/chapterLinks.js`, replicating the Extra-field/relations
   convention from `backend/services/chapter_link_store.py:1-78`):
   - Compute `slug` for the current library (new helper — see below).
   - On the book item: append (not overwrite) `X-Contains:
     <slug>:<chapter_key>` to its Extra field's comma-separated list; add
     `http://zotero.org/<slug>/items/<chapter_key>` to its
     `relations["dc:relation"]`.
   - On the chapter item: set `X-Contained-By: <slug>:<book_key>` in its
     Extra field.
   - `saveTx()` both items. **Needs verification during implementation**:
     whether Zotero's local relations storage auto-mirrors the reverse
     relation onto the chapter item the way the server does (per
     `chapter_retrofit.py:204-218`'s comment about server-side
     auto-mirroring) — if not, the chapter side's `relations` must be set
     explicitly too, via the same `addRelatedItem`-equivalent local API.
   - No plugin-authored code currently writes an item's Extra field or
     relations, but the vendored `zotero-plugin-toolkit` bundle
     (`plugin/src/toolkit.bundle.js`) already exposes `getExtraField`/
     `setExtraField` helpers (`:3450`, `:3486` — the latter wraps
     `item.getField("extra")` / `item.setField("extra", ...)` +
     `item.saveTx()`) that `chapterLinks.js` can call directly instead of
     hand-rolling Extra-field parsing on top of `item.getField`/`setField`.
     Relations currently only have a *read* precedent in this plugin
     (`zotero-rag.js:2700-2714`, `_tryFixViaRelations`, via
     `Zotero.Relations.linkedObjectPredicate` /
     `item.getRelationsByPredicate`) — writing a relation
     (`item.addRelatedItem(otherItem)` + `saveTx()`) has no existing
     precedent here and is new territory for this plugin, reinforcing the
     "needs verification" note above about local mirroring behavior.

6. **Create New Book Item**: creates a local `book` item with `title =
   chapter.bookTitle`, and `publisher`/`place`/`date`/`ISBN`/`language`
   copied over from the chapter item where present (the chapter's own
   creators are chapter authors, not book editors, so the new book item is
   created with no creators — the user fills those in manually afterward).
   Then runs the same link-write as step 5 against the new item.

### New shared helper: library slug

Both flows need a `users/<id>` / `groups/<id>` slug for the current
library — distinct from `getBackendLibraryId()`
(`zotero-rag.js:813-819`), which produces a different format for a
different purpose. New helper, e.g. `getZoteroSlug(libraryID)`:

```js
function getZoteroSlug(libraryID) {
  const library = Zotero.Libraries.get(libraryID);
  if (library.libraryType === 'user') return `users/${Zotero.Users.getCurrentUserID()}`;
  return `groups/${Zotero.Groups.getGroupIDFromLibraryID(libraryID)}`;
}
```

(`Zotero.Groups.getGroupIDFromLibraryID(libraryID)` —
`chrome/content/zotero/xpcom/data/groups.js:103-111` in the Zotero
source — returns the numeric group ID directly from the in-memory cache;
throws if the library isn't a known group, which can't happen here since
the item's own `libraryID` is used.)

## Backend changes

**None required.** "Segment Book" reuses `analyze` and `segment-upload`
exactly as they exist today, scoped via `item_keys`. "Match Chapter" is
fully client-side per the above — no new endpoint.

## Error handling

- No PDF / below page threshold → `Zotero.alert` with "No book-type
  attachment found", no backend call made.
- Backend job reports `status: "error"` → progress window shows the
  error message; no browser page opened.
- Network/backend unreachable during "Segment Book" → progress window
  shows a generic connection error (reuse whatever existing error
  presentation the plugin already uses for other backend calls).
- "Match Chapter" with zero local `book` items in the library → dialog
  shows an empty candidates table with "Reject All" / "Create New Book
  Item" still available.

## Testing

- `plugin/test/`: menu enablement logic (mocked selection: right item
  type/count, already-linked chapter excluded, etc.), `fuzzyMatch.js`
  scoring unit tests (Node test runner, pure function — no Zotero runtime
  needed), `chapterLinks.js` Extra-field read/modify/write round-trip
  tests (pure string logic, mockable).
- Manual live test against the `test-rag-plugin` group library
  (`groups/6297749`) for both flows end-to-end, per this project's live
  debugging conventions (CLAUDE.md's "Live Query Debugging" /
  `zotero_execute_js` via the MCP Zotero bridge).
- No backend test changes needed (no backend code changes).

## Known limitations / deferred work

- `/admin/review` isn't scoped to a single item/book — "Segment Book"
  always opens the whole-library queue. Deferred per the request
  ("instead of re-implementing the UI client-side, save this for a later
  iteration").
- The JS fuzzy-matching implementation in `fuzzyMatch.js` and the Python
  one in `chapter_retrofit.py` are two separate implementations of a
  similar idea; they are not required to produce identical scores, since
  "Match Chapter" is a human-reviewed tool, not an auto-commit gate.
- Whether Zotero's local relations API auto-mirrors the reverse relation
  (book→chapter write implying chapter→book read) the way the server does
  needs confirming during implementation; the design assumes it might not
  and calls this out as a checkpoint rather than asserting either way.
