# Per-Item Book/Chapter Menu Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add "Segment Book" and "Match Chapter" actions to a new "Zotero RAG: Tools" submenu (items-pane right-click menu + Tools menu), so a user can selectively run the existing chapter-segmentation pipeline against one book, or manually link one standalone chapter to its book, without touching the whole-library batch pipeline.

**Architecture:** Two new small pure-logic modules (`fuzzyMatch.js` for title/year similarity scoring, `chapterLinks.js` for the client-side X-Contains/X-Contained-By Extra-field + native-relations write, both plain scripts loaded the same way every other top-level plugin script is), a new `chapterActions.js` controller that registers the menus and implements both flows (Segment Book calls the existing backend job endpoints; Match Chapter is 100% local — Zotero.Search + FuzzyMatch + ChapterLinks, no backend call), and a new native dialog (`match-chapter-dialog.xhtml`/`.js`) for picking a candidate. One existing file (`toolkit.js`) gets a small addition (exposing `ExtraFieldTool`) that requires rebuilding the vendored `toolkit.bundle.js`.

**Tech Stack:** Plain (non-module) JavaScript loaded via `Services.scriptloader.loadSubScript` inside Zotero's chrome environment; `zotero-plugin-toolkit` (`UITool`, `ExtraFieldTool`) for DOM/Extra-field helpers; Node's built-in test runner (`node --test`) with a hand-rolled `vm`-context stubbing pattern for unit tests (no CommonJS/Jest — matches every existing `plugin/test/*.test.js` file).

**Reference spec:** `docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md`

---

### Task 1: `fuzzyMatch.js` — title/year similarity scoring

**Files:**
- Create: `plugin/src/fuzzyMatch.js`
- Test: `plugin/test/fuzzyMatch.test.js`

Pure, dependency-free module. No Zotero globals needed at all — this is testable in a bare `vm` context.

- [ ] **Step 1: Write the failing test**

```js
// plugin/test/fuzzyMatch.test.js
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'fuzzyMatch.js');

function loadFuzzyMatch() {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'fuzzyMatch.js' });
	return context.FuzzyMatch;
}

test('titleSimilarity scores identical titles as 100', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(FuzzyMatch.titleSimilarity('The Great Book', 'The Great Book'), 100);
});

test('titleSimilarity is order-independent (token-sort)', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(FuzzyMatch.titleSimilarity('Great The Book', 'The Great Book'), 100);
});

test('titleSimilarity scores a partial rewording lower but still similar', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(
		FuzzyMatch.titleSimilarity('Handbook of Zotero Studies', 'The Zotero Studies Handbook'),
		74
	);
});

test('titleSimilarity scores unrelated titles low', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(
		FuzzyMatch.titleSimilarity('Handbook of Zotero Studies', 'Introduction to Reference Managers'),
		26
	);
});

test('scoreCandidate applies no penalty for a matching year', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', 2020, { title: 'Handbook of Zotero Studies', year: 2020 });
	assert.strictEqual(score, 100);
});

test('scoreCandidate applies a small penalty for a 1-year difference', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', 2020, { title: 'Handbook of Zotero Studies', year: 2021 });
	assert.strictEqual(score, 98);
});

test('scoreCandidate applies a larger, capped penalty for a big year gap', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', 2020, { title: 'Handbook of Zotero Studies', year: 2025 });
	assert.strictEqual(score, 85);
});

test('scoreCandidate skips the year penalty when either year is missing', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', null, { title: 'Handbook of Zotero Studies', year: null });
	assert.strictEqual(score, 100);
});

test('rankCandidates sorts descending by score and truncates to topN', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const books = [
		{ key: 'BOOK1', title: 'Handbook of Zotero Studies', year: 2020 },
		{ key: 'BOOK2', title: 'Introduction to Reference Managers', year: 2019 },
		{ key: 'BOOK3', title: 'The Zotero Studies Handbook', year: 2020 },
	];
	const ranked = FuzzyMatch.rankCandidates('Handbook of Zotero Studies', 2020, books, 5);
	assert.deepStrictEqual(ranked.map(b => b.key), ['BOOK1', 'BOOK3', 'BOOK2']);
	assert.strictEqual(ranked[0].score, 100);
	assert.strictEqual(ranked[1].score, 74);
	assert.strictEqual(ranked[2].score, 24);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/fuzzyMatch.test.js`
Expected: FAIL — `plugin/src/fuzzyMatch.js` doesn't exist yet (`ENOENT`).

- [ ] **Step 3: Write the implementation**

```js
// plugin/src/fuzzyMatch.js
// Pure, dependency-free book-title/year similarity scoring for the "Match
// Chapter" dialog (chapterActions.js). Deliberately independent from
// backend/services/chapter_retrofit.py's rapidfuzz-based scorer -- see
// docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md
// for why this isn't shared code.

/**
 * @param {string} a
 * @param {string} b
 * @returns {number} Levenshtein edit distance
 */
function _levenshtein(a, b) {
	const m = a.length, n = b.length;
	if (m === 0) return n;
	if (n === 0) return m;
	let prev = new Array(n + 1);
	let curr = new Array(n + 1);
	for (let j = 0; j <= n; j++) prev[j] = j;
	for (let i = 1; i <= m; i++) {
		curr[0] = i;
		for (let j = 1; j <= n; j++) {
			const cost = a[i - 1] === b[j - 1] ? 0 : 1;
			curr[j] = Math.min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost);
		}
		[prev, curr] = [curr, prev];
	}
	return prev[n];
}

/**
 * @param {string} str
 * @returns {string}
 */
function _normalizeTitle(str) {
	return (str || '').toLowerCase().trim().replace(/\s+/g, ' ');
}

/**
 * @param {string} str
 * @returns {string} words lowercased, sorted, space-joined
 */
function _sortedTokens(str) {
	return _normalizeTitle(str).split(' ').filter(Boolean).sort().join(' ');
}

/**
 * @param {string} a
 * @param {string} b
 * @returns {number} 0-100
 */
function _levenshteinRatio(a, b) {
	a = a || ''; b = b || '';
	const dist = _levenshtein(a, b);
	const maxLen = Math.max(a.length, b.length);
	if (maxLen === 0) return 100;
	return Math.round((1 - dist / maxLen) * 100);
}

var FuzzyMatch = {
	/**
	 * Token-sort-ratio-style title similarity: order-independent, 0-100.
	 * @param {string} a
	 * @param {string} b
	 * @returns {number}
	 */
	titleSimilarity(a, b) {
		return _levenshteinRatio(_sortedTokens(a), _sortedTokens(b));
	},

	/**
	 * @param {string} chapterTitle
	 * @param {number|null} chapterYear
	 * @param {{title: string, year: number|null}} book
	 * @returns {number} 0-100
	 */
	scoreCandidate(chapterTitle, chapterYear, book) {
		let score = this.titleSimilarity(chapterTitle, book.title);
		if (chapterYear != null && book.year != null) {
			const diff = Math.abs(chapterYear - book.year);
			if (diff === 0) {
				// no penalty
			} else if (diff <= 1) {
				score -= 2;
			} else {
				score -= Math.min(20, diff * 3);
			}
		}
		return Math.max(0, Math.min(100, Math.round(score)));
	},

	/**
	 * @param {string} chapterTitle
	 * @param {number|null} chapterYear
	 * @param {Array<{title: string, year: number|null}>} books
	 * @param {number} [topN]
	 * @returns {Array<object>} `books` entries with a `score` property, sorted descending, truncated to topN
	 */
	rankCandidates(chapterTitle, chapterYear, books, topN = 5) {
		return books
			.map(book => Object.assign({}, book, { score: this.scoreCandidate(chapterTitle, chapterYear, book) }))
			.sort((a, b) => b.score - a.score)
			.slice(0, topN);
	},
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/fuzzyMatch.test.js`
Expected: PASS — all 9 tests green.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/fuzzyMatch.js plugin/test/fuzzyMatch.test.js
git commit -m "feat: add client-side fuzzy title/year matcher for chapter linking"
```

---

### Task 2: `chapterLinks.js` — client-side link read/write

**Files:**
- Create: `plugin/src/chapterLinks.js`
- Test: `plugin/test/chapterLinks.test.js`

Replicates the format `backend/services/chapter_link_store.py` reads (`X-Contains`/`X-Contained-By` Extra-field lines, `dc:relation` native relations) entirely client-side. Depends only on `Zotero.Libraries`/`Zotero.Users`/`Zotero.Groups` (for the slug) and an injected `extraFieldTool` (a `zotero-plugin-toolkit` `ExtraFieldTool` instance — see Task 3) plus `item.addRelatedItem`/`item.saveTx` (native Zotero).

- [ ] **Step 1: Write the failing test**

```js
// plugin/test/chapterLinks.test.js
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'chapterLinks.js');

