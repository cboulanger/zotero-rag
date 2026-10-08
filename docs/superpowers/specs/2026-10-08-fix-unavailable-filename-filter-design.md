# Fix Unavailable: Filename Filter — Design Spec

## 1. Goal

The **Fix Unavailable Attachments** dialog's table has no way to narrow down
the list. For libraries with many unavailable attachments, the main practical
need is to isolate rows by file extension (e.g. "show only `.pdf`s I haven't
fixed yet") before selecting and acting on them. This adds a filter input to
the table toolbar that filters rows by attachment filename, plus a clear
("✕") control, and scopes the existing "Select all" checkbox to the
currently-visible (filtered) rows.

## 2. Current state

- **Dialog** — `plugin/src/fix-unavailable.xhtml` / `fix-unavailable.js`
  (`ZoteroFixUnavailableDialog`).
- **Toolbar** — `#table-toolbar`: `[Select all checkbox] [Include missing
  attachments checkbox] [Refresh button]`, `display:flex; gap:10px`.
- **Table** — a `VirtualizedTableHelper` instance (`_initTable()`). Row
  rendering is index-based end to end:
  - `getRowCount: () => this.items.length`
  - `getRowData: (index) => ...` reads `this.items[index]`
  - Three custom cell renderers (`checkboxCellRenderer`, `statusCellRenderer`,
    `selectCellRenderer`) are each called with that same `index` and read/write
    `this.selected` (a `Set<number>` of checked row indices) and
    `this.rowStatus` (a `Map<number, RowStatus>`) directly by that index.
  - Whenever `this.items` is mutated (e.g. `populateTable()` reloading, or the
    fixed-row-removal block at the end of `searchAndFix()`), `this.selected`
    and `this.rowStatus` are rebuilt in lockstep so indices stay aligned
    (see `searchAndFix()` lines ~1184-1205 for the existing pattern).
  - The filename shown per row (`getRowData`'s `filename` field, and the
    `filename` field in `copySelectedRowsToClipboard()`) is computed the same
    way in two places: `info.isLinked ? info.attachmentItem.attachmentPath :
    info.attachmentItem.attachmentFilename`.
- **Select all** — a single checkbox (`#select-all-cb`), not separate
  select-all/unselect-all buttons. Checking it adds every index `0..items.length`
  to `this.selected`; unchecking clears `this.selected` entirely.
  `_updateSelectAllCheckbox()` sets its checked/indeterminate state from
  `this.selected.size` vs `this.items.length`.

## 3. Scope decisions

| Decision | Choice |
| --- | --- |
| What's matched | Attachment filename only (the same value already shown in the Filename column), not author/title/status |
| Match rule | Case-insensitive substring, anywhere in the filename |
| Hidden-but-selected rows | Stay selected while hidden by the filter. "Search & Fix"/"Delete" still act on them if triggered while hidden |
| "Select all" checkbox, when checked | Selects only the currently-visible (filtered) rows, not all rows |
| "Select all" checkbox, when unchecked | Clears the entire selection, visible or hidden (unchanged from today — an explicit full clear, not filter-scoped) |
| "Select all" checkbox display state | Checked/indeterminate computed from the visible subset only (visible-selected-count vs visible-total), not the full item list |
| Filter text across Refresh | Persists; reapplied against the newly loaded list |
| Status bar counts ("N unavailable attachments found") | Unchanged — still reports the total, not the filtered count. Out of scope |

Filtering must never change `this.items`, `this.selected`, or `this.rowStatus`
semantics — it only changes which indices the table currently displays.

## 4. Implementation approach

### 4.1 Why a display-index mapping (not a split item list)

Two approaches were considered:

- **Rejected: split into `allItems` (canonical) + `items` (filtered subset).**
  This breaks the existing index-based design: `this.selected`/`this.rowStatus`
  would need to switch from index-keyed to identity-keyed (e.g. by
  `attachmentItem.key`) everywhere they're read or written — the checkbox
  renderer, `_updateSelectAllCheckbox`, `getSelectedIndices`, `deleteSelected`,
  every phase of `searchAndFix`, and debug-report collection. That's a large,
  invasive change for this feature's scope, and risks losing the "hidden rows
  stay selected" behavior during the remap.
- **Chosen: add a display-index → real-index mapping layer.** `this.items`,
  `this.selected`, and `this.rowStatus` are untouched. A new array,
  `this.visibleIndices`, maps the table's display position (what
  `getRowCount`/`getRowData`/the renderers are called with) to the real index
  into `this.items`. Everything downstream of selection (delete, search & fix,
  debug collection) keeps working unmodified because it already operates on
  real indices via `this.selected`, never on display position.

### 4.2 New state and helper

```js
/** @type {string} */
filterText: '',

/** @type {Array<number>} */
visibleIndices: [],
```

