# Follow-Up Chat Dialog Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the item-pane-section follow-up chat UI (which cannot render for standalone notes — see the design doc) with a result state inside the existing query dialog: rendered answer + working citation links, a follow-up box, and on-demand Save-as-Note / Export-debug-info actions.

**Architecture:** `plugin/src/dialog.xhtml`/`dialog.js` gain a second UI state alongside today's question-input form. After a successful submit, the input controls are hidden and a result view (built from `zotero-rag.js`'s existing `formatTurnHTML`/`buildLibraryMap`) is shown in the same window. Conversation state (`this.turns`) lives directly on the `ZoteroRAGDialog` instance instead of a note-ID-keyed global map. `plugin/src/chat-pane.js` and its `Zotero.ItemPaneManager` section registration are deleted entirely. `zotero-rag.js`'s `formatNoteHTML`/`createResultNote` change from taking one `(question, result)` pair to taking the full `turns` array, since a saved note may already contain several turns by the time the user chooses to save it.

**Tech Stack:** Plain JS (`@ts-check` + JSDoc typing, no TypeScript compilation), Zotero's privileged XUL/HTML dialog windows, Node's built-in test runner (`node --test`), no new dependencies.

---

## Context for every task below

- Backend (`backend/`) is completely unaffected by this plan — see [2026-07-22-note-followup-chat-dialog-redesign.md](../specs/2026-07-22-note-followup-chat-dialog-redesign.md) §5. Do not touch any file under `backend/`.
- Baseline plugin test command: `node --test plugin/test/*.test.js` (currently 41 passing tests before this plan starts).
- `plugin/src/dialog.js` defines a single object literal `var ZoteroRAGDialog = {...}` (not a class) — existing tests call methods via `ZoteroRAGDialog.methodName.call(fakeThis, ...)` rather than instantiating anything (see `plugin/test/dialog.test.js`'s header comment). Follow this exact pattern for all new dialog.js tests.
- `plugin/src/zotero-rag.js` defines `class ZoteroRAGPlugin` and instantiates a singleton at the bottom. Tests load it into a `vm` context and pull the class out (see `plugin/test/zotero-rag.test.js`'s `loadPlugin()` helper) — reuse that exact helper for new tests in that file.
- Do not run `npm run plugin:build` / `scripts/build_plugin.py` for anything except the one explicit typings-regeneration step in Task 4 — per root `CLAUDE.md`, the dev server hot-reloads source changes directly.

---

### Task 1: `formatNoteHTML` and `createResultNote` accept the full turns array, and stop embedding the debug trace

**Files:**
- Modify: `plugin/src/zotero-rag.js:44-57` (the `QueryResult` JSDoc typedef)
- Modify: `plugin/src/zotero-rag.js:1795-1866` (`buildLibraryMap`, `formatNoteHTML`)
- Modify: `plugin/src/zotero-rag.js:1183-1241` (`createResultNote`)
- Test: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Fix the stale `QueryResult` JSDoc typedef**

The typedef at `zotero-rag.js:44-57` is missing fields the code already relies on (`status` is documented as only `"complete" | "needs_client_evidence"`, but `formatTurnHTML` already checks for `"needs_clarification"`; `clarification_message` and `source_refs` aren't documented at all). Fix it before changing anything else, since the new code you're about to write uses these fields explicitly:

```js
/**
 * @typedef {Object} QueryResult
 * @property {string} question - Original question
 * @property {string} answer - Generated answer
 * @property {string} answer_format - Format of answer: "text", "html", or "markdown"
 * @property {Array<SourceCitation>} sources - Source citations
 * @property {Array<string>} library_ids - Libraries queried
 * @property {string|null} [model_name] - LLM model used for answering
 * @property {Array<string>} [agents_used] - Agent(s) dispatched to answer
 * @property {Array<string>} [source_refs] - Opaque evidence refs this turn produced, echoed back on the next follow-up
 * @property {Record<string, number>} [library_document_counts] - Indexed document count per library ID
 * @property {Record<string, any>|null} [trace] - Full execution trace, populated when include_trace=true
 * @property {string} [status] - "complete" | "needs_client_evidence" | "needs_clarification"
 * @property {string|null} [clarification_message] - Human-readable narrowing prompt, populated when status is "needs_clarification"
 * @property {Array<{author: string, year: number|null, title_keywords: Array<string>}>} [citation_targets] - populated when status is "needs_client_evidence"
 * @property {any} [query_plan] - echo of the routing plan; present when status is "needs_client_evidence" or "needs_clarification" — pass through unchanged on resubmit
 */
```

- [ ] **Step 2: Write the failing test for `formatNoteHTML`'s new multi-turn signature**

Add to `plugin/test/zotero-rag.test.js` (near the existing `formatTurnHTML`/`createResultNote` tests, i.e. after line 560):

```js
test('formatNoteHTML joins multiple turns with a divider and appends a metadata footer built from the first turn', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 }, Groups: { getAll: () => [] } }, {}, {});
	plugin.version = '1.0.0';
	const turns = [
		{ question: 'Q1', result: { answer: 'A1', answer_format: 'text', sources: [], model_name: 'gpt', agents_used: ['rag'] } },
		{ question: 'Q2', result: { answer: 'A2', answer_format: 'text', sources: [], agents_used: ['continuation'] } },
	];
	const html = plugin.formatNoteHTML(turns, ['u12345']);

	assert.ok(html.includes('Q1') && html.includes('A1'));
	assert.ok(html.includes('Q2') && html.includes('A2'));
	assert.ok(html.includes('Model: gpt'), 'model comes from the first turn');
	assert.ok(html.includes('Agents: rag, continuation'), 'agents are the union across all turns');
	assert.ok(html.includes('Generated:'));
});

test('formatNoteHTML never embeds a debug trace — export is on-demand only', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 }, Groups: { getAll: () => [] } }, {}, {});
	plugin.version = '1.0.0';
	const turns = [{ question: 'Q', result: { answer: 'A', answer_format: 'text', sources: [], trace: { some: 'trace data' } } }];
	const html = plugin.formatNoteHTML(turns, ['u12345']);
	assert.ok(!html.includes('Debugging Trace'));
	assert.ok(!html.includes('trace data'));
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `formatNoteHTML` still expects `(question, result, libraryIDs)`, so `turns[0]` gets treated as `question` and the assertions on joined content/footer won't match.

- [ ] **Step 4: Replace `formatNoteHTML` (and update the `buildLibraryMap` JSDoc reference)**

Replace `zotero-rag.js:1795-1866` (the `buildLibraryMap` JSDoc comment through the end of `formatNoteHTML`) with:

```js
	/**
	 * Build a library-ID → {name, type} map for the given backend library IDs,
	 * annotated with document counts when available. Shared by formatNoteHTML()
	 * and dialog.js's result-state renderer.
	 * @param {Array<string>} libraryIDs - Backend library IDs
	 * @param {Record<string, number>} [libraryDocumentCounts] - Optional map of library ID to document count
	 * @returns {Map<string, LibraryInfo>}
	 */
	buildLibraryMap(libraryIDs, libraryDocumentCounts = {}) {
		/** @type {Map<string, LibraryInfo>} */
		const libraryMap = new Map();

		for (let id of libraryIDs) {
			const libraries = this.getLibraries();
			const lib = libraries.find((/** @type {Library} */ l) => l.id === id);
			if (lib) {
				libraryMap.set(id, {
					name: lib.name,
					type: lib.type
				});
			}
		}

		return libraryMap;
	}

	/**
	 * Format an entire conversation (one or more turns) as HTML for a note.
	 * Called on demand from the result dialog's "Save as Note" button, once
	 * every turn so far is known — not automatically at submit time. Never
	 * embeds a debug trace; that's exported on demand instead (see dialog.js's
	 * exportDebugInfo()).
	 * @param {Array<{question: string, result: QueryResult}>} turns - All turns
	 *   in the conversation so far, oldest first
	 * @param {Array<string>} libraryIDs - Libraries that were queried
	 * @returns {string} HTML content
	 */
	formatNoteHTML(turns, libraryIDs) {
		const timestamp = new Date().toLocaleString();
		const firstResult = turns[0].result;

		// Build map of library ID to library info for source URI generation
		const libraryMap = this.buildLibraryMap(libraryIDs, firstResult.library_document_counts);

		const counts = firstResult.library_document_counts || {};
		const libraryNames = Array.from(libraryMap.entries()).map(([id, info]) => {
			const n = counts[id];
			return n ? `${info.name} (${n} documents)` : info.name;
		}).join(', ');

		let html = `<div>`;
		html += turns.map(({ question, result }) => this.formatTurnHTML(question, result, libraryMap)).join('<hr/>');

		// Metadata footer, based on the first turn — the one that actually
		// chose a model/routing config; follow-ups reuse it, so it stays
		// representative of the whole conversation.
		html += `<hr/>`;
		html += `<p style="font-size: 0.9em; color: #666;">`;
		html += `<em>Generated: ${timestamp}<br/>`;
		html += `Libraries: ${this.escapeHTML(libraryNames)}<br/>`;
		if (firstResult.model_name) {
			html += `Model: ${this.escapeHTML(firstResult.model_name)}<br/>`;
		}
		const allAgents = [...new Set(turns.flatMap(t => t.result.agents_used || []))];
		if (allAgents.length > 0) {
			html += `Agents: ${this.escapeHTML(allAgents.join(', '))}<br/>`;
		}
		html += `Plugin: v${this.escapeHTML(this.version)}`;
		html += `</em></p>`;

		html += `</div>`;

		return html;
	}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS (the two new tests; the existing 41 tests still pass except `createResultNote seeds ChatPane...`, which Task 2 replaces).

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "$(cat <<'EOF'
refactor: formatNoteHTML takes the full turns array, drops the debug-trace appendix

Note creation is moving from automatic-at-submit-time to an on-demand
"Save as Note" action that can fire after several follow-ups already
happened in the dialog session, so the note-HTML builder needs every
turn, not just the first. The trace becomes an on-demand export instead
of an always-embedded appendix.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 2: `createResultNote` accepts the turns array and no longer references `ChatPane`

**Files:**
- Modify: `plugin/src/zotero-rag.js:1183-1241` (`createResultNote`)
- Test: `plugin/test/zotero-rag.test.js:562-609` (replace the existing `createResultNote seeds ChatPane...` test)

- [ ] **Step 1: Write the failing test**

Replace the existing test at `plugin/test/zotero-rag.test.js:562-609` (`test('createResultNote seeds ChatPane with the clarification message...')`) with:

```js
test('createResultNote creates a tagged note from every turn and does not reference ChatPane', async () => {
	/** @type {string[]} */
	const noteHtmls = [];
	const noteStub = {
		id: 'note1',
		libraryID: 1,
		setNote(/** @type {string} */ html) { noteHtmls.push(html); },
		addToCollection() {},
		addTag() {},
		async saveTx() {},
	};

	const zoteroPaneStub = {
		getSelectedLibraryID: () => 1,
		getSelectedCollection: () => undefined,
		selectItem: async () => {},
	};

	const zotero = {
		Libraries: { userLibraryID: 1, get: () => ({ name: 'My Library' }) },
		Groups: { getAll: () => [] },
		Item: function (/** @type {string} */ _type) { return noteStub; },
		getActiveZoteroPane: () => zoteroPaneStub,
	};

	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const plugin = loadPlugin(zotero, {}, {}, { Services: servicesStub });
	plugin.version = '1.0.0';

	const turns = [
		{ question: 'Q1', result: { answer: 'A1', answer_format: 'text', sources: [] } },
		{ question: 'Q2', result: { answer: 'A2', answer_format: 'text', sources: [] } },
	];

	const note = await plugin.createResultNote(turns, ['u12345']);

	assert.strictEqual(note, noteStub);
	assert.strictEqual(noteHtmls.length, 1);
	assert.ok(noteHtmls[0].includes('Q1') && noteHtmls[0].includes('Q2'));
});
```

Note: this test deliberately does **not** put `ChatPane` in the `extra` globals passed to `loadPlugin(...)`. If `createResultNote` still referenced `ChatPane.seedConversation(...)`, this test would fail with a `ReferenceError: ChatPane is not defined` (reading an undeclared identifier always throws, regardless of strict mode) — that failure itself is step 3's expected result, not a mistake.

- [ ] **Step 2: (already written above — this task's test *is* the replacement, not an addition)**

- [ ] **Step 3: Run the test to verify it fails**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `createResultNote` still takes `(question, result, libraryIDs)` and still calls `ChatPane.seedConversation(...)`, which throws `ReferenceError: ChatPane is not defined` in this test's context.

- [ ] **Step 4: Replace `createResultNote`**

Replace `zotero-rag.js:1183-1241` with:

```js
	/**
	 * Create a note in the current collection from an entire conversation.
	 * Called on demand from the result dialog's "Save as Note" button — not
	 * automatically at submit time.
	 * @param {Array<{question: string, result: QueryResult}>} turns
	 * @param {Array<string>} libraryIDs - Libraries that were queried
	 * @returns {Promise<*>} Created note item
	 * @throws {Error} If note creation fails
	 */
	async createResultNote(turns, libraryIDs) {
		const zoteroPane = Zotero.getActiveZoteroPane();
		if (!zoteroPane) {
			throw new Error('No active Zotero pane');
		}

		// Get current library/collection
		const libraryID = zoteroPane.getSelectedLibraryID();
		const collectionID = zoteroPane.getSelectedCollection()?.id;

		// Create standalone note
		const note = new Zotero.Item('note');
		if (libraryID !== null) {
			note.libraryID = libraryID;
		}

		// Format note content as HTML
		const html = this.formatNoteHTML(turns, libraryIDs);
		note.setNote(html);

		// Add to collection before saving so it's included in the same transaction
		if (collectionID) {
			// @ts-ignore - addToCollection exists on Zotero.Item at runtime
			note.addToCollection(collectionID);
		}

		note.addTag('RAG Query Result');
		// Save note
		await note.saveTx();
		await this._ensureRAGResultsSearch(note.libraryID);

		// Select the note in the main library view, so the user sees the note
		// they just asked to be saved.
		try {
			await zoteroPane.selectItem(note.id);
		} catch (e) {
			this.log(`[createResultNote] Failed to select note in library pane: ${e instanceof Error ? e.message : e}`);
		}

		return note;
	}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "$(cat <<'EOF'
refactor: createResultNote takes the full turns array, drops ChatPane

Note creation moves to an on-demand action fired from the result
dialog (Task 6+), which may already have several follow-up turns by
the time the user clicks Save — and the dialog now owns conversation
state directly instead of going through ChatPane's note-ID-keyed map.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 3: Delete `chat-pane.js` and its item-pane-section wiring

**Files:**
- Delete: `plugin/src/chat-pane.js`
- Delete: `plugin/test/chat-pane.test.js`
- Modify: `plugin/src/bootstrap.js:44-57`

- [ ] **Step 1: Confirm nothing else references `ChatPane` or `chat-pane.js`**

Run: `grep -rn "ChatPane\|chat-pane" plugin/src/*.js plugin/test/*.js`
Expected: only matches inside `plugin/src/chat-pane.js`, `plugin/test/chat-pane.test.js`, and `plugin/src/bootstrap.js` (the load + init call) — Tasks 1-2 already removed the two references in `zotero-rag.js`.

- [ ] **Step 2: Delete the files**

```bash
git rm plugin/src/chat-pane.js plugin/test/chat-pane.test.js
```

- [ ] **Step 3: Remove the bootstrap.js load and init call**

Replace `bootstrap.js:44-57`:

```js
	// Eager, plugin-lifetime scripts — loaded once at startup, not per dialog
	// window. mentions.js is also loaded separately inside dialog.xhtml for
	// that window's own separate scope; this is a second, independent load
	// into the plugin-lifetime scope, needed because chat-pane.js's
	// ChatPane.submitFollowUp calls MentionSearch.findMentionEvidence(...)
	// from this scope.
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');
	Services.scriptloader.loadSubScript(rootURI + 'chat-pane.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
	ZoteroRAG.init({ id, version, rootURI });
	ChatPane.init({ pluginID: id });
	Zotero.ZoteroRAG = ZoteroRAG;
```

with:

```js
	// Eager, plugin-lifetime script — loaded once at startup, not per dialog
	// window. dialog.js (loaded separately inside dialog.xhtml for that
	// window's own scope) depends on MentionSearch for its two-phase
	// "needs_client_evidence" protocol.
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
	ZoteroRAG.init({ id, version, rootURI });
	Zotero.ZoteroRAG = ZoteroRAG;
```

- [ ] **Step 4: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS, 41 tests total minus the 9 deleted `chat-pane.test.js` tests, i.e. the same count Task 1-2 left you with (no `chat-pane` tests should appear in the output at all).

- [ ] **Step 5: Commit**

```bash
git add -A plugin/src/bootstrap.js
git commit -m "$(cat <<'EOF'
refactor: remove chat-pane.js and its item-pane section registration

Zotero routes note selections to a separate <note-editor> element that
bypasses <item-details> — the component that reads
ItemPaneManager.customSectionData — so this section could never
actually render for a note in the main library pane. The follow-up
chat UI moves into the query dialog itself (see the dialog redesign
spec and the tasks that follow).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 4: Remove the obsolete locale strings and regenerate typings

**Files:**
- Modify: `plugin/src/locale/en-US/zotero-rag.ftl`
- Modify: `plugin/typings/i10n.d.ts` (regenerated, not hand-edited)

- [ ] **Step 1: Remove the three item-pane-section strings**

`plugin/src/locale/en-US/zotero-rag.ftl` currently reads:

```ftl
# Zotero RAG Plugin Localization (English)

zotero-rag-ask-question = Ask Question...

zotero-rag-chat-header = Follow-up Chat
zotero-rag-chat-sidenav =
    .tooltiptext = Follow-up Chat
zotero-rag-chat-trash-button =
    .tooltiptext = Move this note to Trash
```

Replace it with:

```ftl
# Zotero RAG Plugin Localization (English)

zotero-rag-ask-question = Ask Question...
```

(`plugin/src/locale/de/zotero-rag.ftl` never had these three strings translated — nothing to change there. Confirmed by `grep -n "zotero-rag-chat" plugin/src/locale/de/zotero-rag.ftl` returning no matches. The new result-state dialog buttons added in later tasks use plain hardcoded English text, matching `dialog.xhtml`'s own existing convention — it has no `data-l10n-id` usage anywhere today; confirmed via `grep -n "l10n" plugin/src/dialog.xhtml` returning no matches.)

- [ ] **Step 2: Regenerate typings**

Run: `npx zotero-plugin build`
Expected: completes without error; `plugin/typings/i10n.d.ts` loses the three `zotero-rag-chat-*` entries it gained when they were added (commit `0a6747f`).

- [ ] **Step 3: Verify the diff is typings-only and string-removal-only**

Run: `git diff --stat plugin/typings/i10n.d.ts plugin/src/locale/en-US/zotero-rag.ftl`
Expected: only these two files changed; no build artifacts from other directories got staged.

- [ ] **Step 4: Commit**

```bash
git add plugin/src/locale/en-US/zotero-rag.ftl plugin/typings/i10n.d.ts
git commit -m "$(cat <<'EOF'
chore: remove the deleted item-pane section's locale strings

zotero-rag-chat-header/sidenav/trash-button were only used by the
now-deleted ItemPaneManager section registration in chat-pane.js.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 5: Add result-state markup and CSS to `dialog.xhtml`

**Files:**
- Modify: `plugin/src/dialog.xhtml`

No test — this is markup-only (JS wiring and its tests come in later tasks). Verified visually in Task 15's manual verification.

- [ ] **Step 1: Add IDs to the existing input-state content and button rows**

In `dialog.xhtml`, change:

```html
  <div class="dialog-container">
    <div class="dialog-content">
```

to:

```html
  <div class="dialog-container">
    <div id="input-content" class="dialog-content">
```

and change:

```html
    <!-- Dialog buttons -->
    <div class="dialog-buttons">
```

to:

```html
    <!-- Dialog buttons -->
    <div id="input-buttons" class="dialog-buttons">
```

(These are the only two markup changes to existing elements — everything else in the input-state form is untouched.)

- [ ] **Step 2: Add the result-state CSS**

Immediately before the closing `</style>` tag (after the existing `.advanced-options[open] > summary::before { content: '\25BC  '; font-size: 10px; }` and `.advanced-options .form-group:last-child { margin-bottom: 0; }` rules), add:

```css
    #result-section { display: none; flex-direction: column; height: 100%; overflow: hidden; }
    #result-section.visible { display: flex; }
    #result-content { flex: 1; overflow-y: auto; padding-bottom: 10px; }
    #result-content h2:first-child { margin-top: 0; }
```

- [ ] **Step 3: Add the result-state markup**

Immediately after the closing `</div>` of `id="input-buttons"` (i.e. right before the `</div>` that closes `.dialog-container`, and right before `</body>`), add:

```html
    <!-- Result state: rendered answer + follow-up chat (shown after a successful submit) -->
    <div id="result-section">
      <div id="result-content"></div>
      <div class="form-group" style="flex-shrink: 0;">
        <textarea id="followup-input"
                  rows="3"
                  placeholder="Ask a follow-up question..."
                  class="question-input"></textarea>
      </div>
      <div class="dialog-buttons">
        <button id="export-debug-button" type="button" class="dialog-button" style="display: none; margin-right: auto;">Export Debug Info</button>
        <button id="save-note-button" type="button" class="dialog-button">Save as Note</button>
        <button id="result-close-button" type="button" class="dialog-button" title="Close this dialog">Close</button>
        <button id="result-submit-button" type="button" class="dialog-button primary">Submit</button>
      </div>
    </div>
```

- [ ] **Step 4: Commit**

```bash
git add plugin/src/dialog.xhtml
git commit -m "$(cat <<'EOF'
feat: add result-state markup to the query dialog

Hidden by default (#result-section has no .visible class yet) —
Task 9 makes dialog.js reveal it after a successful submit. No JS
wiring yet; this task is markup + CSS only.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 6: `dialog.js` — conversation state and `buildConversationHistory()`

**Files:**
- Modify: `plugin/src/dialog.js:70-81` (instance state, right after `abortController`)
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing test**

Add to `plugin/test/dialog.test.js` (after the existing `resolveZoteroLibraryID` tests):

```js
test('buildConversationHistory maps turns to the backend ChatTurn shape', () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const fakeThis = {
		turns: [
			{ question: 'Q1', result: { answer: 'A1', agents_used: ['rag'], source_refs: ['c1'], query_plan: { agents_to_use: ['rag'] } } },
			{ question: 'Q2', result: { status: 'needs_clarification', answer: '', clarification_message: 'Narrow it down.', agents_used: [], source_refs: [], query_plan: null } },
		],
	};
	const history = ZoteroRAGDialog.buildConversationHistory.call(fakeThis);
	assert.strictEqual(history.length, 2);
	assert.strictEqual(history[0].answer, 'A1');
	assert.strictEqual(history[1].answer, 'Narrow it down.'); // clarification_message, not the empty answer
	assert.deepStrictEqual(history[0].source_refs, ['c1']);
});

test('buildConversationHistory returns an empty array with no turns yet', () => {
	const ZoteroRAGDialog = loadDialogMethods();
	assert.deepStrictEqual(ZoteroRAGDialog.buildConversationHistory.call({ turns: [] }), []);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `ZoteroRAGDialog.buildConversationHistory` is not a function.

- [ ] **Step 3: Add the conversation state and the method**

In `dialog.js`, right after the `abortController` property (currently ending at line 71 with `abortController: null,`), add:

```js
	/**
	 * Turns in the current result-state conversation, in chronological order.
	 * Empty until the first submit completes; the window only ever returns
	 * to a fresh input state by being closed and reopened (a new document
	 * each time), so there's no separate "reset" for this.
	 * @type {Array<{question: string, result: QueryResult}>}
	 */
	turns: [],

	/** @type {number|null} Zotero note ID once "Save as Note" has been clicked; null until then. */
	noteID: null,

	/** @type {Array<string>} Backend library IDs used for the current conversation. */
	libraryIds: [],
```

Then, near the end of the file (a natural place is right after `resolveZoteroLibraryID`, currently ending around line 1479 — check the actual line after Task 1-3's edits since line numbers shift slightly), add:

```js
	/**
	 * Convert the current turns into the `conversation_history` shape the
	 * backend expects (see backend/models/conversation.py's ChatTurn).
	 * @returns {Array<Object>}
	 */
	buildConversationHistory() {
		return this.turns.map(({ question, result }) => ({
			question,
			answer: result.status === 'needs_clarification' ? result.clarification_message : result.answer,
			agents_used: result.agents_used || [],
			source_refs: result.source_refs || [],
			query_plan: result.query_plan || null,
		}));
	},
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
feat(dialog): add conversation state and buildConversationHistory()

First piece of the result-state dialog: turns/noteID/libraryIds live
directly on ZoteroRAGDialog now, replacing the note-ID-keyed map
chat-pane.js used to own.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 7: `dialog.js` — extract `runQuery()` (the two-phase mentions protocol, shared by the first submit and every follow-up)

**Files:**
- Modify: `plugin/src/dialog.js` (new method; `submit()` itself is NOT modified in this task — that's Task 9, once result-state methods exist to call)
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/dialog.test.js`:

```js
test('runQuery returns the result directly when no client evidence is needed', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const fakeThis = {
		plugin: { submitQuery: async () => ({ status: 'complete', answer: 'A', sources: [] }) },
	};
	const result = await ZoteroRAGDialog.runQuery.call(fakeThis, 'Q', ['1'], { minScore: 0.3 });
	assert.strictEqual(result.answer, 'A');
});

test('runQuery gathers mention evidence and resubmits once when the backend requests it', async () => {
	/** @type {any[]} */
	const submittedOptions = [];
	const fakeThis = {
		plugin: {
			submitQuery: async (/** @type {string} */ _q, /** @type {string[]} */ _ids, /** @type {any} */ opts) => {
				submittedOptions.push(opts);
				if (submittedOptions.length === 1) {
					return { status: 'needs_client_evidence', citation_targets: [{ author: 'X', year: null, title_keywords: [] }], query_plan: { agents_to_use: ['mentions'] } };
				}
				return { status: 'complete', answer: 'Resolved.', sources: [] };
			},
		},
		resolveZoteroLibraryID: (/** @type {string} */ id) => (id === 'u1' ? 1 : null),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		MentionSearch: { findMentionEvidence: async () => ({ items: [], truncated: false, total_candidates: 0 }) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const result = await ContextDialog.runQuery.call(fakeThis, 'Q', ['u1'], {});

	assert.strictEqual(result.answer, 'Resolved.');
	assert.strictEqual(submittedOptions.length, 2);
	assert.ok(submittedOptions[1].clientEvidence);
	assert.deepStrictEqual(submittedOptions[1].queryPlan, { agents_to_use: ['mentions'] });
});

test('runQuery calls the optional progress callback around the mentions round trip', async () => {
	/** @type {any[]} */
	const progressCalls = [];
	let calls = 0;
	const fakeThis = {
		plugin: {
			submitQuery: async () => {
				calls++;
				return calls === 1
					? { status: 'needs_client_evidence', citation_targets: [], query_plan: null }
					: { status: 'complete', answer: 'A', sources: [] };
			},
		},
		resolveZoteroLibraryID: () => null,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		MentionSearch: { findMentionEvidence: async () => ({ items: [], truncated: false, total_candidates: 0 }) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await ContextDialog.runQuery.call(fakeThis, 'Q', [], {}, (/** @type {number} */ pct, /** @type {string} */ label) => progressCalls.push({ pct, label }));

	assert.strictEqual(progressCalls.length, 2);
	assert.strictEqual(progressCalls[0].label, 'Searching local library');
	assert.strictEqual(progressCalls[1].label, 'Resubmitting query');
});

test('runQuery throws if the backend requests client evidence a second time', async () => {
	const fakeThis = {
		plugin: { submitQuery: async () => ({ status: 'needs_client_evidence', citation_targets: [], query_plan: null }) },
		resolveZoteroLibraryID: () => null,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		MentionSearch: { findMentionEvidence: async () => ({ items: [], truncated: false, total_candidates: 0 }) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await assert.rejects(
		() => ContextDialog.runQuery.call(fakeThis, 'Q', [], {}),
		/requested citation evidence a second time/
	);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `ZoteroRAGDialog.runQuery` is not a function.

- [ ] **Step 3: Add `runQuery()`**

Add this method to `dialog.js` (a good spot is right after `buildConversationHistory()` from Task 6):

```js
	/**
	 * Submit a question to the backend, transparently handling the two-phase
	 * "needs_client_evidence" mentions protocol (gathers local full-text
	 * evidence and resubmits once, echoing back query_plan so the backend
	 * skips re-running the routing LLM call). Shared by submit() (the first
	 * turn) and submitFollowUp() (every later turn) so the protocol lives in
	 * one place.
	 * @param {string} question
	 * @param {Array<string>} libraryIds
	 * @param {QueryOptions} options
	 * @param {(percentage: number, label: string, message?: string) => void} [onProgress] - Optional UI progress callback for the mentions round trip
	 * @returns {Promise<QueryResult>}
	 */
	async runQuery(question, libraryIds, options, onProgress) {
		let result = await this.plugin.submitQuery(question, libraryIds, options);

		if (result.status === 'needs_client_evidence') {
			if (onProgress) onProgress(25, 'Searching local library', 'Scanning full text for citations...');
			const zoteroLibraryIDs = /** @type {Array<number>} */ (
				libraryIds
					.map((/** @type {string} */ id) => this.resolveZoteroLibraryID(id))
					.filter((/** @type {number|null} */ id) => id !== null)
			);
			const evidence = await MentionSearch.findMentionEvidence(result.citation_targets, zoteroLibraryIDs);

			if (onProgress) onProgress(40, 'Resubmitting query', 'Sending citation evidence to backend...');
			result = await this.plugin.submitQuery(question, libraryIds, {
				...options,
				clientEvidence: evidence,
				queryPlan: result.query_plan,
			});

			if (result.status === 'needs_client_evidence') {
				throw new Error('Backend requested citation evidence a second time — this should not happen.');
			}
		}

		return result;
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
refactor(dialog): extract runQuery() for the two-phase mentions protocol

Pulled out of submit()'s inline logic so submitFollowUp() (Task 11)
can reuse the exact same protocol handling for every later turn, not
just the first.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 8: `dialog.js` — `renderResultContent()` and `updateExportButtonVisibility()`

**Files:**
- Modify: `plugin/src/dialog.js`
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing tests**

```js
test('renderResultContent joins every turn\'s formatted HTML with a divider', () => {
	/** @type {any[][]} */
	const libraryMapCalls = [];
	const fakeElement = { innerHTML: '' };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => (id === 'result-content' ? fakeElement : null) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		plugin: {
			buildLibraryMap: (/** @type {string[]} */ ids) => { libraryMapCalls.push(ids); return new Map(); },
			formatTurnHTML: (/** @type {string} */ q, /** @type {any} */ r) => `<p>${q}:${r.answer}</p>`,
		},
		libraryIds: ['u1'],
		turns: [
			{ question: 'Q1', result: { answer: 'A1' } },
			{ question: 'Q2', result: { answer: 'A2' } },
		],
	};

	ContextDialog.renderResultContent.call(fakeThis);

	assert.strictEqual(fakeElement.innerHTML, '<p>Q1:A1</p><hr/><p>Q2:A2</p>');
	assert.deepStrictEqual(libraryMapCalls, [['u1']]);
});

test('updateExportButtonVisibility shows the button only when the first turn has a trace', () => {
	const fakeButton = { style: {} };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => (id === 'export-debug-button' ? fakeButton : null) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.updateExportButtonVisibility.call({ turns: [{ question: 'Q', result: { trace: { a: 1 } } }] });
	assert.strictEqual(fakeButton.style.display, '');

	ContextDialog.updateExportButtonVisibility.call({ turns: [{ question: 'Q', result: {} }] });
	assert.strictEqual(fakeButton.style.display, 'none');
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — neither method exists yet.

- [ ] **Step 3: Add the two methods**

```js
	/**
	 * Re-render the full conversation transcript from `this.turns` into
	 * `#result-content`. Called after the first submit and after every
	 * follow-up — simpler and more robust than incrementally appending DOM
	 * nodes, since the QueryResult objects (not their rendered HTML) are the
	 * source of truth.
	 * @returns {void}
	 */
	renderResultContent() {
		const container = document.getElementById('result-content');
		if (!container || !this.plugin) return;
		const libraryMap = this.plugin.buildLibraryMap(this.libraryIds);
		container.innerHTML = this.turns
			.map(({ question, result }) => this.plugin.formatTurnHTML(question, result, libraryMap))
			.join('<hr/>');
	},

	/**
	 * Show the Export Debug Info button iff the very first turn's result
	 * carries a trace — only the original question's advanced options can
	 * request one; follow-ups never expose that control.
	 * @returns {void}
	 */
	updateExportButtonVisibility() {
		const exportButton = /** @type {HTMLElement|null} */ (document.getElementById('export-debug-button'));
		if (!exportButton) return;
		const hasTrace = !!(this.turns[0] && this.turns[0].result.trace);
		exportButton.style.display = hasTrace ? '' : 'none';
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
feat(dialog): add renderResultContent() and updateExportButtonVisibility()

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 9: `dialog.js` — `switchToResultState()` (DOM toggle + citation click handling)

**Files:**
- Modify: `plugin/src/dialog.js`
- Test: `plugin/test/dialog.test.js` (a light smoke test only — see note below)

- [ ] **Step 1: Write a light failing test**

This method is almost entirely DOM manipulation with no meaningful business logic to assert beyond "did it toggle the right things" — following this codebase's existing precedent (`dialog.test.js`'s header comment: DOM-heavy code is deferred to manual verification, not unit-tested in depth). Add one smoke test:

```js
test('switchToResultState hides the input state and reveals the result state', () => {
	const inputContent = { style: {} };
	const inputButtons = { style: {} };
	const resultSection = { classList: { added: /** @type {string[]} */ ([]), add(/** @type {string} */ c) { this.added.push(c); } } };
	const resultContent = { addEventListener: () => {} };
	const elementsById = {
		'input-content': inputContent,
		'input-buttons': inputButtons,
		'result-section': resultSection,
		'result-content': resultContent,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.switchToResultState.call({});

	assert.strictEqual(inputContent.style.display, 'none');
	assert.strictEqual(inputButtons.style.display, 'none');
	assert.deepStrictEqual(resultSection.classList.added, ['visible']);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `switchToResultState` is not a function.

- [ ] **Step 3: Add `switchToResultState()`**

```js
	/**
	 * Hide the input-state form and reveal the result-state view. One-way per
	 * dialog session — the window only returns to a fresh input state by
	 * being closed and reopened (ZoteroRAG.openQueryDialog() always creates a
	 * new window/document when none is already open).
	 * @returns {void}
	 */
	switchToResultState() {
		const inputContent = /** @type {HTMLElement|null} */ (document.getElementById('input-content'));
		const inputButtons = /** @type {HTMLElement|null} */ (document.getElementById('input-buttons'));
		const resultSection = document.getElementById('result-section');
		if (inputContent) inputContent.style.display = 'none';
		if (inputButtons) inputButtons.style.display = 'none';
		if (resultSection) resultSection.classList.add('visible');

		// zotero:// links are a real registered Gecko protocol handler (not
		// specific to the note editor), but this is a privileged dialog window
		// rather than the note editor's own document — handle the click
		// explicitly via Zotero.launchURL rather than relying on default
		// navigation.
		const resultContent = document.getElementById('result-content');
		if (resultContent) {
			resultContent.addEventListener('click', (/** @type {MouseEvent} */ event) => {
				const target = /** @type {HTMLElement} */ (event.target);
				const anchor = /** @type {HTMLAnchorElement|null} */ (
					target.closest ? target.closest('a[href^="zotero://"]') : null
				);
				if (!anchor) return;
				event.preventDefault();
				// @ts-ignore - Zotero is a global in this context
				Zotero.launchURL(anchor.href);
			});
		}
	},
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
feat(dialog): add switchToResultState() with zotero:// citation click handling

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 10: `dialog.js` — wire `submit()` to enter result state instead of auto-creating a note and closing

**Files:**
- Modify: `plugin/src/dialog.js` (the `submit()` method)

No new automated test for `submit()` itself — the existing test suite has never unit-tested it directly (too much DOM: it reads six form fields via `document.getElementById`/`querySelector`). Its constituent pieces (`runQuery`, `renderResultContent`, `updateExportButtonVisibility`, `switchToResultState`) are already independently tested by Tasks 7-9. This task is verified by Task 15's manual/live testing.

- [ ] **Step 1: Replace the query-submission block**

In `submit()`, replace (originally around lines 1102-1139, but re-locate by searching for `'Processing query'` since line numbers have shifted from earlier tasks):

```js
			// Update progress for query phase
			this.updateProgress(0, 'Processing query', 'Sending query to backend...');

			let result = await this.plugin.submitQuery(question, libraryIds, {
				minScore: minScore,
				topK: topK,
				llmModel: llmModel,
				enableRouting: enableRouting,
				includeTrace: includeTrace
			});

			// The router determined this question needs citation evidence that only
			// exists in the user's local Zotero full-text index — gather it and resubmit,
			// echoing back query_plan so the backend doesn't re-run the routing LLM call.
			if (result.status === 'needs_client_evidence') {
				this.updateProgress(25, 'Searching local library', 'Scanning full text for citations...');
				const zoteroLibraryIDs = /** @type {Array<number>} */ (
					libraryIds
						.map((/** @type {string} */ id) => this.resolveZoteroLibraryID(id))
						.filter((/** @type {number|null} */ id) => id !== null)
				);
				const evidence = await MentionSearch.findMentionEvidence(result.citation_targets, zoteroLibraryIDs);

				this.updateProgress(40, 'Resubmitting query', 'Sending citation evidence to backend...');
				result = await this.plugin.submitQuery(question, libraryIds, {
					minScore: minScore,
					topK: topK,
					llmModel: llmModel,
					enableRouting: enableRouting,
					includeTrace: includeTrace,
					clientEvidence: evidence,
					queryPlan: result.query_plan
				});

				if (result.status === 'needs_client_evidence') {
					throw new Error('Backend requested citation evidence a second time — this should not happen.');
				}
			}

			// Update progress for note creation phase
			this.updateProgress(50, 'Creating note', 'Formatting results...');

			await this.plugin.createResultNote(question, result, libraryIds);

			if (result.status === 'needs_clarification') {
				this.updateProgress(100, 'Needs narrowing', 'Your question was too broad — see the note for details, and use the note\'s follow-up chat to narrow it.');
			} else {
				this.updateProgress(100, 'Complete', 'Note created successfully!');
			}

			// Close dialog after successful completion
			setTimeout(() => {
				window.close();
			}, 1000);
```

with:

```js
			// Update progress for query phase
			this.updateProgress(0, 'Processing query', 'Sending query to backend...');

			const result = await this.runQuery(question, libraryIds, {
				minScore: minScore,
				topK: topK,
				llmModel: llmModel,
				enableRouting: enableRouting,
				includeTrace: includeTrace
			}, (pct, label, message) => this.updateProgress(pct, label, message));

			this.libraryIds = libraryIds;
			this.turns = [{ question, result }];

			this.updateProgress(100, 'Complete', 'Rendering result...');
			this.switchToResultState();
			this.renderResultContent();
			this.updateExportButtonVisibility();
```

- [ ] **Step 2: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS — no test exercised `submit()`'s tail directly, so nothing should break; this step is a regression check on everything else in the file.

- [ ] **Step 3: Commit**

```bash
git add plugin/src/dialog.js
git commit -m "$(cat <<'EOF'
feat(dialog): submit() enters result state instead of auto-creating a note

Note creation is now on-demand (Task 12's Save as Note button) rather
than automatic — this is the last piece connecting the first question
to the new result-state view.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 11: `dialog.js` — `submitFollowUp()`

**Files:**
- Modify: `plugin/src/dialog.js`
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing tests**

```js
test('submitFollowUp appends a turn and re-renders, without touching a note when none has been saved', async () => {
	const fakeInput = { value: 'Follow-up question', disabled: false };
	const fakeButton = { disabled: false };
	const elementsById = { 'followup-input': fakeInput, 'result-submit-button': fakeButton };
	/** @type {number[]} */
	const renderCalls = [];
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		plugin: { submitQuery: async () => ({ status: 'complete', answer: 'Follow-up answer.', sources: [] }) },
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: null,
		buildConversationHistory: ContextDialog.buildConversationHistory,
		runQuery: ContextDialog.runQuery,
		renderResultContent() { renderCalls.push(this.turns.length); },
		showStatus() {},
	};

	await ContextDialog.submitFollowUp.call(fakeThis);

	assert.strictEqual(fakeThis.turns.length, 2);
	assert.strictEqual(fakeThis.turns[1].question, 'Follow-up question');
	assert.strictEqual(fakeInput.value, '');
	assert.deepStrictEqual(renderCalls, [2]);
});

test('submitFollowUp appends the new turn to the note when one has already been saved', async () => {
	const fakeInput = { value: 'Follow-up question', disabled: false };
	const elementsById = { 'followup-input': fakeInput, 'result-submit-button': { disabled: false } };
	/** @type {string[]} */
	const notedHtml = [];
	const noteStub = { getNote: () => '<div>existing</div>', setNote: (/** @type {string} */ html) => notedHtml.push(html), saveTx: async () => {} };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
		Zotero: { Items: { get: (/** @type {number} */ id) => (id === 42 ? noteStub : null) } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		plugin: {
			submitQuery: async () => ({ status: 'complete', answer: 'Follow-up answer.', sources: [] }),
			buildLibraryMap: () => new Map(),
			formatTurnHTML: (/** @type {string} */ q, /** @type {any} */ r) => `<p>${q}:${r.answer}</p>`,
		},
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: 42,
		buildConversationHistory: ContextDialog.buildConversationHistory,
		runQuery: ContextDialog.runQuery,
		renderResultContent() {},
		showStatus() {},
	};

	await ContextDialog.submitFollowUp.call(fakeThis);

	assert.strictEqual(notedHtml.length, 1);
	assert.strictEqual(notedHtml[0], '<div>existing</div><p>Follow-up question:Follow-up answer.</p>');
});

test('submitFollowUp does nothing when the input is empty', async () => {
	const fakeInput = { value: '   ', disabled: false };
	const elementsById = { 'followup-input': fakeInput };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = { plugin: { submitQuery: async () => { throw new Error('should not be called'); } }, turns: [] };
	await ContextDialog.submitFollowUp.call(fakeThis);
	assert.strictEqual(fakeThis.turns.length, 0);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `ZoteroRAGDialog.submitFollowUp` is not a function.

- [ ] **Step 3: Add `submitFollowUp()`**

```js
	/**
	 * Submit a follow-up question in result state: run the query (reusing the
	 * two-phase mentions protocol via runQuery), append the turn, re-render,
	 * and — if a note has already been created via saveAsNote() — append the
	 * turn to it too.
	 * @returns {Promise<void>}
	 */
	async submitFollowUp() {
		if (!this.plugin) return;
		const input = /** @type {HTMLTextAreaElement|null} */ (document.getElementById('followup-input'));
		if (!input) return;
		const question = input.value.trim();
		if (!question) return;

		const submitButton = /** @type {HTMLButtonElement|null} */ (document.getElementById('result-submit-button'));
		if (submitButton) submitButton.disabled = true;
		input.disabled = true;

		try {
			const result = await this.runQuery(question, this.libraryIds, {
				conversationHistory: this.buildConversationHistory(),
			});
			this.turns.push({ question, result });
			input.value = '';
			this.renderResultContent();

			if (this.noteID !== null) {
				const libraryMap = this.plugin.buildLibraryMap(this.libraryIds);
				const turnHtml = this.plugin.formatTurnHTML(question, result, libraryMap);
				// @ts-ignore - Zotero is a global in this context
				const note = Zotero.Items.get(this.noteID);
				note.setNote(note.getNote() + turnHtml);
				await note.saveTx();
			}
		} catch (error) {
			const msg = error instanceof Error ? error.message : String(error);
			this.showStatus(`Error: ${msg}`, 'error');
		} finally {
			if (submitButton) submitButton.disabled = false;
			input.disabled = false;
		}
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
feat(dialog): add submitFollowUp() for result-state follow-up questions

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 12: `dialog.js` — `saveAsNote()`

**Files:**
- Modify: `plugin/src/dialog.js`
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing tests**

```js
test('saveAsNote creates the note from every turn and disables the button', async () => {
	const fakeButton = { disabled: false, textContent: 'Save as Note' };
	const elementsById = { 'save-note-button': fakeButton };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any} */
	let receivedTurns = null;
	const fakeThis = {
		plugin: { createResultNote: async (/** @type {any} */ turns, /** @type {string[]} */ _libIds) => { receivedTurns = turns; return { id: 99 }; } },
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: null,
	};

	await ContextDialog.saveAsNote.call(fakeThis);

	assert.strictEqual(fakeThis.noteID, 99);
	assert.strictEqual(receivedTurns, fakeThis.turns);
	assert.strictEqual(fakeButton.disabled, true);
	assert.strictEqual(fakeButton.textContent, 'Saved');
});

test('saveAsNote does nothing if a note has already been saved', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const fakeThis = {
		plugin: { createResultNote: async () => { throw new Error('should not be called'); } },
		turns: [], noteID: 42,
	};
	await ZoteroRAGDialog.saveAsNote.call(fakeThis);
	assert.strictEqual(fakeThis.noteID, 42);
});

test('saveAsNote re-enables the button and shows an error if note creation fails', async () => {
	const fakeButton = { disabled: false, textContent: 'Save as Note' };
	const elementsById = { 'save-note-button': fakeButton };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any[]} */
	const statusCalls = [];
	const fakeThis = {
		plugin: { createResultNote: async () => { throw new Error('disk full'); } },
		libraryIds: ['u1'], turns: [{ question: 'Q0', result: { answer: 'A0' } }], noteID: null,
		showStatus(/** @type {string} */ msg, /** @type {string} */ type) { statusCalls.push({ msg, type }); },
	};

	await ContextDialog.saveAsNote.call(fakeThis);

	assert.strictEqual(fakeThis.noteID, null);
	assert.strictEqual(fakeButton.disabled, false);
	assert.strictEqual(statusCalls.length, 1);
	assert.strictEqual(statusCalls[0].type, 'error');
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `ZoteroRAGDialog.saveAsNote` is not a function.

- [ ] **Step 3: Add `saveAsNote()`**

```js
	/**
	 * Create the Zotero note from every turn so far. First click only —
	 * subsequent follow-ups append directly to the already-created note (see
	 * submitFollowUp()). Disables the button once done.
	 * @returns {Promise<void>}
	 */
	async saveAsNote() {
		if (!this.plugin || this.noteID !== null) return;
		const saveButton = /** @type {HTMLButtonElement|null} */ (document.getElementById('save-note-button'));
		if (saveButton) saveButton.disabled = true;
		try {
			const note = await this.plugin.createResultNote(this.turns, this.libraryIds);
			this.noteID = note.id;
			if (saveButton) saveButton.textContent = 'Saved';
		} catch (error) {
			if (saveButton) saveButton.disabled = false;
			const msg = error instanceof Error ? error.message : String(error);
			this.showStatus(`Failed to save note: ${msg}`, 'error');
		}
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
feat(dialog): add saveAsNote() for on-demand note creation

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 13: `dialog.js` — `exportDebugInfo()`

**Files:**
- Modify: `plugin/src/dialog.js`
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing tests**

```js
test('exportDebugInfo writes the first turn\'s trace as formatted JSON to the picked file path', async () => {
	/** @type {string[]} */
	const written = [];
	const fakePicker = {
		appendFilter() {}, init() {}, defaultString: '',
		file: { path: '/fake/path/trace.json' },
		open: (/** @type {(rv: number) => void} */ callback) => callback(1 /* returnOK */),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => fakePicker } },
		Ci: { nsIFilePicker: { modeSave: 0, returnOK: 1, returnReplace: 2 } },
		IOUtils: { writeUTF8: async (/** @type {string} */ p, /** @type {string} */ text) => { written.push(`${p}::${text}`); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = { turns: [{ question: 'Q', result: { trace: { step: 1 } } }] };
	await ContextDialog.exportDebugInfo.call(fakeThis);

	assert.strictEqual(written.length, 1);
	assert.ok(written[0].startsWith('/fake/path/trace.json::'));
	assert.ok(written[0].includes('"step": 1'));
});

test('exportDebugInfo does nothing when the first turn has no trace', async () => {
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => { throw new Error('should not be called'); } } },
		Ci: { nsIFilePicker: {} },
		IOUtils: { writeUTF8: async () => { throw new Error('should not be called'); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await ContextDialog.exportDebugInfo.call({ turns: [{ question: 'Q', result: {} }] });
	// No assertion beyond "did not throw" — the stubs above throw if called.
});

test('exportDebugInfo does not write a file when the user cancels the save dialog', async () => {
	/** @type {string[]} */
	const written = [];
	const fakePicker = {
		appendFilter() {}, init() {}, defaultString: '',
		file: { path: '/fake/path/trace.json' },
		open: (/** @type {(rv: number) => void} */ callback) => callback(-1 /* returnCancel */),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => fakePicker } },
		Ci: { nsIFilePicker: { modeSave: 0, returnOK: 1, returnReplace: 2 } },
		IOUtils: { writeUTF8: async (/** @type {string} */ p) => { written.push(p); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await ContextDialog.exportDebugInfo.call({ turns: [{ question: 'Q', result: { trace: { a: 1 } } }] });
	assert.strictEqual(written.length, 0);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `ZoteroRAGDialog.exportDebugInfo` is not a function.

- [ ] **Step 3: Add `exportDebugInfo()`**

```js
	/**
	 * Save the first turn's execution trace as formatted JSON via a native
	 * save-file dialog. Only meaningfully callable when the button is
	 * visible, which happens iff `this.turns[0].result.trace` is present
	 * (see updateExportButtonVisibility()).
	 * @returns {Promise<void>}
	 */
	async exportDebugInfo() {
		const trace = this.turns[0] && this.turns[0].result.trace;
		if (!trace) return;

		// @ts-ignore - Cc/Ci are globals in this privileged context
		const fp = Cc['@mozilla.org/filepicker;1'].createInstance(Ci.nsIFilePicker);
		fp.init(window, 'Export Debug Info', Ci.nsIFilePicker.modeSave);
		fp.appendFilter('JSON files', '*.json');
		fp.defaultString = 'zotero-rag-debug-trace.json';

		const rv = await new Promise((resolve) => fp.open(resolve));
		if (rv !== Ci.nsIFilePicker.returnOK && rv !== Ci.nsIFilePicker.returnReplace) return;

		// @ts-ignore - IOUtils is a global in Firefox/Zotero
		await IOUtils.writeUTF8(fp.file.path, JSON.stringify(trace, null, 2));
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "$(cat <<'EOF'
feat(dialog): add exportDebugInfo() using nsIFilePicker

The trace is no longer auto-embedded in the note (Task 1) — this is
the on-demand export path replacing it. Uses the native nsIFilePicker
directly rather than the zotero-plugin-toolkit's FilePickerHelper,
since toolkit.bundle.js is never loaded into the dialog window's
scope (only bootstrap.js's) and dialog.js already uses raw platform
APIs (Services.prompt, IOUtils, PathUtils) elsewhere.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 14: `dialog.js` — wire the new buttons in `init()`

**Files:**
- Modify: `plugin/src/dialog.js:183-263` (`init()`)

No new automated test — this task only adds `addEventListener` calls in `init()`, following the exact existing pattern for `submit-button`/`cancel-button` (lines 201-213), which itself has no dedicated test (per the file's established precedent — `init()`'s DOM wiring is covered by Task 15's manual verification, not unit tests).

- [ ] **Step 1: Add the four new listeners**

In `init()`, immediately after the existing block:

```js
		const cancelButton = document.getElementById('cancel-button');
		if (cancelButton) {
			cancelButton.addEventListener('click', () => {
				this.handleCancel();
			});
		}
```

add:

```js
		const resultSubmitButton = document.getElementById('result-submit-button');
		if (resultSubmitButton) {
			resultSubmitButton.addEventListener('click', () => {
				this.submitFollowUp();
			});
		}

		const saveNoteButton = document.getElementById('save-note-button');
		if (saveNoteButton) {
			saveNoteButton.addEventListener('click', () => {
				this.saveAsNote();
			});
		}

		const exportDebugButton = document.getElementById('export-debug-button');
		if (exportDebugButton) {
			exportDebugButton.addEventListener('click', () => {
				this.exportDebugInfo();
			});
		}

		const resultCloseButton = document.getElementById('result-close-button');
		if (resultCloseButton) {
			resultCloseButton.addEventListener('click', () => {
				window.close();
			});
		}
```

- [ ] **Step 2: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS — `loadDialogMethods()`'s base context's `document.addEventListener`/no-op `getElementById` (returning `null` for unknown IDs, per its existing stub) already handles these new `getElementById` calls returning `null` gracefully — `init()` still never actually runs in that test file (see its header comment), so this change is inert from the existing tests' point of view.

- [ ] **Step 3: Commit**

```bash
git add plugin/src/dialog.js
git commit -m "$(cat <<'EOF'
feat(dialog): wire the result-state buttons to their handlers

Last piece of the dialog redesign — result-submit-button,
save-note-button, export-debug-button, and result-close-button are
now live.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 15: Manual live verification

Not a code task — no commit. Use the dev Zotero instance + dev backend per root `CLAUDE.md`'s "Live Query Debugging" section, against the `test-rag-plugin` group library (`groups/6297749`). Verify, in order:

- [ ] Open the query dialog, ask a question (e.g. "What is Zotero used for?"), submit. Confirm the input-state form (library list, advanced options) disappears and the result state shows the rendered answer + bibliography.
- [ ] Confirm citation links are clickable and correctly select/open the right Zotero item (tests the `Zotero.launchURL` click handler added in Task 9).
- [ ] Type a follow-up question and submit without ever clicking Save as Note. Confirm the transcript grows in place and nothing appears in the library yet.
- [ ] Click "Save as Note". Confirm a tagged (`RAG Query Result`) note appears in the library containing every turn so far, and the button becomes disabled/relabeled "Saved".
- [ ] Ask another follow-up after saving. Confirm it appends to the same note (check the note's content directly) rather than creating a second note.
- [ ] Check "Include debugging information" in advanced options before the very first submit. Confirm "Export Debug Info" appears in result state, and clicking it produces a valid JSON file via the native save dialog.
- [ ] Repeat a fresh query with that checkbox left unchecked. Confirm "Export Debug Info" is absent.
- [ ] Ask an intentionally broad question that triggers `needs_clarification` (e.g. "What has been written about Zotero?" across a small library with no filters). Confirm the clarification message renders correctly (not a blank turn) and a follow-up reply continues the same conversation.
- [ ] Close the dialog without saving, then reopen it. Confirm it's back to a clean input state (library list, advanced options all restored/reset).
- [ ] Restart the dev backend mid-conversation (`npm start`, per CLAUDE.md's "Live Server" section) and confirm a follow-up submitted afterward still works (the backend is stateless — see `docs/query-routing.md`'s "Follow-up Conversations" section).