function loadChapterLinks(zoteroStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'chapterLinks.js' });
	return context.ChapterLinks;
}

/** In-memory stand-in for a zotero-plugin-toolkit ExtraFieldTool instance. */
function makeExtraFieldToolStub() {
	const store = new Map(); // item.key -> { fieldName -> string[] }
	return {
		getExtraField(item, key, all = false) {
			const map = store.get(item.key) || {};
			const values = map[key];
			if (!values) return undefined;
			return all ? values : values[0];
		},
		async setExtraField(item, key, value, options = {}) {
			const map = store.get(item.key) || {};
			if (value === '' || value === undefined) {
				delete map[key];
			} else {
				map[key] = Array.isArray(value) ? value : [value];
			}
			store.set(item.key, map);
		},
	};
}

function makeItem(key, libraryID = 1) {
	return {
		key,
		libraryID,
		_related: [],
		_saved: 0,
		addRelatedItem(other) { this._related.push(other.key); },
		async saveTx() { this._saved += 1; },
	};
}

test('getZoteroSlug returns users/<id> for the personal library', () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 42 },
		Groups: {},
	});
	assert.strictEqual(ChapterLinks.getZoteroSlug(1), 'users/42');
});

test('getZoteroSlug returns groups/<id> for a group library', () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: {},
		Groups: { getByLibraryID: () => ({ id: 6297749 }) },
	});
	assert.strictEqual(ChapterLinks.getZoteroSlug(5), 'groups/6297749');
});

test('hasContainedByLink reflects the native Extra field', () => {
	const ChapterLinks = loadChapterLinks({ Libraries: {}, Users: {}, Groups: {} });
	assert.strictEqual(ChapterLinks.hasContainedByLink({ getExtraField: () => '' }), false);
	assert.strictEqual(ChapterLinks.hasContainedByLink({ getExtraField: () => 'users/42:BOOK1' }), true);
});

test('writeLink sets X-Contains/X-Contained-By and native relations both ways', async () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 42 },
		Groups: {},
	});
	const extraFieldTool = makeExtraFieldToolStub();
	const book = makeItem('BOOK1', 1);
	const chapter = makeItem('CHAP1', 1);

	await ChapterLinks.writeLink(extraFieldTool, book, chapter);

	assert.strictEqual(extraFieldTool.getExtraField(book, 'X-Contains'), 'users/42:CHAP1');
	assert.strictEqual(extraFieldTool.getExtraField(chapter, 'X-Contained-By'), 'users/42:BOOK1');
	assert.deepStrictEqual(book._related, ['CHAP1']);
	assert.deepStrictEqual(chapter._related, ['BOOK1']);
	assert.strictEqual(book._saved, 1);
	assert.strictEqual(chapter._saved, 1);
});

test('writeLink appends a second chapter to the same book without duplicating', async () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 42 },
		Groups: {},
	});
	const extraFieldTool = makeExtraFieldToolStub();
	const book = makeItem('BOOK1', 1);
	const chapter1 = makeItem('CHAP1', 1);
	const chapter2 = makeItem('CHAP2', 1);

	await ChapterLinks.writeLink(extraFieldTool, book, chapter1);
	await ChapterLinks.writeLink(extraFieldTool, book, chapter2);
	assert.strictEqual(extraFieldTool.getExtraField(book, 'X-Contains'), 'users/42:CHAP1,users/42:CHAP2');

	// re-linking the same chapter is idempotent
	await ChapterLinks.writeLink(extraFieldTool, book, chapter1);
	assert.strictEqual(extraFieldTool.getExtraField(book, 'X-Contains'), 'users/42:CHAP1,users/42:CHAP2');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test plugin/test/chapterLinks.test.js`
Expected: FAIL — `plugin/src/chapterLinks.js` doesn't exist yet.

- [ ] **Step 3: Write the implementation**

```js
// plugin/src/chapterLinks.js
// Client-side implementation of the X-Contains/X-Contained-By Extra-field
// convention plus native "Related" item linking, used by the "Match
// Chapter" dialog (match-chapter-dialog.js) and chapterActions.js to link
// a bookSection item to its book without any backend call. Mirrors the
// format backend/services/chapter_link_store.py reads (write_links(),
// add_related_item()) closely enough to stay compatible with backend
// readers (chapter retrieval / citation suppression), implemented
// independently in JS -- see
// docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md.

