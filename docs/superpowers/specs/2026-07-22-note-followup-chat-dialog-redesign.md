# Follow-Up Chat UI Redesign: Result Dialog Replaces Item-Pane Section

## 1. Why this supersedes part of the original design

[2026-07-22-note-followup-chat-design.md](2026-07-22-note-followup-chat-design.md) (the original spec) built the
follow-up chat UI as a `Zotero.ItemPaneManager` section (§5.7–5.9 of that
doc) attached to notes tagged `RAG Query Result`, auto-created on every
query. Live testing against the dev Zotero instance found this
structurally broken, not buggy:

- Confirmed via the running dev instance that the section registers
  correctly (`Zotero.ItemPaneManager.customSectionData.options` lists
  `zotero-rag@cboulanger.github.io-zotero-rag-chat`, no errors, correct
  `onItemChange`/tag gating).
- Zotero's `itemPane.js` routes any `item.isNote()` selection to a
  separate `<note-editor>` element (`renderNoteEditor()`), which never
  mounts `<item-details>` — the component that reads
  `ItemPaneManager.customSectionData`. Confirmed directly in the Zotero
  source (`chrome/content/zotero/elements/itemPane.js`).
- Custom sections *do* render for notes, but only inside the reader tab's
  context/sidebar pane (`contextPane.js`) — a surface standalone
  RAG-result notes never open in.

Net effect: the chat box and trash button could never have appeared for a
note selected in the main library pane, regardless of implementation
correctness. This doc replaces §5.7–5.9 of the original spec (the plugin
UI attachment point) with a dialog-based design. **§5.1–5.6 of the
original spec (all backend changes: `ContinuationAgent`,
`NeedsClarificationError`, router/orchestrator conversation-history
plumbing, `source_refs`, `get_chunks_by_ids`) are unaffected, already
implemented, and already merged** — this is a plugin-UI-only redesign.

## 2. Scope decisions (this redesign)

