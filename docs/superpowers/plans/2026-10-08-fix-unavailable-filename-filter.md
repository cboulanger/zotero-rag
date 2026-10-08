# Fix Unavailable Filename Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a filename filter box (with a clear "✕" control) to the Fix Unavailable Attachments dialog's table toolbar, and scope the existing "Select all" checkbox to only the currently-visible (filtered) rows.

**Architecture:** Add a display-index → real-index mapping array (`this.visibleIndices`) alongside the existing index-based `this.items`/`this.selected`/`this.rowStatus` state in `ZoteroFixUnavailableDialog` (`plugin/src/fix-unavailable.js`). The `VirtualizedTableHelper`'s row count/data/renderers resolve display position through this mapping; everything else (selection, delete, search & fix, debug collection) is untouched because it already operates on real indices via `this.selected`. See `docs/superpowers/specs/2026-10-08-fix-unavailable-filename-filter-design.md` for the full design rationale.

**Tech Stack:** Vanilla JS (Zotero plugin, XHTML/HTML dialog), Node's built-in test runner (`node --test`).

---

## Task 1: Create the feature branch

**Files:** none (git only)

- [ ] **Step 1: Confirm a clean working tree on `devel`**

```bash
cd /Users/cboulanger/Code/zotero-rag
git status --short
git branch --show-current
```
Expected: clean tree, current branch `devel`.

- [ ] **Step 2: Create and switch to the feature branch**

```bash
git checkout -b feature/fix-unavailable-filename-filter devel
```
Expected: `Switched to a new branch 'feature/fix-unavailable-filename-filter'`.

---

## Task 2: Extract `_filenameFor()` and reuse it in `getRowData`/`copySelectedRowsToClipboard`

The same "linked path or attachment filename" computation is duplicated in `getRowData` (inside `_initTable()`) and in `copySelectedRowsToClipboard()`. The upcoming filter needs this exact value too, so extract it once.

**Files:**
- Modify: `plugin/src/fix-unavailable.js`
- Test: `plugin/test/fix-unavailable.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/fix-unavailable.test.js` (anywhere after the `loadDialog` helper, e.g. right after the `_typeLabelFor` tests):

```js
test('_filenameFor returns the regular attachment filename for a non-linked file', () => {
	const dialog = loadDialog();
	const info = { isLinked: false, attachmentItem: { attachmentFilename: 'report.pdf', attachmentPath: '' } };
	assert.strictEqual(dialog._filenameFor(info), 'report.pdf');
});

test('_filenameFor returns the linked path for a linked file', () => {
	const dialog = loadDialog();
	const info = { isLinked: true, attachmentItem: { attachmentFilename: 'ignored.pdf', attachmentPath: '/Users/x/notes.pdf' } };
	assert.strictEqual(dialog._filenameFor(info), '/Users/x/notes.pdf');
});

test('_filenameFor falls back to an empty string when neither is set', () => {
	const dialog = loadDialog();
	const info = { isLinked: false, attachmentItem: {} };
	assert.strictEqual(dialog._filenameFor(info), '');
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: FAIL — `TypeError: dialog._filenameFor is not a function` for the three new tests; the other 37 still pass.

- [ ] **Step 3: Add `_filenameFor()` and use it from the two existing call sites**

In `plugin/src/fix-unavailable.js`, add this method right after `_typeLabelFor(info) { ... },` (around line 168, before `_initTable()`):

```js
	/**
	 * The filename shown for a row: the linked path for a linked attachment,
	 * otherwise the regular attachment filename. Shared by getRowData,
	 * copySelectedRowsToClipboard, and the filename filter.
	 * @param {AttachmentInfo} info
	 * @returns {string}
	 */
	_filenameFor(info) {
		const linkedPath = info.isLinked ? (info.attachmentItem.attachmentPath || '') : '';
		return linkedPath || info.attachmentItem.attachmentFilename || '';
	},