var ChapterLinks = {
	/**
	 * @param {number} libraryID
	 * @returns {string} "users/<id>" or "groups/<id>"
	 */
	getZoteroSlug(libraryID) {
		if (libraryID === Zotero.Libraries.userLibraryID) {
			const userId = Zotero.Users.getCurrentUserID();
			return `users/${userId}`;
		}
		const group = Zotero.Groups.getByLibraryID(libraryID);
		return `groups/${group.id}`;
	},

	/**
	 * @param {string} slug
	 * @param {string} itemKey
	 * @returns {string}
	 */
	formatChapterId(slug, itemKey) {
		return `${slug}:${itemKey}`;
	},

	/**
	 * @param {*} item
	 * @returns {boolean}
	 */
	hasContainedByLink(item) {
		return !!item.getExtraField('X-Contained-By');
	},

	/**
	 * Write the X-Contains/X-Contained-By Extra-field link and the native
	 * "Related" connection between a book and one of its chapters. Both
	 * directions are written explicitly -- Zotero's local relations
	 * storage does not auto-mirror the reverse direction the way the
	 * server does (Zotero.Item.prototype._getRelatedItems only reads an
	 * item's own stored relations, confirmed against the Zotero source).
	 * @param {*} extraFieldTool - a zotero-plugin-toolkit ExtraFieldTool instance (plugin.toolkit.extraField)
	 * @param {*} bookItem
	 * @param {*} chapterItem
	 * @returns {Promise<void>}
	 */
	async writeLink(extraFieldTool, bookItem, chapterItem) {
		const slug = this.getZoteroSlug(bookItem.libraryID);
		const chapterId = this.formatChapterId(slug, chapterItem.key);
		const bookId = this.formatChapterId(slug, bookItem.key);

		const existing = extraFieldTool.getExtraField(bookItem, 'X-Contains', true) || [];
		const containsList = existing.length
			? existing[0].split(',').map(s => s.trim()).filter(Boolean)
			: [];
		if (!containsList.includes(chapterId)) {
			containsList.push(chapterId);
		}
		await extraFieldTool.setExtraField(bookItem, 'X-Contains', containsList.join(','), { save: false });
		bookItem.addRelatedItem(chapterItem);
		await bookItem.saveTx();

		await extraFieldTool.setExtraField(chapterItem, 'X-Contained-By', bookId, { save: false });
		chapterItem.addRelatedItem(bookItem);
		await chapterItem.saveTx();
	},
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test plugin/test/chapterLinks.test.js`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/chapterLinks.js plugin/test/chapterLinks.test.js
git commit -m "feat: add client-side book/chapter link writer"
```

---

### Task 3: Expose `ExtraFieldTool` from the toolkit bundle

**Files:**
- Modify: `plugin/src/toolkit.js`
- Regenerate: `plugin/src/toolkit.bundle.js`, `plugin/src/toolkit.bundle.js.map`

`ChapterLinks.writeLink` (Task 2) needs a `zotero-plugin-toolkit` `ExtraFieldTool` instance, but nothing in this plugin instantiates one today (`toolkit.js`'s `createToolkit()` only builds `BasicTool`/`UITool`/`ProgressWindowHelper`). No test for this step — it's a mechanical import + rebuild, verified by grepping the rebuilt bundle.

- [ ] **Step 1: Add the import**

In `plugin/src/toolkit.js`, change:

```js
import { BasicTool, UITool, ProgressWindowHelper, VirtualizedTableHelper } from 'zotero-plugin-toolkit';
```

to:

```js
import { BasicTool, UITool, ProgressWindowHelper, VirtualizedTableHelper, ExtraFieldTool } from 'zotero-plugin-toolkit';
```

- [ ] **Step 2: Instantiate and expose it**

In the same file, change:

```js
	const basicTool = new BasicTool();
	const uiTool = new UITool();
	const progressHelper = new ProgressWindowHelper(config.id, 'Zotero RAG');

	return {
		basicTool,
		uiTool,
		progressHelper,
```

to:

```js
	const basicTool = new BasicTool();
	const uiTool = new UITool();
	const progressHelper = new ProgressWindowHelper(config.id, 'Zotero RAG');
	const extraField = new ExtraFieldTool(basicTool);

	return {
		basicTool,
		uiTool,
		progressHelper,
		extraField,
```

- [ ] **Step 3: Rebuild the bundle**

Run: `npm run plugin:build:toolkit`
Expected output: `[OK] Toolkit bundled successfully` and `[OK] Output: .../plugin/src/toolkit.bundle.js`.

- [ ] **Step 4: Verify the bundle actually contains it**

Run: `grep -c "ExtraFieldTool" plugin/src/toolkit.bundle.js`
Expected: a non-zero count (the class definition plus the new `extraField:` instantiation).

- [ ] **Step 5: Commit**

```bash
git add plugin/src/toolkit.js plugin/src/toolkit.bundle.js plugin/src/toolkit.bundle.js.map
git commit -m "feat: expose ExtraFieldTool from the plugin toolkit wrapper"
```

---

### Task 4: `minBookPages` preference

**Files:**
- Modify: `plugin/src/preferences.xhtml`
- Modify: `plugin/src/preferences.js`

Client-side-only threshold (never sent to the backend, per the design) for "Segment Book" eligibility, following the existing standalone-numeric-pref pattern (`maxQueries`), not the `DIVERSITY_TUNING_FIELDS` table (that table's consumer is specific to the retrieval-tuning payload).

- [ ] **Step 1: Add the input to the Preferences pane**

In `plugin/src/preferences.xhtml`, change:

```xhtml
  <!-- Performance Settings -->
  <html:fieldset class="settings-group">
    <html:legend>Performance</html:legend>

    <html:div class="setting-row">
      <html:label for="zotero-rag-max-queries">Max Concurrent Queries:</html:label>
      <html:input id="zotero-rag-max-queries"
                  type="number"
                  min="1"
                  max="10"
                  class="setting-input-small"/>
    </html:div>

    <html:div class="setting-description">
      Maximum number of simultaneous queries allowed (1-10)
    </html:div>
  </html:fieldset>

  <!-- Retrieval Tuning -->
```

to:

```xhtml
  <!-- Performance Settings -->
  <html:fieldset class="settings-group">
    <html:legend>Performance</html:legend>

    <html:div class="setting-row">
      <html:label for="zotero-rag-max-queries">Max Concurrent Queries:</html:label>
      <html:input id="zotero-rag-max-queries"
                  type="number"
                  min="1"
                  max="10"
                  class="setting-input-small"/>
    </html:div>

    <html:div class="setting-description">
      Maximum number of simultaneous queries allowed (1-10)
    </html:div>
  </html:fieldset>

  <!-- Book & Chapter Tools -->
  <html:fieldset class="settings-group">
    <html:legend>Book &amp; Chapter Tools</html:legend>

    <html:div class="setting-row">
      <html:label for="zotero-rag-min-book-pages">Minimum book pages for "Segment Book":</html:label>
      <html:input id="zotero-rag-min-book-pages"
                  type="number"
                  min="1"
                  class="setting-input-small"/>
    </html:div>

    <html:div class="setting-description">
      "Segment Book" (right-click menu / Tools menu) refuses to run against a PDF attachment
      with fewer pages than this. Checked locally, never sent to the backend.
    </html:div>
  </html:fieldset>

  <!-- Retrieval Tuning -->
```

- [ ] **Step 2: Read the stored value on pane load**

In `plugin/src/preferences.js`, change:

```js
	const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

	// Show stored value; leave blank so the placeholder shows when nothing is saved
	doc.getElementById('zotero-rag-backend-url').value = backendURL;
	doc.getElementById('zotero-rag-zotero-api-key').value = zoteroApiKey;
	doc.getElementById('zotero-rag-max-queries').value = maxQueries;
```

to:

```js
	const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;
	const minBookPages = Zotero.Prefs.get('extensions.zotero-rag.minBookPages', true) || 50;

	// Show stored value; leave blank so the placeholder shows when nothing is saved
	doc.getElementById('zotero-rag-backend-url').value = backendURL;
	doc.getElementById('zotero-rag-zotero-api-key').value = zoteroApiKey;
	doc.getElementById('zotero-rag-max-queries').value = maxQueries;
	doc.getElementById('zotero-rag-min-book-pages').value = minBookPages;
```

- [ ] **Step 3: Add the change listener**

In `plugin/src/preferences.js`, change:

```js
	doc.getElementById('zotero-rag-max-queries').addEventListener('change', (e) => {
		const value = parseInt(/** @type {HTMLInputElement} */ (e.target).value);
		if (value >= 1 && value <= 10) {
			Zotero.Prefs.set('extensions.zotero-rag.maxQueries', value, true);
			this.maxConcurrentQueries = value;
		}
	});
```

to:

```js
	doc.getElementById('zotero-rag-max-queries').addEventListener('change', (e) => {
		const value = parseInt(/** @type {HTMLInputElement} */ (e.target).value);
		if (value >= 1 && value <= 10) {
			Zotero.Prefs.set('extensions.zotero-rag.maxQueries', value, true);
			this.maxConcurrentQueries = value;
		}
	});

	doc.getElementById('zotero-rag-min-book-pages').addEventListener('change', (e) => {
		const value = parseInt(/** @type {HTMLInputElement} */ (e.target).value);
		if (value >= 1) {
			Zotero.Prefs.set('extensions.zotero-rag.minBookPages', value, true);
		}
	});
```

- [ ] **Step 4: Commit**

```bash
git add plugin/src/preferences.xhtml plugin/src/preferences.js
git commit -m "feat: add minBookPages preference for Segment Book eligibility"
```

(No automated test — this is declarative XHTML + a DOM event listener identical in shape to the existing `maxQueries` field; correctness is confirmed by the manual live-test task at the end of this plan.)

---

### Task 5: `chapterActions.js` — helpers, menu registration, both flows

**Files:**
- Create: `plugin/src/chapterActions.js`
- Test: `plugin/test/chapterActions.test.js`
- Modify: `plugin/src/bootstrap.js`
- Modify: `plugin/src/zotero-rag.js`

This is the controller: menu registration on both the items-pane context menu and the Tools menu, the "Segment Book" flow (calls the existing `/api/chapter-linking/analyze` and `/api/chapter-linking/segment-upload` job endpoints), and the "Match Chapter" flow (local `Zotero.Search` + `FuzzyMatch.rankCandidates` + opens the dialog built in Task 6). The four pure/isolable helpers (`computeEnablement`, `findBookPdfAttachmentID`, `getPageCount`, `extractYear`) get unit tests; menu wiring and the network calls are exercised by the manual live-test task at the end of this plan (same convention `remote_indexer.test.js` follows — it doesn't unit-test its full upload flow either, only isolable pieces).

- [ ] **Step 1: Write the failing tests for the isolable helpers**

```js
// plugin/test/chapterActions.test.js
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const CHAPTER_LINKS_PATH = path.join(__dirname, '..', 'src', 'chapterLinks.js');
const CHAPTER_ACTIONS_PATH = path.join(__dirname, '..', 'src', 'chapterActions.js');

/**
 * ChapterActions references the global ChapterLinks (both are loaded into
 * the same window scope by bootstrap.js), so load chapterLinks.js into the
 * same vm context first, exactly matching the real load order.
 * @param {any} zoteroStub
 * @returns {any} the ChapterActions object
 */
function loadChapterActions(zoteroStub) {
	const context = { Zotero: zoteroStub, console };
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(CHAPTER_LINKS_PATH, 'utf8'), context, { filename: 'chapterLinks.js' });
	vm.runInContext(fs.readFileSync(CHAPTER_ACTIONS_PATH, 'utf8'), context, { filename: 'chapterActions.js' });
	return context.ChapterActions;
}

// computeEnablement runs inside the vm context and returns a freshly-built
// object literal there, so it belongs to that context's own realm --
// assert.deepStrictEqual would report a spurious mismatch against an
// object literal built in this (outer) realm, even with identical
// key/value contents, because it also compares [[Prototype]]. Compare the
// two boolean fields individually instead.
/**
 * @param {{segmentEnabled: boolean, matchEnabled: boolean}} actual
 * @param {boolean} segmentEnabled
 * @param {boolean} matchEnabled
 */
function assertEnablement(actual, segmentEnabled, matchEnabled) {
	assert.strictEqual(actual.segmentEnabled, segmentEnabled);
	assert.strictEqual(actual.matchEnabled, matchEnabled);
}

test('computeEnablement disables both actions unless exactly one item is selected', () => {
	const ChapterActions = loadChapterActions({});
	assertEnablement(ChapterActions.computeEnablement([]), false, false);
	assertEnablement(ChapterActions.computeEnablement([{ itemType: 'book' }, { itemType: 'book' }]), false, false);
});

test('computeEnablement enables Segment Book only for a single book item', () => {
	const ChapterActions = loadChapterActions({});
	assertEnablement(ChapterActions.computeEnablement([{ itemType: 'book' }]), true, false);
	assertEnablement(ChapterActions.computeEnablement([{ itemType: 'journalArticle' }]), false, false);
});

test('computeEnablement enables Match Chapter only for a single unlinked bookSection item', () => {
	const ChapterActions = loadChapterActions({});
	const unlinked = { itemType: 'bookSection', getExtraField: () => '' };
	const linked = { itemType: 'bookSection', getExtraField: () => 'users/42:BOOK1' };
	assertEnablement(ChapterActions.computeEnablement([unlinked]), false, true);
	assertEnablement(ChapterActions.computeEnablement([linked]), false, false);
});

test('findBookPdfAttachmentID returns the first PDF attachment id', () => {
	const items = {
		101: { id: 101, attachmentContentType: 'text/html' },
		102: { id: 102, attachmentContentType: 'application/pdf' },
	};
	const ChapterActions = loadChapterActions({ Items: { get: (id) => items[id] } });
	const item = { getAttachments: () => [101, 102] };
	assert.strictEqual(ChapterActions.findBookPdfAttachmentID(item), 102);
});

test('findBookPdfAttachmentID returns null when there is no PDF attachment', () => {
	const items = { 101: { id: 101, attachmentContentType: 'text/html' } };
	const ChapterActions = loadChapterActions({ Items: { get: (id) => items[id] } });
	const item = { getAttachments: () => [101] };
	assert.strictEqual(ChapterActions.findBookPdfAttachmentID(item), null);
});

test('getPageCount prefers the cached FullText page count', async () => {
	const ChapterActions = loadChapterActions({
		FullText: { getPages: async () => ({ indexedPages: 10, total: 250 }) },
		PDFWorker: { getFullText: async () => { throw new Error('should not be called'); } },
	});
	assert.strictEqual(await ChapterActions.getPageCount(999), 250);
});

test('getPageCount falls back to PDFWorker when FullText has no cached total', async () => {
	const ChapterActions = loadChapterActions({
		FullText: { getPages: async () => undefined },
		PDFWorker: { getFullText: async () => ({ totalPages: 42 }) },
	});
	assert.strictEqual(await ChapterActions.getPageCount(999), 42);
});

test('extractYear pulls the first 4-digit year out of a Zotero date string', () => {
	const ChapterActions = loadChapterActions({});
	assert.strictEqual(ChapterActions.extractYear('2020-05-01'), 2020);
	assert.strictEqual(ChapterActions.extractYear(''), null);
	assert.strictEqual(ChapterActions.extractYear(undefined), null);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test plugin/test/chapterActions.test.js`
Expected: FAIL — `plugin/src/chapterActions.js` doesn't exist yet.

- [ ] **Step 3: Write `plugin/src/chapterActions.js`**

```js
// plugin/src/chapterActions.js
// "Zotero RAG: Tools" per-item menu actions: Segment Book (client-side
// eligibility check, then the existing analyze/segment-upload backend
// pipeline scoped to one book, then opens /admin/review) and Match Chapter
// (fully client-side book-candidate search + link writing, no backend
// call). See
// docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md.
//
// Loaded by bootstrap.js after chapterLinks.js and fuzzyMatch.js, into the
// same window-scoped global environment as zotero-rag.js, so ChapterLinks
// and FuzzyMatch are available here as plain globals.

var ChapterActions = {
	/** @type {WeakMap<Window, Array<{target: EventTarget, type: string, handler: Function}>>} */
	_cleanupHandlers: new WeakMap(),

	// ---- Menu registration -------------------------------------------------

	/**
	 * @param {Document} doc
	 * @param {*} ui - plugin.toolkit.uiTool
	 * @param {string} idPrefix
	 * @param {*} plugin
	 * @param {Window} window
	 * @returns {{menu: Element, segmentItem: Element, matchItem: Element}}
	 */
	buildSubmenu(doc, ui, idPrefix, plugin, window) {
		const segmentItem = ui.createElement(doc, 'menuitem', {
			namespace: 'xul',
			id: `${idPrefix}-segment-book`,
			attributes: { label: 'Segment Book…' },
			listeners: [{ type: 'command', listener: () => this.handleSegmentBook(window, plugin) }],
		});
		const matchItem = ui.createElement(doc, 'menuitem', {
			namespace: 'xul',
			id: `${idPrefix}-match-chapter`,
			attributes: { label: 'Match Chapter…' },
			listeners: [{ type: 'command', listener: () => this.handleMatchChapter(window, plugin) }],
		});
		const menupopup = ui.createElement(doc, 'menupopup', {
			namespace: 'xul',
			id: `${idPrefix}-popup`,
		});
		menupopup.appendChild(segmentItem);
		menupopup.appendChild(matchItem);

		const menu = ui.createElement(doc, 'menu', {
			namespace: 'xul',
			id: `${idPrefix}-menu`,
			attributes: { label: 'Zotero RAG: Tools' },
		});
		menu.appendChild(menupopup);

		return { menu, segmentItem, matchItem };
	},

	/**
	 * @param {Array<*>} selectedItems
	 * @returns {{segmentEnabled: boolean, matchEnabled: boolean}}
	 */
	computeEnablement(selectedItems) {
		if (!selectedItems || selectedItems.length !== 1) {
			return { segmentEnabled: false, matchEnabled: false };
		}
		const item = selectedItems[0];
		const segmentEnabled = item.itemType === 'book';
		const matchEnabled = item.itemType === 'bookSection' && !ChapterLinks.hasContainedByLink(item);
		return { segmentEnabled, matchEnabled };
	},

	/**
	 * @param {Window} window
	 * @param {{segmentItem: Element, matchItem: Element}} refs
	 * @returns {void}
	 */
	updateMenuState(window, refs) {
		const selected = window.ZoteroPane.getSelectedItems();
		const { segmentEnabled, matchEnabled } = this.computeEnablement(selected);
		refs.segmentItem.disabled = !segmentEnabled;
		refs.matchItem.disabled = !matchEnabled;
	},

	/**
	 * @param {Window} window
	 * @param {EventTarget} target
	 * @param {string} type
	 * @param {Function} handler
	 * @returns {void}
	 */
	_registerCleanup(window, target, type, handler) {
		if (!this._cleanupHandlers.has(window)) {
			this._cleanupHandlers.set(window, []);
		}
		this._cleanupHandlers.get(window).push({ target, type, handler });
	},

	/**
	 * @param {Window} window
	 * @param {*} plugin
	 * @returns {void}
	 */
	addToWindow(window, plugin) {
		const doc = window.document;
		const ui = plugin.toolkit.uiTool;

		const itemMenuPopup = doc.getElementById('zotero-itemmenu');
		if (itemMenuPopup) {
			const { menu, segmentItem, matchItem } = this.buildSubmenu(doc, ui, 'zotero-rag-itemmenu', plugin, window);
			itemMenuPopup.appendChild(menu);
			plugin.storeAddedElement(menu);
			const handler = () => this.updateMenuState(window, { segmentItem, matchItem });
			itemMenuPopup.addEventListener('popupshowing', handler);
			this._registerCleanup(window, itemMenuPopup, 'popupshowing', handler);
		}

		const toolsMenuPopup = doc.getElementById('menu_ToolsPopup');
		if (toolsMenuPopup) {
			const { menu, segmentItem, matchItem } = this.buildSubmenu(doc, ui, 'zotero-rag-toolsmenu', plugin, window);
			toolsMenuPopup.appendChild(menu);
			plugin.storeAddedElement(menu);
			const handler = () => this.updateMenuState(window, { segmentItem, matchItem });
			toolsMenuPopup.addEventListener('popupshowing', handler);
			this._registerCleanup(window, toolsMenuPopup, 'popupshowing', handler);
		}
	},

	/**
	 * @param {Window} window
	 * @returns {void}
	 */
	removeFromWindow(window) {
		const handlers = this._cleanupHandlers.get(window) || [];
		for (const { target, type, handler } of handlers) {
			target.removeEventListener(type, handler);
		}
		this._cleanupHandlers.delete(window);
	},

	// ---- Segment Book -------------------------------------------------------

	/**
	 * @param {*} item - Zotero book item
	 * @returns {number|null} attachment item ID of the first PDF attachment, or null
	 */
	findBookPdfAttachmentID(item) {
		const attachmentIDs = item.getAttachments();
		for (const id of attachmentIDs) {
			const attachment = Zotero.Items.get(id);
			if (attachment && attachment.attachmentContentType === 'application/pdf') {
				return id;
			}
		}
		return null;
	},

	/**
	 * @param {number} attachmentID
	 * @returns {Promise<number|null>}
	 */
	async getPageCount(attachmentID) {
		const pages = await Zotero.FullText.getPages(attachmentID);
		if (pages && pages.total != null) {
			return pages.total;
		}
		const fullText = await Zotero.PDFWorker.getFullText(attachmentID, 1);
		return fullText ? fullText.totalPages : null;
	},

	/**
	 * @param {string} backendURL
	 * @param {string} path - e.g. "analyze", "segment-upload"
	 * @param {object} body
	 * @returns {Promise<string>} job_id
	 */
	async postChapterLinkingJob(backendURL, path, body) {
		const response = await fetch(`${backendURL}/api/chapter-linking/${path}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});
		if (!response.ok) {
			const errBody = await response.json().catch(() => ({}));
			throw new Error(errBody.detail || `POST /api/chapter-linking/${path}: HTTP ${response.status}`);
		}
		const data = await response.json();
		return data.job_id;
	},

	/**
	 * @param {string} backendURL
	 * @param {string} jobId
	 * @param {function(string): void} [onProgress]
	 * @returns {Promise<any>} the job's `result` field
	 */
	async pollChapterLinkingJob(backendURL, jobId, onProgress) {
		const POLL_INTERVAL_MS = 3000;
		while (true) {
			await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
			const response = await fetch(`${backendURL}/api/chapter-linking/jobs/${jobId}`);
			if (!response.ok) {
				throw new Error(`GET /api/chapter-linking/jobs/${jobId}: HTTP ${response.status}`);
			}
			const data = await response.json();
			if (onProgress) onProgress(data.message || '');
			if (data.status === 'done') return data.result;
			if (data.status === 'error') throw new Error(data.error || 'Job failed');
			// status === 'processing' -- keep polling
		}
	},

	/**
	 * @param {Window} window
	 * @param {*} plugin
	 * @returns {Promise<void>}
	 */
	async handleSegmentBook(window, plugin) {
		const selected = window.ZoteroPane.getSelectedItems();
		if (selected.length !== 1 || selected[0].itemType !== 'book') {
			plugin.showError('Select a single book item.');
			return;
		}
		const item = selected[0];
		const attachmentID = this.findBookPdfAttachmentID(item);
		if (attachmentID == null) {
			plugin.showError('No book-type attachment found');
			return;
		}
		const pageCount = await this.getPageCount(attachmentID);
		const minPages = Zotero.Prefs.get('extensions.zotero-rag.minBookPages', true) || 50;
		if (pageCount == null || pageCount < minPages) {
			plugin.showError('No book-type attachment found');
			return;
		}

		const slug = ChapterLinks.getZoteroSlug(item.libraryID);
		const backendURL = plugin.backendURL;

		try {
			plugin.toolkit.showNotification(`Analyzing "${item.getField('title')}"…`, 'default');
			const analyzeJobId = await this.postChapterLinkingJob(backendURL, 'analyze', {
				library_slug: slug,
				api_key: plugin.zoteroApiKey,
				item_keys: [item.key],
				enable_llm_fallback: true,
			});
			const analyzeResult = await this.pollChapterLinkingJob(backendURL, analyzeJobId,
				(msg) => plugin.toolkit.showNotification(msg, 'default'));

			const attachments = (analyzeResult && analyzeResult.attachments) || [];
			if (attachments.length === 0) {
				plugin.showError('No chapters detected — this book may already be linked, or the backend could not process its attachment.');
				return;
			}

			const needsOcrAny = attachments.some(a => a.needs_ocr);
			const readyAttachments = attachments.filter(a => !a.needs_ocr);
			if (needsOcrAny) {
				plugin.toolkit.showNotification('Some attachments need OCR before chapters can be detected — queued for review.', 'default');
			}
			if (readyAttachments.length > 0) {
				const uploadJobId = await this.postChapterLinkingJob(backendURL, 'segment-upload', {
					library_slug: slug,
					api_key: plugin.zoteroApiKey,
					analyses: readyAttachments,
					committed: false,
				});
				await this.pollChapterLinkingJob(backendURL, uploadJobId,
					(msg) => plugin.toolkit.showNotification(msg, 'default'));
			}
			Zotero.launchURL(`${backendURL}/admin/review?library_slug=${encodeURIComponent(slug)}`);
		} catch (err) {
			plugin.showError(`Segment Book failed: ${err.message}`);
		}
	},

	// ---- Match Chapter --------------------------------------------------

	/**
	 * @param {string} dateStr
	 * @returns {number|null}
	 */
	extractYear(dateStr) {
		const match = /(\d{4})/.exec(dateStr || '');
		return match ? parseInt(match[1], 10) : null;
	},

	/**
	 * @param {Window} window
	 * @param {*} plugin
	 * @returns {Promise<void>}
	 */
	async handleMatchChapter(window, plugin) {
		const selected = window.ZoteroPane.getSelectedItems();
		if (selected.length !== 1 || selected[0].itemType !== 'bookSection') {
			plugin.showError('Select a single book-section item.');
			return;
		}
		const chapterItem = selected[0];
		if (ChapterLinks.hasContainedByLink(chapterItem)) {
			plugin.showError('This chapter is already linked to a book.');
			return;
		}

		const s = new Zotero.Search();
		s.libraryID = chapterItem.libraryID;
		s.addCondition('itemType', 'is', 'book');
		const ids = await s.search();
		const books = Zotero.Items.get(ids).map(b => ({
			key: b.key,
			title: b.getField('title') || '',
			year: this.extractYear(b.getField('date')),
			creators: b.getCreators().map(c => [c.firstName, c.lastName].filter(Boolean).join(' ')).join('; '),
			_item: b,
		}));

		const chapterTitle = chapterItem.getField('bookTitle') || '';
		const chapterYear = this.extractYear(chapterItem.getField('date'));
		const candidates = FuzzyMatch.rankCandidates(chapterTitle, chapterYear, books, 5);

		window.openDialog(
			'chrome://zotero-rag/content/match-chapter-dialog.xhtml',
			'zotero-rag-match-chapter',
			'chrome,centerscreen,resizable=yes,width=640,height=480',
			{ plugin, chapterItem, chapterTitle, candidates }
		);
	},
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test plugin/test/chapterActions.test.js`
Expected: PASS — all 8 tests green.

- [ ] **Step 5: Wire the new scripts into `bootstrap.js`**

In `plugin/src/bootstrap.js`, change:

```js
	// Eager, plugin-lifetime script — loaded once at startup, not per dialog
	// window. dialog.js (loaded separately inside dialog.xhtml for that
	// window's own scope) depends on MentionSearch for its two-phase
	// "needs_client_evidence" protocol.
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
```

to:

```js
	// Eager, plugin-lifetime script — loaded once at startup, not per dialog
	// window. dialog.js (loaded separately inside dialog.xhtml for that
	// window's own scope) depends on MentionSearch for its two-phase
	// "needs_client_evidence" protocol.
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');

	// Book/chapter menu actions (Segment Book / Match Chapter) — loaded
	// before zotero-rag.js, which calls ChapterActions.addToWindow() /
	// removeFromWindow() and references the ChapterLinks global directly.
	Services.scriptloader.loadSubScript(rootURI + 'fuzzyMatch.js');
	Services.scriptloader.loadSubScript(rootURI + 'chapterLinks.js');
	Services.scriptloader.loadSubScript(rootURI + 'chapterActions.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
```

- [ ] **Step 6: Call `ChapterActions.addToWindow`/`removeFromWindow` from `zotero-rag.js`**

In `plugin/src/zotero-rag.js`, change:

```js
		const pane = /** @type {any} */ (window.ZoteroPane);
		if (pane && !pane._zoteroRagOrigOnCollectionSelected) {
			pane._zoteroRagOrigOnCollectionSelected = pane.onCollectionSelected;
			pane.onCollectionSelected = async (/** @type {any[]} */ ...args) => {
				const result = await pane._zoteroRagOrigOnCollectionSelected.apply(pane, args);
				clearTimeout(this._unavailableScanTimeout);
				this._unavailableScanTimeout = setTimeout(() => this._scanUnavailableCount(window), 150);
				return result;
			};
		}
	}

	/**
	 * Add plugin UI to all open Zotero windows.
	 * @returns {void}
	 */
	addToAllWindows() {
```

to:

```js
		const pane = /** @type {any} */ (window.ZoteroPane);
		if (pane && !pane._zoteroRagOrigOnCollectionSelected) {
			pane._zoteroRagOrigOnCollectionSelected = pane.onCollectionSelected;
			pane.onCollectionSelected = async (/** @type {any[]} */ ...args) => {
				const result = await pane._zoteroRagOrigOnCollectionSelected.apply(pane, args);
				clearTimeout(this._unavailableScanTimeout);
				this._unavailableScanTimeout = setTimeout(() => this._scanUnavailableCount(window), 150);
				return result;
			};
		}

		// @ts-ignore - ChapterActions is a global loaded by bootstrap.js
		if (typeof ChapterActions !== 'undefined') ChapterActions.addToWindow(window, this);
	}

	/**
	 * Add plugin UI to all open Zotero windows.
	 * @returns {void}
	 */
	addToAllWindows() {
```

- [ ] **Step 7: Call `ChapterActions.removeFromWindow` on cleanup**

In `plugin/src/zotero-rag.js`, change:

```js
		doc.querySelector('[href="zotero-rag.ftl"]')?.remove();
		// Restore wrapped ZoteroPane.onCollectionSelected
		const pane = /** @type {any} */ (window.ZoteroPane);
		if (pane && pane._zoteroRagOrigOnCollectionSelected) {
			pane.onCollectionSelected = pane._zoteroRagOrigOnCollectionSelected;
			delete pane._zoteroRagOrigOnCollectionSelected;
		}
	}
```

to:

```js
		doc.querySelector('[href="zotero-rag.ftl"]')?.remove();
		// Restore wrapped ZoteroPane.onCollectionSelected
		const pane = /** @type {any} */ (window.ZoteroPane);
		if (pane && pane._zoteroRagOrigOnCollectionSelected) {
			pane.onCollectionSelected = pane._zoteroRagOrigOnCollectionSelected;
			delete pane._zoteroRagOrigOnCollectionSelected;
		}

		// @ts-ignore - ChapterActions is a global loaded by bootstrap.js
		if (typeof ChapterActions !== 'undefined') ChapterActions.removeFromWindow(window);
	}