| Decision | Choice |
| --- | --- |
| Where the chat UI lives | The same singleton dialog window used to submit the original question (`chrome://zotero-rag/content/dialog.xhtml`, opened via `ZoteroRAG.openQueryDialog()`), transitioning from an "input state" to a "result state" in place — not a second window, not an item-pane section |
| Note creation | On-demand only, via an explicit "Save as Note" button in result state. No note is created automatically on submit |
| Trash-from-note button | Removed. It existed only to clean up notes that were created automatically whether wanted or not; since note creation is now explicit, there is nothing to clean up |
| Debug trace | No longer auto-embedded in the note. An "Export debug info" button (visible only when the initial turn's `result.trace` is present) saves it as a `.json` file via the toolkit's `FilePickerHelper` |
| Follow-up scope | Only the original question's turn can carry advanced options (model, similarity, sources, disable-routing, include-trace) — result-state follow-ups never expose that UI again, matching the existing router/orchestrator contract where `conversation_history` already carries forward the original `query_plan`/filters |
| State reset | Closing the dialog and reopening it (already a singleton, tracked via `ZoteroRAG._dialogWindow`) creates a fresh window/document, which naturally resets to input state — no explicit reset code |
| Citation links in the dialog | Same `zotero://select/...` / `zotero://open-pdf/...` href scheme already used in notes (confirmed to be a real registered Gecko protocol handler, not note-editor-specific). Result state adds an explicit delegated click handler for `a[href^="zotero://"]` as a defensive measure, since this is a privileged dialog window rather than the note-editor's iframe, and no such handler exists anywhere else to reuse |

## 3. Architecture

```text
ZoteroRAG.openQueryDialog(window)
        │  singleton: focuses existing window if already open
        ▼
dialog.xhtml — INPUT STATE (unchanged: question box, library list,
        │       advanced options, progress/status feedback)
        │  Submit → existing indexing-check + /api/query + two-phase
        │           mentions flow (unchanged)
        ▼
dialog.js switches to RESULT STATE in the same window/document:
        │  - input-state controls (library list, advanced options) hidden
        │  - rendered answer + bibliography (formatTurnHTML output)
        │  - follow-up question box + Submit
        │  - Save as Note / Export debug info / Close buttons
        ▼
Follow-up Submit → POST /api/query with conversation_history built from
        │           this dialog session's own turns array (no note.id
        │           involved yet if unsaved) → same two-phase mentions
        │           handling, same needs_clarification handling as today
        ▼
Save as Note (first click) → creates + tags + saves the Zotero note from
        │  every turn so far; subsequent follow-ups auto-append to it
        ▼
Close → discards session state if never saved (matches the original
         spec's already-approved "session-only" stance)
```

## 4. Component detail

### 4.1 `plugin/src/dialog.xhtml`

Add a result-state container (hidden by default, e.g. `#result-section`)
sibling to the existing `.dialog-content` input-state markup, containing:

- `#result-content` — the answer/bibliography HTML gets injected here.
- `#followup-input` — `<textarea>` for the next question.
- Result-state button row: `#save-note-button`, `#export-debug-button`
  (hidden unless a trace exists), `#result-submit-button`.

The existing `.dialog-buttons` row (`#cancel-button`/`#submit-button`) is
reused for input state; result state swaps in its own button row rather
than repurposing those IDs, to keep the two states' event wiring
independent and avoid a stale listener from one state firing in the
other.

### 4.2 `plugin/src/dialog.js`

- New instance state: `this.turns = []` (array of `ChatTurn`-shaped
  objects, mirroring the backend's `ChatTurn` model), `this.noteID =
  null`, `this.libraryIds = []`, `this.firstResult = null` (kept for its
  `trace` field, read by the export button).
- `submit()` (today ends at line ~1144 with an unconditional
  `createResultNote()` call and a 1s-delayed `window.close()`): after a
  successful (non-`needs_client_evidence`) result, instead of creating a
  note and closing, call a new `enterResultState(question, result)` that:
  1. Pushes the turn onto `this.turns`.
  2. Calls `switchToResultState()` (hides input-state DOM, shows
     result-state DOM).
  3. Renders `ZoteroRAG.formatTurnHTML(question, result, libraryMap)`
     into `#result-content` (same helper the note-append path already
     uses — see 4.3).
  4. Shows `#export-debug-button` iff `result.trace` is present.
- New `submitFollowUp()` (replacing what `chat-pane.js`'s
  `ChatPane.submitFollowUp` did, but operating on `this.turns` directly
  instead of a note-ID-keyed global map — see 4.4): builds the request
  from `this.turns`, reuses the exact same two-phase
  `needs_client_evidence` handling already written in `submit()` (factor
  the shared block into one method both call), appends the new turn to
  `this.turns` and to `#result-content`, and — if `this.noteID` is
  already set — also appends to the saved note (`note.setNote(existing +
  turnHtml); await note.saveTx()`).
- New `saveAsNote()`: builds the full note HTML from `this.turns` (loop
  `formatTurnHTML` per turn + the existing metadata footer from
  `formatNoteHTML`, minus its trace block — see 4.3), creates and tags
  the `Zotero.Item('note')` exactly as today's `createResultNote()` does
  (current collection, `RAG Query Result` tag, `_ensureRAGResultsSearch`,
  `zoteroPane.selectItem`), stores the resulting `note.id` into
  `this.noteID`, and disables/relabels the button (e.g. "Saved").
- New `exportDebugInfo()`: calls the toolkit's `FilePickerHelper` (already
  bundled — `toolkit.bundle.js`'s `FilePickerHelper`, exposed as
  `ztoolkit.FilePicker` per existing conventions used elsewhere in this
  file, e.g. `openFixUnavailableDialog`'s sibling dialogs) to save
  `JSON.stringify(this.firstResult.trace, null, 2)` to a user-chosen
  `.json` path.
- Delegated click handler on `#result-content` for
  `a[href^="zotero://"]`: `event.preventDefault()` then
  `Zotero.launchURL(anchor.href)` — a real, existing Zotero API
  (`chrome/content/zotero/xpcom/zotero.js`'s `this.launchURL = function
  (url) {...}`) that hands a URL to the platform's registered handler,
  used elsewhere in Zotero's own chrome code for exactly this purpose.

### 4.3 `plugin/src/zotero-rag.js`

- `formatNoteHTML()` (currently `zotero-rag.js:1828`): drop the
  `result.trace` appendix block (currently lines 1857–1861). Trace is
  export-only now, never embedded in note HTML.
- `createResultNote()` (currently `zotero-rag.js:1191`): no longer called
  from `dialog.js`'s `submit()`. Repurposed as the function
  `dialog.js`'s new `saveAsNote()` calls (still lives in `zotero-rag.js`
  since it's the piece that knows how to create/tag/save a Zotero item;
  only its caller and trigger point change — from "always, at submit
  time" to "on demand, from the result dialog"). Drop its
  `ChatPane.seedConversation(...)` call (§4.4 — no longer needed, since
  conversation state now lives in the dialog instance, not a note-ID-keyed
  map).
- `formatTurnHTML`, `buildLibraryMap`, `replaceCitationsInText`,
  `formatBibliographyHTML`, `mergeConsecutiveCitations`, `escapeHTML`:
  unchanged, reused as-is by both `dialog.js`'s result-state rendering and
  `createResultNote`'s full-note HTML assembly.

### 4.4 `plugin/src/chat-pane.js`

Deleted. Its only real logic (`seedConversation`/`getTurns`/`recordTurn`/
`buildFollowUpPayload`) was a thin wrapper around an in-memory
`Map<noteID, ChatTurn[]>` that existed specifically to let an item-pane
section (keyed by whichever note is currently selected) look up
conversation state on demand. With conversation state now owned directly
by the dialog instance that's doing the asking (`this.turns` in
`dialog.js`, §4.2), there is no second place that needs to look it up by
note ID — the indirection has no remaining purpose. `submitFollowUp`'s
logic (two-phase mentions handling, turn construction, note-append) moves
into `dialog.js` as described in §4.2.

### 4.5 `plugin/src/bootstrap.js`

Remove the `chat-pane.js` load (currently line 51) and the
`ChatPane.init({ pluginID: id })` call (currently line 57). `mentions.js`
continues to load eagerly (line 50) — `dialog.js` already depends on
`MentionSearch` for the two-phase protocol, both in the original
submit flow and the new `submitFollowUp`.

### 4.6 Locale strings (`plugin/src/locale/en-US/zotero-rag.ftl`)

Remove `zotero-rag-chat-header`, `zotero-rag-chat-sidenav`,
`zotero-rag-chat-trash-button` (lines 5–9 — the item-pane-section-specific
strings). Add strings for the new result-state controls (e.g.
`zotero-rag-result-save-note`, `zotero-rag-result-export-debug`,
`zotero-rag-result-followup-placeholder`) plus their German translations
in `plugin/src/locale/de/zotero-rag.ftl`, matching the existing bilingual
convention.

### 4.7 `typings/i10n.d.ts`

Regenerate via the plugin scaffold build after the `.ftl` changes, same as
last time (`0a6747f`).

## 5. What's explicitly unaffected

Nothing under `backend/` changes. `ContinuationAgent`, the router's
`conversation_history` rendering, `NeedsClarificationError`/
`NeedsClientEvidenceError`, `source_refs`/`chunk_id` plumbing,
`MetadataAgent`'s narrowing threshold, and `docs/query-routing.md`'s
documented protocol all stay exactly as implemented. This redesign only
changes which plugin-side file renders the conversation and where a note
gets created.

## 6. Testing plan

- **Plugin unit tests** (`plugin/test/`): delete `chat-pane.test.js`
  (module removed). Extend `dialog.test.js` (or a new
  `dialog-result-state.test.js` if `dialog.test.js` is already large) with:
  state-transition on successful submit (input-state DOM hidden,
  result-state DOM shown, correct HTML rendered); `submitFollowUp`
  request-payload construction (`conversation_history` built from
  `this.turns`, `force_fresh_retrieval` wiring); `saveAsNote()` creates a
  tagged note from accumulated turns and disables the button;
  `exportDebugInfo()` invoked only when a trace is present; a follow-up
  after `saveAsNote()` appends to the existing note rather than creating
  a second one.
- **Manual verification** (live dev Zotero + dev backend, per root
  `CLAUDE.md`'s "Live Query Debugging" section — use the `test-rag-plugin`
  group library): submit a question, confirm result state renders with
  working citation links (clicking one selects/opens the right item);
  ask a follow-up without saving, confirm nothing appears in the library
  yet; click Save as Note, confirm a tagged note appears with both turns;
  ask another follow-up, confirm it appends to that same note; check the
  include-trace checkbox on the original question, confirm Export shows
  up and produces a valid JSON file; confirm Export is absent when
  include-trace was unchecked; close the dialog without saving, reopen it,
  confirm it's back to a clean input state.