```

Then in `_initTable()`'s `getRowData`, replace:

```js
				getRowData: (/** @type {number} */ index) => {
					const info = this.items[index];
					if (!info) return { author: '', year: '', title: '', zoteroID: '', filename: '', status: '', select: '' };
					const linkedPath = info.isLinked ? (info.attachmentItem.attachmentPath || '') : '';
					const filename = linkedPath || info.attachmentItem.attachmentFilename || '';
					return {
```

with:

```js
				getRowData: (/** @type {number} */ index) => {
					const info = this.items[index];
					if (!info) return { author: '', year: '', title: '', zoteroID: '', filename: '', status: '', select: '' };
					const filename = this._filenameFor(info);
					return {
```

And in `copySelectedRowsToClipboard()`, replace:

```js
		const rows = indices.map(i => {
			const info = this.items[i];
			const linkedPath = info.isLinked ? (info.attachmentItem.attachmentPath || '') : '';
			const filename = linkedPath || info.attachmentItem.attachmentFilename || '';
			const status = this.rowStatus.get(i);
```

with:

```js
		const rows = indices.map(i => {
			const info = this.items[i];
			const filename = this._filenameFor(info);
			const status = this.rowStatus.get(i);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 40`, `pass 40`, `fail 0`.

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "refactor(plugin): extract _filenameFor() helper in fix-unavailable dialog"
```

---

## Task 3: Add `filterText`/`visibleIndices` state and `_computeVisibleIndices()`

**Files:**
- Modify: `plugin/src/fix-unavailable.js`
- Test: `plugin/test/fix-unavailable.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/fix-unavailable.test.js`:

```js
test('_computeVisibleIndices includes every row when filterText is empty', () => {
	const dialog = loadDialog();
	dialog.filterText = '';
	dialog.items = [
		{ isLinked: false, attachmentItem: { attachmentFilename: 'a.pdf' } },
		{ isLinked: false, attachmentItem: { attachmentFilename: 'b.docx' } },
	];
	dialog._computeVisibleIndices();
	assert.deepStrictEqual(dialog.visibleIndices, [0, 1]);
});

test('_computeVisibleIndices matches the filename case-insensitively, substring anywhere', () => {
	const dialog = loadDialog();
	dialog.filterText = 'PDF';
	dialog.items = [
		{ isLinked: false, attachmentItem: { attachmentFilename: 'report.pdf' } },
		{ isLinked: false, attachmentItem: { attachmentFilename: 'notes.docx' } },
		{ isLinked: false, attachmentItem: { attachmentFilename: 'pdf-scan.docx' } },
	];
	dialog._computeVisibleIndices();
	assert.deepStrictEqual(dialog.visibleIndices, [0, 2]);
});

test('_computeVisibleIndices is empty when nothing matches', () => {
	const dialog = loadDialog();
	dialog.filterText = 'zzz';
	dialog.items = [{ isLinked: false, attachmentItem: { attachmentFilename: 'a.pdf' } }];
	dialog._computeVisibleIndices();
	assert.deepStrictEqual(dialog.visibleIndices, []);
});

test('_computeVisibleIndices trims surrounding whitespace from the filter text', () => {
	const dialog = loadDialog();
	dialog.filterText = '  pdf  ';
	dialog.items = [{ isLinked: false, attachmentItem: { attachmentFilename: 'a.pdf' } }];
	dialog._computeVisibleIndices();
	assert.deepStrictEqual(dialog.visibleIndices, [0]);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: FAIL — `dialog.filterText` assignment itself is harmless (plain object), but `dialog._computeVisibleIndices is not a function` for all four new tests.

- [ ] **Step 3: Add the state fields and the method**

In `plugin/src/fix-unavailable.js`, add two new fields to the `ZoteroFixUnavailableDialog` object, right after the `selected` field declaration (around line 80, after `selected: new Set(),`):

```js

	/**
	 * Current text typed into the filename filter box.
	 * @type {string}
	 */
	filterText: '',

	/**
	 * Display position -> real index into this.items, recomputed whenever
	 * this.items or this.filterText changes. The VirtualizedTable's
	 * getRowCount/getRowData/cell renderers are called with a display
	 * position and must resolve through this array before touching
	 * this.items/this.selected/this.rowStatus.
	 * @type {Array<number>}
	 */
	visibleIndices: [],
```

Then add the method right after `_filenameFor()` (added in Task 2):

```js

	/**
	 * Recompute this.visibleIndices from this.items + this.filterText.
	 * Call after this.items changes, or after this.filterText changes.
	 * @returns {void}
	 */
	_computeVisibleIndices() {
		const q = this.filterText.trim().toLowerCase();
		if (!q) {
			this.visibleIndices = this.items.map((_, i) => i);
			return;
		}
		this.visibleIndices = [];
		for (let i = 0; i < this.items.length; i++) {
			if (this._filenameFor(this.items[i]).toLowerCase().includes(q)) {
				this.visibleIndices.push(i);
			}
		}
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 44`, `pass 44`, `fail 0`.

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat(plugin): add visibleIndices filename-filter matching to fix-unavailable dialog"
```

---

## Task 4: Recompute `visibleIndices` after `populateTable()` and after dropping fixed rows in `searchAndFix()`

**Files:**
- Modify: `plugin/src/fix-unavailable.js`
- Test: `plugin/test/fix-unavailable.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/fix-unavailable.test.js`:

```js
test('populateTable recomputes visibleIndices to match the freshly loaded items, honoring a persisted filter', async () => {
	const dialog = loadDialog({ Zotero: { Libraries: { get: () => ({ name: 'Test Library' }) } } });
	dialog.libraryID = 1;
	dialog.backendLibraryId = 'u1';
	dialog.rowStatus = new Map();
	dialog.selected = new Set();
	dialog.filterText = 'pdf';
	dialog.plugin = {
		_getUnavailableAttachments: async () => ([
			{ attachmentItem: { key: 'A1', attachmentFilename: 'report.pdf' }, isLinked: false },
			{ attachmentItem: { key: 'A2', attachmentFilename: 'notes.docx' }, isLinked: false },
		]),
	};

	await dialog.populateTable();

	assert.deepStrictEqual(dialog.visibleIndices, [0]);
});

test('searchAndFix recomputes visibleIndices after dropping fixed rows, preserving an active filter', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0, 1]);
	dialog.filterText = 'doc';
	dialog.items = [
		{ attachmentItem: { key: 'FIXED', attachmentFilename: 'report.pdf' }, isLinked: false },
		{ attachmentItem: { key: 'KEPT', attachmentFilename: 'notes.doc' }, isLinked: false },
	];
	dialog.plugin = {
		_tryDownloadAttachment: async (att) => ({ downloaded: att.key === 'FIXED' }),
		_searchAndFixUnavailableAttachment: async () => ({ found: false }),
	};

	await dialog.searchAndFix();

	// 'FIXED' (report.pdf) is gone; the single remaining row ('notes.doc') matches "doc".
	assert.strictEqual(dialog.items.length, 1);
	assert.deepStrictEqual(dialog.visibleIndices, [0]);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: FAIL — both new tests fail because `dialog.visibleIndices` is still `[]` (never recomputed by either method yet).

- [ ] **Step 3: Call `_computeVisibleIndices()` at the end of `populateTable()`**

In `plugin/src/fix-unavailable.js`, in `populateTable()`, replace:

```js
		// Pre-check all rows in our independent checkbox set
		this.selected.clear();
		for (let i = 0; i < this.items.length; i++) this.selected.add(i);

		if (this.tableHelper?.treeInstance) {
```

with:

```js
		// Pre-check all rows in our independent checkbox set
		this.selected.clear();
		for (let i = 0; i < this.items.length; i++) this.selected.add(i);
		this._computeVisibleIndices();

		if (this.tableHelper?.treeInstance) {
```

- [ ] **Step 4: Call `_computeVisibleIndices()` in the fixed-row-removal block of `searchAndFix()`**

In the same file, in `searchAndFix()`, replace:

```js
			this.items = survivingItems;
			this.rowStatus = survivingRowStatus;
			this.selected = survivingSelected;
			if (this.plugin && typeof this.plugin.updateMissingFilesCount === 'function') {
```

with:

```js
			this.items = survivingItems;
			this.rowStatus = survivingRowStatus;
			this.selected = survivingSelected;
			this._computeVisibleIndices();
			if (this.plugin && typeof this.plugin.updateMissingFilesCount === 'function') {
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 46`, `pass 46`, `fail 0`.

- [ ] **Step 6: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat(plugin): recompute visibleIndices after table reloads/row removal"
```

---

## Task 5: Resolve display index through `visibleIndices` in `_initTable()`

This wires the actual `VirtualizedTableHelper` row count/data/renderers to the filtered view. There is no automated test for this step: `_initTable()` is only reachable through `init()`, which the existing test suite deliberately never calls (see the header comment in `plugin/test/fix-unavailable.test.js` — tests exercise dialog methods directly with a `window` stub that has no `.arguments`, which makes `init()` return immediately). Verify this step manually in Task 9.

**Files:**
- Modify: `plugin/src/fix-unavailable.js`

- [ ] **Step 1: Resolve `getRowCount`/`getRowData` through `visibleIndices`**

In `_initTable()`, replace:

```js
					getRowCount: () => this.items.length,
					getRowData: (/** @type {number} */ index) => {
						const info = this.items[index];
						if (!info) return { author: '', year: '', title: '', zoteroID: '', filename: '', status: '', select: '' };
						const filename = this._filenameFor(info);
						return {
```

with:

```js
					getRowCount: () => this.visibleIndices.length,
					getRowData: (/** @type {number} */ displayIndex) => {
						const index = this.visibleIndices[displayIndex];
						const info = this.items[index];
						if (!info) return { author: '', year: '', title: '', zoteroID: '', filename: '', status: '', select: '' };
						const filename = this._filenameFor(info);
						return {
```

- [ ] **Step 2: Resolve `checkboxCellRenderer` through `visibleIndices`**

Replace:

```js
		const checkboxCellRenderer = (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
			const span = document.createElement('span');
			span.className = `cell ${column.className}`;
			span.style.cssText = 'display:flex;align-items:center;justify-content:center;';
			const cb = document.createElement('input');
			cb.type = 'checkbox';
			cb.checked = this.selected.has(index);
			cb.style.margin = '0';
			cb.addEventListener('change', () => {
				if (cb.checked) {
					this.selected.add(index);
				} else {
					this.selected.delete(index);
				}
				this.updateActionButtons();
			});
			span.appendChild(cb);
			return span;
		};
```

with:

```js
		const checkboxCellRenderer = (/** @type {number} */ displayIndex, /** @type {string} */ _data, /** @type {any} */ column) => {
			const index = this.visibleIndices[displayIndex];
			const span = document.createElement('span');
			span.className = `cell ${column.className}`;
			span.style.cssText = 'display:flex;align-items:center;justify-content:center;';
			const cb = document.createElement('input');
			cb.type = 'checkbox';
			cb.checked = this.selected.has(index);
			cb.style.margin = '0';
			cb.addEventListener('change', () => {
				if (cb.checked) {
					this.selected.add(index);
				} else {
					this.selected.delete(index);
				}
				this.updateActionButtons();
			});
			span.appendChild(cb);
			return span;
		};
```

- [ ] **Step 3: Resolve `statusCellRenderer` through `visibleIndices`**

Replace:

```js
		const statusCellRenderer = (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
			const span = document.createElement('span');
			const status = this.rowStatus.get(index);
			span.className = `cell ${column.className}${status ? ' status-' + status.cssClass : ''}`;
			span.textContent = status ? status.text : '';
			if (status?.tooltip) span.title = status.tooltip;
			return span;
		};
```

with:

```js
		const statusCellRenderer = (/** @type {number} */ displayIndex, /** @type {string} */ _data, /** @type {any} */ column) => {
			const index = this.visibleIndices[displayIndex];
			const span = document.createElement('span');
			const status = this.rowStatus.get(index);
			span.className = `cell ${column.className}${status ? ' status-' + status.cssClass : ''}`;
			span.textContent = status ? status.text : '';
			if (status?.tooltip) span.title = status.tooltip;
			return span;
		};
```

- [ ] **Step 4: Resolve `selectCellRenderer` through `visibleIndices`**

Replace:

```js
		const selectCellRenderer = (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
			const span = document.createElement('span');
			span.className = `cell ${column.className}`;
			const btn = document.createElement('button');
			btn.className = 'select-btn';
			btn.textContent = '🔍';
			btn.title = 'Select in Zotero';
			btn.addEventListener('mousedown', e => e.stopPropagation());
			btn.addEventListener('click', e => {
				e.stopPropagation();
				const info = this.items[index];
				if (info) this.selectItemInZotero(info);
			});
			span.appendChild(btn);
			return span;
		};
```

with:

```js
		const selectCellRenderer = (/** @type {number} */ displayIndex, /** @type {string} */ _data, /** @type {any} */ column) => {
			const index = this.visibleIndices[displayIndex];
			const span = document.createElement('span');
			span.className = `cell ${column.className}`;
			const btn = document.createElement('button');
			btn.className = 'select-btn';
			btn.textContent = '🔍';
			btn.title = 'Select in Zotero';
			btn.addEventListener('mousedown', e => e.stopPropagation());
			btn.addEventListener('click', e => {
				e.stopPropagation();
				const info = this.items[index];
				if (info) this.selectItemInZotero(info);
			});
			span.appendChild(btn);
			return span;
		};
```

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 46`, `pass 46`, `fail 0` (unchanged from Task 4 — this task only touches code paths the suite doesn't exercise).

- [ ] **Step 6: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.js
git commit -m "feat(plugin): render the fix-unavailable table through visibleIndices"
```

---

## Task 6: Scope "Select all" to the visible subset

**Files:**
- Modify: `plugin/src/fix-unavailable.js`
- Test: `plugin/test/fix-unavailable.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/fix-unavailable.test.js`:

```js
test('_handleSelectAllChange(true) selects only the currently visible indices', () => {
	const dialog = loadDialog();
	dialog.items = [{}, {}, {}];
	dialog.visibleIndices = [0, 2];
	dialog.selected = new Set();
	dialog.updateActionButtons = () => {};
	dialog._handleSelectAllChange(true);
	assert.deepStrictEqual([...dialog.selected].sort(), [0, 2]);
});

test('_handleSelectAllChange(false) clears the entire selection, including hidden rows', () => {
	const dialog = loadDialog();
	dialog.items = [{}, {}, {}];
	dialog.visibleIndices = [0, 2];
	dialog.selected = new Set([0, 1, 2]);
	dialog.updateActionButtons = () => {};
	dialog._handleSelectAllChange(false);
	assert.deepStrictEqual([...dialog.selected], []);
});

test('_updateSelectAllCheckbox is checked when every visible row is selected, even if a hidden row is not', () => {
	const cb = { checked: false, indeterminate: false };
	const dialog = loadDialog({
		document: { getElementById: (id) => (id === 'select-all-cb' ? cb : { addEventListener: () => {}, style: {} }) },
	});
	dialog.items = [{}, {}, {}];
	dialog.visibleIndices = [0, 2]; // row 1 is hidden by the filter
	dialog.selected = new Set([0, 2]); // every visible row selected; hidden row 1 is not
	dialog._updateSelectAllCheckbox();
	assert.strictEqual(cb.checked, true);
	assert.strictEqual(cb.indeterminate, false);
});

test('_updateSelectAllCheckbox is indeterminate when only some visible rows are selected', () => {
	const cb = { checked: false, indeterminate: false };
	const dialog = loadDialog({
		document: { getElementById: (id) => (id === 'select-all-cb' ? cb : { addEventListener: () => {}, style: {} }) },
	});
	dialog.items = [{}, {}, {}];
	dialog.visibleIndices = [0, 2];
	dialog.selected = new Set([0]);
	dialog._updateSelectAllCheckbox();
	assert.strictEqual(cb.checked, false);
	assert.strictEqual(cb.indeterminate, true);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: FAIL — `_handleSelectAllChange` tests fail with "is not a function"; the two `_updateSelectAllCheckbox` tests fail because it still compares against `this.items.length` (all three items), so with `selected = {0, 2}` and `items.length = 3`, the "checked" test currently gets `checked: false` instead of `true`, and the "indeterminate" test happens to already pass by coincidence — confirm by running, don't assume.

- [ ] **Step 3: Add `_handleSelectAllChange()` and update `_updateSelectAllCheckbox()`**

Add this method right after `_computeVisibleIndices()`:

```js

	/**
	 * Handle a change on the "Select all" toolbar checkbox: checking it
	 * selects only the currently-visible (filtered) rows; unchecking it
	 * clears the entire selection, visible or hidden.
	 * @param {boolean} checked
	 * @returns {void}
	 */
	_handleSelectAllChange(checked) {
		if (checked) {
			for (const i of this.visibleIndices) this.selected.add(i);
		} else {
			this.selected.clear();
		}
		this.tableHelper?.treeInstance?.invalidate();
		this.updateActionButtons();
	},
```

Then replace `_updateSelectAllCheckbox()`:

```js
	_updateSelectAllCheckbox() {
		const cb = /** @type {HTMLInputElement|null} */ (document.getElementById('select-all-cb'));
		if (!cb) return;
		const count = this.selected.size;
		const total = this.items.length;
		cb.checked = total > 0 && count === total;
		cb.indeterminate = count > 0 && count < total;
	},
```

with:

```js
	_updateSelectAllCheckbox() {
		const cb = /** @type {HTMLInputElement|null} */ (document.getElementById('select-all-cb'));
		if (!cb) return;
		const total = this.visibleIndices.length;
		const count = this.visibleIndices.filter(i => this.selected.has(i)).length;
		cb.checked = total > 0 && count === total;
		cb.indeterminate = count > 0 && count < total;
	},
```

- [ ] **Step 4: Wire the toolbar checkbox's listener to the new method**

In `init()`, replace:

```js
		document.getElementById('select-all-cb')?.addEventListener('change', (/** @type {Event} */ e) => {
			if (/** @type {HTMLInputElement} */(e.target).checked) {
				for (let i = 0; i < this.items.length; i++) this.selected.add(i);
			} else {
				this.selected.clear();
			}
			this.tableHelper?.treeInstance?.invalidate();
			this.updateActionButtons();
		});
```

with:

```js
		document.getElementById('select-all-cb')?.addEventListener('change', (/** @type {Event} */ e) => {
			this._handleSelectAllChange(/** @type {HTMLInputElement} */ (e.target).checked);
		});
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 50`, `pass 50`, `fail 0`.

- [ ] **Step 6: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat(plugin): scope Select All to currently-visible rows"
```

---

## Task 7: Add `_handleFilterInput()` / `_handleFilterClear()`

**Files:**
- Modify: `plugin/src/fix-unavailable.js`
- Test: `plugin/test/fix-unavailable.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/fix-unavailable.test.js`:

```js
test('_handleFilterInput updates filterText and recomputes visibleIndices without touching the selection', () => {
	const dialog = loadDialog();
	dialog.items = [
		{ isLinked: false, attachmentItem: { attachmentFilename: 'report.pdf' } },
		{ isLinked: false, attachmentItem: { attachmentFilename: 'notes.docx' } },
	];
	dialog.selected = new Set([1]);
	dialog.updateActionButtons = () => {};
	dialog._handleFilterInput('pdf');
	assert.strictEqual(dialog.filterText, 'pdf');
	assert.deepStrictEqual(dialog.visibleIndices, [0]);
	// Selection (including the now-hidden row 1) is untouched by filtering.
	assert.deepStrictEqual([...dialog.selected], [1]);
});

test('_handleFilterClear resets filterText to empty and restores every index as visible', () => {
	const dialog = loadDialog();
	dialog.items = [
		{ isLinked: false, attachmentItem: { attachmentFilename: 'report.pdf' } },
		{ isLinked: false, attachmentItem: { attachmentFilename: 'notes.docx' } },
	];
	dialog.filterText = 'pdf';
	dialog.visibleIndices = [0];
	dialog.updateActionButtons = () => {};
	dialog._handleFilterClear();
	assert.strictEqual(dialog.filterText, '');
	assert.deepStrictEqual(dialog.visibleIndices, [0, 1]);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: FAIL — both "is not a function".

- [ ] **Step 3: Add both methods**

Add right after `_handleSelectAllChange()`:

```js

	/**
	 * Handle input into the filename filter box: update filterText and
	 * recompute which rows are visible. Does not touch this.selected —
	 * hidden rows keep whatever selection state they had.
	 * @param {string} value
	 * @returns {void}
	 */
	_handleFilterInput(value) {
		this.filterText = value;
		this._computeVisibleIndices();
		this.tableHelper?.treeInstance?.invalidate();
		this.updateActionButtons();
	},

	/**
	 * Clear the filename filter, restoring every row as visible.
	 * @returns {void}
	 */
	_handleFilterClear() {
		this._handleFilterInput('');
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 52`, `pass 52`, `fail 0`.

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat(plugin): add filter-input/clear handlers to fix-unavailable dialog"
```

---

## Task 8: Add the filter box markup, CSS, and event wiring

**Files:**
- Modify: `plugin/src/fix-unavailable.xhtml`
- Modify: `plugin/src/fix-unavailable.js`

No automated test (DOM markup + wiring); verify manually in Task 9.

- [ ] **Step 1: Add the toolbar markup**

In `plugin/src/fix-unavailable.xhtml`, in `#table-toolbar`, replace:

```html
      <button id="refresh-btn" type="button" class="toolbar-btn" title="Reload the list of missing files">Refresh</button>
    </div>
```

with:

```html
      <button id="refresh-btn" type="button" class="toolbar-btn" title="Reload the list of missing files">Refresh</button>
      <div style="flex:1"></div>
      <div id="filter-wrap">
        <input type="text" id="filename-filter" placeholder="Filter filename…"
               title="Filter rows by attachment filename"/>
        <button id="filename-filter-clear" type="button" title="Clear filter"
                aria-label="Clear filter" hidden="true">&#x2715;</button>
      </div>
    </div>
```

- [ ] **Step 2: Add the CSS**

In the same file's `<style>` block, replace:

```css
    #table-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; flex-shrink: 0; }
    #table-toolbar label { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #555; cursor: pointer; user-select: none; }
```

with:

```css
    #table-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; flex-shrink: 0; }
    #table-toolbar label { display: flex; align-items: center; gap: 4px; font-size: 12px; color: #555; cursor: pointer; user-select: none; }
    #filter-wrap { position: relative; display: flex; align-items: center; }
    #filename-filter { width: 180px; padding: 2px 22px 2px 8px; border: 1px solid #ccc; border-radius: 3px; font-family: inherit; font-size: 12px; height: 22px; box-sizing: border-box; }
    #filename-filter-clear { position: absolute; right: 2px; top: 50%; transform: translateY(-50%); border: none; background: none; padding: 2px 4px; cursor: pointer; font-size: 11px; color: #888; line-height: 1; }
    #filename-filter-clear:hover { color: #0066cc; }
    #filename-filter-clear[hidden] { display: none; }
```

- [ ] **Step 3: Wire the input and clear button in `init()`**

In `plugin/src/fix-unavailable.js`'s `init()`, right after the `select-all-cb` listener block (the one now calling `_handleSelectAllChange`, from Task 6 Step 4), add:

```js
		const filterInput = /** @type {HTMLInputElement|null} */ (document.getElementById('filename-filter'));
		const filterClearBtn = /** @type {HTMLButtonElement|null} */ (document.getElementById('filename-filter-clear'));
		filterInput?.addEventListener('input', () => {
			this._handleFilterInput(filterInput.value);
			if (filterClearBtn) filterClearBtn.hidden = !this.filterText;
		});
		filterClearBtn?.addEventListener('click', () => {
			if (filterInput) filterInput.value = '';
			this._handleFilterClear();
			filterClearBtn.hidden = true;
			filterInput?.focus();
		});
```

- [ ] **Step 4: Run the full test suite to confirm nothing broke**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: `tests 52`, `pass 52`, `fail 0` (unchanged — this step only adds markup and `init()`-only wiring the suite doesn't exercise).

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add plugin/src/fix-unavailable.xhtml plugin/src/fix-unavailable.js
git commit -m "feat(plugin): add filename filter box to fix-unavailable dialog toolbar"
```

---

## Task 9: Manual verification and final check

**Files:** none (verification only)

- [ ] **Step 1: Run the full plugin test suite one more time**

```bash
cd /Users/cboulanger/Code/zotero-rag/plugin
node --test test/fix-unavailable.test.js
```
Expected: all tests pass, 0 failures.

- [ ] **Step 2: Manually verify in the live dev Zotero instance**

Per `CLAUDE.md`'s "Hot Reload Plugin Development Server" and "Testing plugin JS directly against the live Zotero library" sections: with the plugin dev server running (hot reload — no rebuild needed) against the `test-rag-plugin` library, open the Fix Unavailable Attachments dialog and check:

1. Typing a filter matching a subset of filenames (e.g. an extension like `pdf`) shows only matching rows.
2. Select a few rows, type a filter that hides them, confirm the Delete/Search & Fix buttons stay enabled (selection count unchanged) — then clear the filter and confirm those rows are still checked.
3. With a filter active, click "Select all" → only currently-visible rows become checked. Uncheck it → every row (visible and hidden) is cleared.
4. Click the "✕" clear button → filter box empties, full list reappears, clear button hides again.
5. Click "Refresh" while a filter is active → the filter text and matching persist against the newly reloaded list.

- [ ] **Step 3: Report results**

If any manual check fails, fix the relevant task's code before proceeding — do not mark this task done with a known-failing manual check.