```

- [ ] **Step 8: Expose `ChapterLinks` on the plugin instance for dialog windows**

Dialog windows (`match-chapter-dialog.xhtml`, Task 6) run in their own separate JS global scope — `ChapterLinks` from `chapterLinks.js` isn't automatically available there the way it is in the main window, so the dialog needs it via the `plugin` object reference it's already passed. In `plugin/src/zotero-rag.js`, change:

```js
			// @ts-ignore - ZoteroPluginToolkit is a global variable
			this.toolkit = ZoteroPluginToolkit.createToolkit({ id, version, rootURI });
			this.log('Toolkit initialized successfully');
		} else {
			this.log('WARNING: Toolkit bundle not loaded');
		}
```

to:

```js
			// @ts-ignore - ZoteroPluginToolkit is a global variable
			this.toolkit = ZoteroPluginToolkit.createToolkit({ id, version, rootURI });
			this.log('Toolkit initialized successfully');
		} else {
			this.log('WARNING: Toolkit bundle not loaded');
		}

		// Expose ChapterLinks on the instance so dialog windows (which run in
		// their own separate JS global scope and don't have chapterLinks.js
		// loaded into it) can reach it via their passed-in `plugin` reference.
		// @ts-ignore - ChapterLinks is a global loaded by bootstrap.js
		this.chapterLinks = (typeof ChapterLinks !== 'undefined') ? ChapterLinks : null;