```js
/**
 * The filename shown/matched for a row (shared by getRowData,
 * copySelectedRowsToClipboard, and the filter).
 * @param {AttachmentInfo} info
 * @returns {string}
 */
_filenameFor(info) {
	const linkedPath = info.isLinked ? (info.attachmentItem.attachmentPath || '') : '';
	return linkedPath || info.attachmentItem.attachmentFilename || '';
},

/**
 * Recompute this.visibleIndices from this.items + this.filterText.
 * Call after this.items changes, or after filterText changes.
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

`getRowData` and `copySelectedRowsToClipboard` are updated to call
`_filenameFor(info)` instead of repeating the inline computation.

### 4.3 Table wiring (`_initTable()`)

- `getRowCount: () => this.visibleIndices.length`
- `getRowData: (displayIndex) => { const i = this.visibleIndices[displayIndex]; const info = this.items[i]; ... }` (same body as today, just resolved through `i`)
- `checkboxCellRenderer`, `statusCellRenderer`, `selectCellRenderer`: each
  receives `displayIndex` from the table; each resolves
  `const i = this.visibleIndices[displayIndex];` first, then reads/writes
  `this.selected`, `this.rowStatus`, and `this.items[i]` exactly as before
  (just substituting `i` for the old `index` parameter).

### 4.4 Recomputation points

`_computeVisibleIndices()` is called (then the table is invalidated/rendered):

1. At the end of `populateTable()`, after `this.items`/`this.rowStatus`/
   `this.selected` are rebuilt from the backend response — before the final
   `treeInstance.invalidate()` / `tableHelper.render()` call.
2. At the end of the fixed-row-removal block inside `searchAndFix()` (after
   `survivingItems`/`survivingRowStatus`/`survivingSelected` replace
   `this.items`/`this.rowStatus`/`this.selected`), before
   `this.tableHelper?.render(...)`.
3. On every `input` event on the filter box (filter text changed), and on
   clear.

### 4.5 Select-all checkbox

```js
document.getElementById('select-all-cb')?.addEventListener('change', (e) => {
	if (e.target.checked) {
		for (const i of this.visibleIndices) this.selected.add(i);
	} else {
		this.selected.clear();
	}
	this.tableHelper?.treeInstance?.invalidate();
	this.updateActionButtons();
});
```

`_updateSelectAllCheckbox()`:

```js
_updateSelectAllCheckbox() {
	const cb = document.getElementById('select-all-cb');
	if (!cb) return;
	const total = this.visibleIndices.length;
	const count = this.visibleIndices.filter(i => this.selected.has(i)).length;
	cb.checked = total > 0 && count === total;
	cb.indeterminate = count > 0 && count < total;
},
```

### 4.6 UI markup (`fix-unavailable.xhtml`)

In `#table-toolbar`, after `#refresh-btn`:

```html
<div style="flex:1"></div>
<div id="filter-wrap">
	<input type="text" id="filename-filter" placeholder="Filter filename…"
	       title="Filter rows by attachment filename"/>
	<button id="filename-filter-clear" type="button" title="Clear filter"
	        aria-label="Clear filter" hidden="true">✕</button>
</div>
```

CSS additions:

- `#filter-wrap { position: relative; display: flex; align-items: center; }`
- `#filename-filter`: sized/styled to match the other toolbar controls
  (`toolbar-btn`'s font-size/height), fixed width (~180px), right-padding to
  leave room for the clear button.
- `#filename-filter-clear`: absolutely positioned inside the input's right
  edge, borderless, small, `[hidden]` by default (same `[hidden] { display:
  none }` pattern already used for `#fix-dropdown-menu`).

The leading `flex:1` spacer div pushes the filter box to the toolbar's right
edge, per the existing `display:flex` toolbar layout.

### 4.7 Filter input behavior (`init()`)

```js
const filterInput = document.getElementById('filename-filter');
const filterClearBtn = document.getElementById('filename-filter-clear');
filterInput?.addEventListener('input', () => {
	this.filterText = filterInput.value;
	filterClearBtn.hidden = !this.filterText;
	this._computeVisibleIndices();
	this.tableHelper?.treeInstance?.invalidate();
	this.updateActionButtons();
});
filterClearBtn?.addEventListener('click', () => {
	filterInput.value = '';
	filterInput.dispatchEvent(new Event('input'));
	filterInput.focus();
});
```

`updateActionButtons()` already calls `_updateDebugCheckboxVisibility()` and
`_updateSelectAllCheckbox()` on every call, so reusing it here keeps the
debug-checkbox-visibility and select-all-checkbox state consistent with the
new visible set without adding a separate code path.

`this.tableHelper?.treeInstance?.invalidate()` on row-count change already is
the established pattern in this file (see `populateTable()`'s final block),
so the filter reuses it rather than a full `tableHelper.render()`.

## 5. Out of scope

- Filtering/searching any column other than filename.
- Changing the status-bar "N unavailable attachments found" text to reflect
  the filtered count.
- Debouncing the filter input — the list sizes in this dialog are small
  enough that per-keystroke filtering is not a performance concern.
- A dedicated "no rows match filter" empty-state message (the table will
  simply render zero rows, same as the existing "no items" case already does
  implicitly).

## 6. Testing

- Node test (`plugin/test/`, if this dialog has existing JS tests — otherwise
  manual): verify `_computeVisibleIndices()` matching logic (empty filter →
  all indices; substring match is case-insensitive; no matches → empty array)
  and that `_filenameFor()` matches the existing inline logic it replaces.
- Manual verification in the live dev Zotero instance (per
  `CLAUDE.md`'s "Testing plugin JS directly against the live Zotero library"):
  - Type a filter matching a subset of filenames → only matching rows shown.
  - Select some, filter to hide them, confirm they remain selected
    (`updateActionButtons`'s enabled Delete/Search & Fix buttons reflect the
    unchanged `this.selected.size`), clear filter, confirm checkboxes are
    still checked.
  - With a filter active, click "Select all" → only visible rows get checked;
    uncheck → all rows (visible and hidden) are cleared.
  - Clear filter via the "✕" button → full list and prior full-list
    selection state (minus any explicit unselect-all) reappear.
  - Refresh while a filter is active → filter text and matching persist
    against the newly loaded list.