```

- [ ] **Step 9: Commit**

```bash
git add plugin/src/chapterActions.js plugin/test/chapterActions.test.js plugin/src/bootstrap.js plugin/src/zotero-rag.js
git commit -m "feat: add Segment Book / Match Chapter menu actions"
```

---

### Task 6: Match Chapter dialog

**Files:**
- Create: `plugin/src/match-chapter-dialog.xhtml`
- Create: `plugin/src/match-chapter-dialog.js`

Native dialog opened by `ChapterActions.handleMatchChapter` (Task 5, Step 3). Uses the `plugin`/`chapterItem`/`chapterTitle`/`candidates` passed via `window.arguments[0]`. No unit tests (DOM-only, exercised by the manual live-test task) — follows `fix-unavailable.xhtml`'s shell structure exactly (plain HTML body, no `VirtualizedTableHelper` needed since candidates are capped at 5).

- [ ] **Step 1: Write `plugin/src/match-chapter-dialog.xhtml`**

```xhtml
<?xml version="1.0"?>
<?xml-stylesheet href="chrome://global/skin/" type="text/css"?>
<?xml-stylesheet href="chrome://zotero/skin/zotero.css" type="text/css"?>
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml"
      xmlns:xul="http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul">

<head>
  <title>Match Chapter - Zotero RAG</title>
  <meta charset="utf-8"/>
  <script>
    document.addEventListener("DOMContentLoaded", () => {
      try {
        Services.scriptloader.loadSubScript(
          "chrome://zotero/content/include.js",
          window
        );
      } catch (e) {
        console.error("Failed to load include.js:", e);
      }
      try {
        Services.scriptloader.loadSubScript(
          "chrome://zotero-rag/content/match-chapter-dialog.js",
          window
        );
      } catch (e) {
        console.error("Failed to load match-chapter-dialog.js:", e);
      }
      if (window.MatchChapterDialog) window.MatchChapterDialog.init();
    });
  </script>
  <style>
    html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 13px; }
    .dialog-container { display: flex; flex-direction: column; height: 100%; padding: 12px 12px 0; box-sizing: border-box; }
    .dialog-header { flex-shrink: 0; margin-bottom: 8px; }
    .dialog-header p { margin: 2px 0; color: #555; font-size: 12px; line-height: 1.5; }
    #chapter-title { font-weight: bold; color: #333; margin: 0 0 2px 0; font-size: 13px; }
    #candidates-table-container { flex: 1; min-height: 0; overflow: auto; border: 1px solid #ccc; border-radius: 4px; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #eee; }
    th { position: sticky; top: 0; background: #f5f5f5; }
    .dialog-buttons { display: flex; align-items: center; gap: 8px; padding: 10px 0 12px; border-top: 1px solid #ddd; flex-shrink: 0; }
    #status-bar { flex: 1; font-size: 11px; color: #666; min-height: 15px; }
    .dialog-button { padding: 6px 16px; min-height: 32px; border: 1px solid #ccc; border-radius: 4px; background: #f5f5f5; color: #333; cursor: pointer; font-family: inherit; font-size: 13px; }
    .dialog-button:hover { background: #e5e5e5; }
    .dialog-button.primary { background: #0066cc; color: #fff; border-color: #0066cc; }
    .dialog-button.primary:hover { background: #0052a3; }
  </style>
</head>

<body>
  <div class="dialog-container">
    <div class="dialog-header">
      <p id="chapter-title"></p>
      <p>Chapter's own book title: <span id="chapter-book-title"></span></p>
      <p>Pick the matching book below, reject all, or create a new book item from this chapter's metadata.</p>
    </div>

    <div id="candidates-table-container">
      <table>
        <thead>
          <tr><th></th><th>Title</th><th>Creators</th><th>Year</th><th>Score</th></tr>
        </thead>
        <tbody id="candidates-body"></tbody>
      </table>
    </div>

    <div class="dialog-buttons">
      <div id="status-bar"></div>
      <button id="reject-btn" type="button" class="dialog-button">Reject All</button>
      <button id="create-btn" type="button" class="dialog-button">Create New Book Item</button>
      <button id="link-btn" type="button" class="dialog-button primary">Link Selected</button>
    </div>
  </div>
</body>
</html>
```

- [ ] **Step 2: Write `plugin/src/match-chapter-dialog.js`**

```js
// plugin/src/match-chapter-dialog.js
// Loaded into its own dialog window's scope by match-chapter-dialog.xhtml.
// See ChapterActions.handleMatchChapter (chapterActions.js) for how this
// dialog is opened and what window.arguments[0] contains. Runs in a
// separate JS global scope from the main Zotero window, so it reaches
// ChapterLinks and the ExtraFieldTool instance via the passed-in `plugin`
// object reference rather than via globals.

var MatchChapterDialog = {
	/** @type {*} */
	plugin: null,
	/** @type {*} */
	chapterItem: null,
	/** @type {string} */
	chapterTitle: '',
	/** @type {Array<*>} */
	candidates: [],
	/** @type {string|null} */
	selectedBookKey: null,

	init() {
		if (!window.arguments || !window.arguments[0]) {
			console.error('No arguments passed to Match Chapter dialog');
			return;
		}
		const args = window.arguments[0];
		this.plugin = args.plugin;
		this.chapterItem = args.chapterItem;
		this.chapterTitle = args.chapterTitle;
		this.candidates = args.candidates;

		document.getElementById('chapter-title').textContent = this.chapterItem.getField('title');
		document.getElementById('chapter-book-title').textContent = this.chapterTitle || '(none set on this chapter)';

		this._renderCandidates();

		document.getElementById('link-btn').addEventListener('click', () => this._onLinkSelected());
		document.getElementById('reject-btn').addEventListener('click', () => window.close());
		document.getElementById('create-btn').addEventListener('click', () => this._onCreateNewBook());
	},

	_renderCandidates() {
		const tbody = document.getElementById('candidates-body');
		tbody.textContent = '';
		this.candidates.forEach((candidate) => {
			const row = document.createElementNS('http://www.w3.org/1999/xhtml', 'tr');

			const radioCell = document.createElementNS('http://www.w3.org/1999/xhtml', 'td');
			const radio = document.createElementNS('http://www.w3.org/1999/xhtml', 'input');
			radio.type = 'radio';
			radio.name = 'candidate';
			radio.value = candidate.key;
			radio.addEventListener('change', () => { this.selectedBookKey = candidate.key; });
			radioCell.appendChild(radio);
			row.appendChild(radioCell);

			[candidate.title, candidate.creators, candidate.year != null ? String(candidate.year) : '', String(candidate.score)]
				.forEach(text => {
					const cell = document.createElementNS('http://www.w3.org/1999/xhtml', 'td');
					cell.textContent = text;
					row.appendChild(cell);
				});

			tbody.appendChild(row);
		});
	},

	async _onLinkSelected() {
		if (!this.selectedBookKey) {
			Services.prompt.alert(window, 'Zotero RAG', 'Select a candidate first.');
			return;
		}
		const candidate = this.candidates.find(c => c.key === this.selectedBookKey);
		await this.plugin.chapterLinks.writeLink(this.plugin.toolkit.extraField, candidate._item, this.chapterItem);
		window.close();
	},

	async _onCreateNewBook() {
		const bookItem = new Zotero.Item('book');
		bookItem.libraryID = this.chapterItem.libraryID;
		bookItem.setField('title', this.chapterTitle || this.chapterItem.getField('title'));
		for (const field of ['publisher', 'place', 'date', 'ISBN', 'language']) {
			const value = this.chapterItem.getField(field);
			if (value) bookItem.setField(field, value);
		}
		const editorTypeID = Zotero.CreatorTypes.getID('editor');
		const editors = this.chapterItem.getCreators().filter(c => c.creatorTypeID === editorTypeID);
		if (editors.length) {
			bookItem.setCreators(editors);
		}
		await bookItem.saveTx();

		await this.plugin.chapterLinks.writeLink(this.plugin.toolkit.extraField, bookItem, this.chapterItem);
		window.close();
	},
};
```

- [ ] **Step 3: Commit**

```bash
git add plugin/src/match-chapter-dialog.xhtml plugin/src/match-chapter-dialog.js
git commit -m "feat: add Match Chapter candidate-selection dialog"
```

---

### Task 7: Full test suite + manual live verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full plugin test suite**

Run: `npm run test:plugin`
Expected: PASS — every test in `plugin/test/*.test.js`, including the three new files from Tasks 1, 2, and 5.

- [ ] **Step 2: Start the dev servers**

Run: `npm start` (backend) and the plugin hot-reload dev server (`npm run start`, per this project's existing "Hot Reload Plugin Development Server" workflow — do not run `scripts/build_plugin.py`, only for final distribution builds).

- [ ] **Step 3: Reload the plugin into the running dev Zotero instance**

Use the MCP Bridge for Zotero plugin's `zotero_plugin_reload` tool (or restart the dev Zotero instance if the bridge isn't responding — see this project's "Never kill/restart Zotero by name" convention: resolve the dev instance's PID first via `ps aux | grep -i "Zotero.app/Contents/MacOS/zotero "`, confirm it carries `-profile "$ZOTERO_PLUGIN_PROFILE_PATH"`, before touching it).

- [ ] **Step 4: Manually verify "Segment Book" end-to-end against `test-rag-plugin`**

Per this project's "Live Query Debugging" convention, use the `test-rag-plugin` group library (`groups/6297749`) for this — never a real personal/project library. Via the MCP Bridge's `zotero_execute_js` tool, find (or create, if none exists yet) a `book`-type item in that library with a PDF attachment over the configured `minBookPages` threshold, then simulate the menu action directly (bypassing the actual right-click, since driving real mouse/menu interaction isn't practical from the bridge):

```js
return await (async () => {
  const win = Zotero.getMainWindow();
  const pane = win.ZoteroPane;
  // test-rag-plugin's Zotero.org group ID, per CLAUDE.md -- resolve the
  // numeric libraryID this session's local Zotero client uses for it.
  const libraryID = Zotero.Groups.get(6297749).libraryID;
  const s = new Zotero.Search();
  s.libraryID = libraryID;
  s.addCondition('itemType', 'is', 'book');
  const ids = await s.search();
  if (!ids.length) return { error: 'no book items found in test-rag-plugin' };
  const item = Zotero.Items.get(ids[0]);
  await pane.selectItem(item.id);
  await ChapterActions.handleSegmentBook(win, Zotero.ZoteroRAG);
  return { triggered: true, itemKey: item.key };
})();
```

Confirm: no JS errors in the Browser Console (`zotero_read_errors`/`zotero_read_logs`), a progress notification appears, and the default browser opens to `/admin/review?library_slug=groups/6297749` once the job completes. If the test library has no book with a qualifying PDF, this step should instead confirm the "No book-type attachment found" error path fires correctly for a book that's too short.

- [ ] **Step 5: Manually verify "Match Chapter" end-to-end against `test-rag-plugin`**

Find (or create) a `bookSection` item in `test-rag-plugin` with no `X-Contained-By` Extra-field line, and at least one plausible `book`-type item in the same library to match against:

```js
return await (async () => {
  const win = Zotero.getMainWindow();
  const pane = win.ZoteroPane;
  const libraryID = Zotero.Groups.get(6297749).libraryID;
  const s = new Zotero.Search();
  s.libraryID = libraryID;
  s.addCondition('itemType', 'is', 'bookSection');
  const ids = await s.search();
  const unlinked = Zotero.Items.get(ids).find(it => !ChapterLinks.hasContainedByLink(it));
  if (!unlinked) return { error: 'no unlinked bookSection items found in test-rag-plugin' };
  await pane.selectItem(unlinked.id);
  await ChapterActions.handleMatchChapter(win, Zotero.ZoteroRAG);
  return { triggered: true, itemKey: unlinked.key };
})();
```

Confirm the dialog opens showing the chapter's title and book title, a candidates table (possibly empty if the library has no plausible match), and that clicking "Link Selected" (after picking a radio) or "Create New Book Item" writes the `X-Contains`/`X-Contained-By` Extra-field lines and native relation — verify directly, substituting the `itemKey` the previous snippet returned:

```js
return await (async () => {
  const libraryID = Zotero.Groups.get(6297749).libraryID;
  const item = Zotero.Items.getByLibraryAndKey(libraryID, 'PASTE_ITEM_KEY_FROM_PREVIOUS_STEP');
  return {
    containedBy: item.getExtraField('X-Contained-By'),
    relatedItemKeys: item.relatedItems,
  };
})();
```

- [ ] **Step 6: Confirm no regressions in menu enablement**

With a non-book, non-bookSection item selected (e.g. a `journalArticle`), right-click in the items pane and confirm the "Zotero RAG: Tools" submenu's two entries are both disabled (grayed out), and confirm the same submenu appears (with the same disabled state) under the Tools menu.

This task has no commit of its own — it's verification of Tasks 1-6's combined work. If any step surfaces a bug, fix it in the relevant task's file and re-run the affected automated tests before re-attempting the manual step.
