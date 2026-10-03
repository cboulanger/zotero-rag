// @ts-check
// Dialog controller for "Fix Unavailable Attachments"

// Wire console so messages appear in Browser Console (same pattern as zotero-rag.js)
;(function() {
	/** @type {Record<string, number>} */
	const nsFlags = { warn: 0x1, error: 0x0 };
	const makeLogger = (/** @type {string} */ level) => (/** @type {any[]} */ ...args) => {
		const msg = "[Zotero RAG / fix-unavailable] " + args.join(" ");
		if (level === "log" || level === "info") {
			// @ts-ignore
			Services.console.logStringMessage(msg);
		} else {
			// @ts-ignore
			const e = Cc["@mozilla.org/scripterror;1"].createInstance(Ci.nsIScriptError);
			// @ts-ignore
			e.init(msg, "", null, 0, 0, nsFlags[level], "chrome javascript");
			// @ts-ignore
			Services.console.logMessage(e);
		}
	};
	// @ts-ignore
	if (typeof console === "undefined") {
		// @ts-ignore
		globalThis.console = { log: makeLogger("log"), info: makeLogger("info"), warn: makeLogger("warn"), error: makeLogger("error") };
	} else {
		["log", "info", "warn", "error"].forEach(level => { /** @type {any} */ (console)[level] = makeLogger(level); });
	}
})();

/**
 * @typedef {import('./zotero-rag.js').UnavailableAttachmentInfo} AttachmentInfo
 */

/**
 * @typedef {object} RowStatus
 * @property {string} cssClass
 * @property {string} text
 * @property {string} [tooltip]
 */

var ZoteroFixUnavailableDialog = {
	/** @type {any} */
	plugin: null,

	/** @type {number|null} */
	libraryID: null,

	/** @type {string} */
	backendLibraryId: '',

	/** @type {Array<AttachmentInfo>} */
	items: [],

	/** @type {boolean} */
	isRunning: false,

	/**
	 * Per-row status state, indexed by row index.
	 * @type {Map<number, RowStatus>}
	 */
	rowStatus: new Map(),

	/**
	 * VirtualizedTableHelper instance.
	 * @type {any}
	 */
	tableHelper: null,

	/**
	 * Set of checked row indices — independent of the VirtualizedTable native cursor selection.
	 * @type {Set<number>}
	 */
	selected: new Set(),

	/**
	 * Initialise the dialog. Called automatically after DOMContentLoaded loads this script.
	 * @returns {void}
	 */
	init() {
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (!window.arguments || !window.arguments[0]) {
			console.error("No arguments passed to fix-unavailable dialog");
			return;
		}
		// @ts-ignore
		const args = window.arguments[0];
		this.plugin = args.plugin;
		this.libraryID = args.libraryID;
		this.backendLibraryId = args.backendLibraryId || String(args.libraryID);

		document.getElementById('close-btn').addEventListener('click', () => {
			try {
				if (window.opener && this.plugin) this.plugin._scanUnavailableCount(window.opener);
			} catch (_) {}
			window.close();
		});
		document.getElementById('search-btn').addEventListener('click', () => this.searchAndFix());
		document.getElementById('delete-btn').addEventListener('click', () => this.deleteSelected());
		document.getElementById('refresh-btn').addEventListener('click', () => { if (!this.isRunning) this.populateTable(); });
		document.getElementById('select-all-cb')?.addEventListener('change', (/** @type {Event} */ e) => {
			if (/** @type {HTMLInputElement} */(e.target).checked) {
				for (let i = 0; i < this.items.length; i++) this.selected.add(i);
			} else {
				this.selected.clear();
			}
			this.tableHelper?.treeInstance?.invalidate();
			this.updateActionButtons();
		});

		this._initTable();
		this.populateTable();
	},

	/**
	 * Return a short "type" label for a row: why the attachment is
	 * unavailable/unreadable, or its file type when there's no failure reason.
	 * Priority matches searchAndFix()'s bucketing: skipReason and isParseError
	 * both mean "not indexable, no retry" and take priority for display, even
	 * though only one of them would ever be set on real data.
	 * @param {AttachmentInfo} info
	 * @returns {string}
	 */
	_typeLabelFor(info) {
		if (info.skipReason === 'no text') return 'empty';
		if (info.skipReason === 'timeout') return 'timeout';
		if (info.isParseError) return 'parse err';
		if (info.serverDownloadFailed) return 'srv fail';
		if (info.isLinked) return 'linked';
		return this.getFileTypeLabel(info.attachmentItem);
	},

	/**
	 * Build the VirtualizedTable and render it into #table-container.
	 * Uses getRowData for plain text columns and column.renderer for status/select.
	 * The table uses the native selection model (click / Ctrl+click / Shift+click / Ctrl+A).
	 * @returns {void}
	 */
	_initTable() {
		// @ts-ignore - ZoteroPluginToolkit is loaded by the xhtml before this script
		const { VirtualizedTableHelper } = ZoteroPluginToolkit;

		// column.renderer(index, data, column) is called by makeRowRenderer when set on a column.
		// column.className is auto-populated by VirtualizedTable to include the dataKey CSS class
		// (see zotero/chrome/content/zotero/components/virtualized-table.jsx line 1475), so
		// span.className = `cell ${column.className}` gives e.g. "cell status status_abc123".

		// Shared renderer for plain text columns: shows the full cell value as a
		// hover tooltip so content clipped by the column's fixed/flex width is still readable.
		const textCellRenderer = (/** @type {number} */ _index, /** @type {string} */ data, /** @type {any} */ column) => {
			const span = document.createElement('span');
			span.className = `cell ${column.className}`;
			span.textContent = data;
			span.title = data;
			return span;
		};

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
		const statusCellRenderer = (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
			const span = document.createElement('span');
			const status = this.rowStatus.get(index);
			span.className = `cell ${column.className}${status ? ' status-' + status.cssClass : ''}`;
			span.textContent = status ? status.text : '';
			if (status?.tooltip) span.title = status.tooltip;
			return span;
		};
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

		/** @type {Array<any>} */
		const columns = [
			{
				dataKey: 'checkbox',
				label: '',
				fixedWidth: true,
				width: 28,
				ignoreInColumnPicker: true,
				// Both names are set for cross-version compatibility: older Zotero
				// builds' virtualized-table dispatch on `column.renderer`, newer ones
				// (confirmed against a current Zotero checkout) renamed the hook to
				// `column.renderCell` — a column missing whichever name that build's
				// renderCell() looks for silently falls back to plain textContent,
				// which is empty for these three columns (their getRowData value is
				// '' on purpose), so the whole cell renders blank with no error.
				renderer: checkboxCellRenderer,
				renderCell: checkboxCellRenderer,
			},
			{ dataKey: 'author',   label: 'Author(s)', flex: 2,   renderer: textCellRenderer, renderCell: textCellRenderer },
			{ dataKey: 'year',     label: 'Year',      fixedWidth: true, width: 48, renderer: textCellRenderer, renderCell: textCellRenderer },
			{ dataKey: 'title',    label: 'Title',     flex: 3,   renderer: textCellRenderer, renderCell: textCellRenderer },
			{ dataKey: 'zoteroID', label: 'Zotero ID', fixedWidth: true, width: 84, renderer: textCellRenderer, renderCell: textCellRenderer },
			{ dataKey: 'filename', label: 'Filename',  flex: 2,   renderer: textCellRenderer, renderCell: textCellRenderer },
			{ dataKey: 'type',     label: 'Type',      fixedWidth: true, width: 70, renderer: textCellRenderer, renderCell: textCellRenderer },
			{
				dataKey: 'status',
				label: 'Status',
				flex: 2,
				renderer: statusCellRenderer,
				renderCell: statusCellRenderer,
			},
			{
				dataKey: 'select',
				label: '',
				fixedWidth: true,
				width: 28,
				ignoreInColumnPicker: true,
				renderer: selectCellRenderer,
				renderCell: selectCellRenderer,
			},
		];

		this.tableHelper = new VirtualizedTableHelper(window)
			.setContainerId('table-container')
			.setProp({
				id: 'fix-unavailable-table',
				columns,
				showHeader: true,
				multiSelect: false,
				staticColumns: true,
				disableFontSizeScaling: false,
				getRowCount: () => this.items.length,
				getRowData: (/** @type {number} */ index) => {
					const info = this.items[index];
					if (!info) return { author: '', year: '', title: '', zoteroID: '', filename: '', type: '', status: '', select: '' };
					const linkedPath = info.isLinked ? (info.attachmentItem.attachmentPath || '') : '';
					const filename = linkedPath || info.attachmentItem.attachmentFilename || '';
					return {
						author:   info.authors || '—',
						year:     info.year    || '—',
						title:    info.title   || '—',
						zoteroID: info.zoteroID,
						filename,
						type:   this._typeLabelFor(info),
						status: '', // rendered by column.renderer reading this.rowStatus
						select: '', // rendered by column.renderer
					};
				},
				onSelectionChange: () => {},
				// This table has no context menu, but some Zotero builds' virtualized-table
				// calls this.props.onItemContextMenu(...) unconditionally on right-click
				// rather than falling back to a default no-op — without this, right-clicking
				// any row throws "onItemContextMenu is not a function" in the Browser Console.
				onItemContextMenu: () => {},
			});

		this.tableHelper.render(undefined, () => {
			this.updateActionButtons();
		});
	},

	/**
	 * Load unavailable attachments from the plugin and render the table.
	 * @returns {Promise<void>}
	 */
	async populateTable() {
		const libraryHeader = document.getElementById('library-header');
		if (libraryHeader) {
			// @ts-ignore - Zotero is available globally
			const libraryName = Zotero.Libraries.get(this.libraryID)?.name || 'Library';
			libraryHeader.textContent = `Unavailable or unreadable attachments in ${libraryName}`;
		}
		this.setStatus('Loading unavailable attachments...');
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = true;

		try {
			this.items = await this.plugin._getUnavailableAttachments(this.libraryID);
		} catch (e) {
			this.setStatus(`Error loading items: ${e instanceof Error ? e.message : String(e)}`);
			return;
		}

		this.rowStatus.clear();

		// Sync the "N unavailable" link in the RAG dialog to the live count from this scan.
		if (this.plugin && typeof this.plugin.updateMissingFilesCount === 'function') {
			this.plugin.updateMissingFilesCount(this.backendLibraryId, this.items.length);
		}
		if (this.items.length === 0) {
			this.setStatus('No unavailable attachments found in this library.');
		} else {
			this.setStatus(`${this.items.length} unavailable attachment${this.items.length !== 1 ? 's' : ''} found.`);
		}

		// Pre-set status labels for non-fixable items so they're visible without running a search
		for (let i = 0; i < this.items.length; i++) {
			const item = this.items[i];
			if (item.isParseError) {
				this.rowStatus.set(i, { cssClass: 'not-found', text: 'binary data', tooltip: 'File is present but cannot be parsed (binary data detected)' });
			} else if (item.skipReason === 'no text') {
				this.rowStatus.set(i, { cssClass: 'not-found', text: 'no text', tooltip: 'No text could be extracted (scanned or protected PDF)' });
			} else if (item.skipReason === 'timeout') {
				this.rowStatus.set(i, { cssClass: 'not-found', text: 'timeout', tooltip: 'Text extraction timed out (file may be too large)' });
			}
		}

		// Pre-check all rows in our independent checkbox set
		this.selected.clear();
		for (let i = 0; i < this.items.length; i++) this.selected.add(i);

		if (this.tableHelper?.treeInstance) {
			this.tableHelper.treeInstance.invalidate();
			this.updateActionButtons();
		} else {
			// Table not yet rendered — re-render with new data
			this.tableHelper?.render(undefined, () => this.updateActionButtons());
		}
	},

	/**
	 * Enable or disable action buttons based on current selection.
	 * @returns {void}
	 */
	updateActionButtons() {
		this._updateDebugCheckboxVisibility();
		if (this.isRunning) return;
		const count = this.selected.size;
		const hasItems = this.items.length > 0;
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = count === 0 || !hasItems;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = count === 0 || !hasItems;
		this._updateSelectAllCheckbox();
	},

	/**
	 * Show the "Download debugging information" checkbox only while 1-10 rows are
	 * selected; disable (not hide) it while a repair run is in progress so the
	 * mode of the current run stays visible but cannot be flipped mid-run.
	 * @returns {void}
	 */
	_updateDebugCheckboxVisibility() {
		const label = document.getElementById('debug-download-label');
		const cb = /** @type {HTMLInputElement|null} */ (document.getElementById('debug-download-cb'));
		if (!label || !label.style) return;
		const n = this.selected.size;
		const max = typeof ZoteroFixDebug !== 'undefined' ? ZoteroFixDebug.MAX_ITEMS : 10;
		label.style.display = (n >= 1 && n <= max) ? '' : 'none';
		if (cb) cb.disabled = this.isRunning;
	},

	/**
	 * Whether debug collection applies to a run over `indices`: the box must be
	 * checked and the selection within range, regardless of display state.
	 * @param {number[]} indices
	 * @returns {boolean}
	 */
	_shouldCollectDebug(indices) {
		if (typeof ZoteroFixDebug === 'undefined') return false;
		const cb = /** @type {HTMLInputElement|null} */ (document.getElementById('debug-download-cb'));
		return !!cb?.checked && indices.length >= 1 && indices.length <= ZoteroFixDebug.MAX_ITEMS;
	},

	/**
	 * Describe the attachment file for the debug report: existence, size,
	 * basename only (never the absolute path). Never throws.
	 * @param {AttachmentInfo} info
	 * @returns {Promise<{exists_locally: boolean|null, size_bytes: number|null, basename: string|null, is_linked: boolean}>}
	 */
	async _describeFile(info) {
		/** @type {{exists_locally: boolean|null, size_bytes: number|null, basename: string|null, is_linked: boolean}} */
		const out = { exists_locally: null, size_bytes: null, basename: null, is_linked: !!info.isLinked };
		try {
			const att = info.attachmentItem;
			out.basename = att.attachmentFilename || null;
			out.exists_locally = await att.fileExists();
			if (out.exists_locally) {
				const filePath = await att.getFilePathAsync();
				// @ts-ignore - IOUtils is a global in Firefox/Zotero
				if (filePath) out.size_bytes = (await IOUtils.stat(filePath)).size ?? null;
			}
		} catch (_) {}
		return out;
	},

	/**
	 * Environment facts for the report's header and path redaction.
	 * @returns {{plugin: any, backend: any, library: any, pathPrefixes: Array<{path: string, label: string}>}}
	 */
	_debugEnvironment() {
		const plugin = this.plugin || {};
		/** @type {Array<{path: string, label: string}>} */
		const pathPrefixes = [];
		/** @type {string|null} */
		let urlHost = null;
		try { urlHost = new URL(plugin.backendURL || '').host; } catch (_) {}
		/** @type {any} */
		let library = { backend_library_id: this.backendLibraryId, zotero_library_id: this.libraryID };
		try {
			// @ts-ignore
			library.library_type = Zotero.Libraries.get(this.libraryID)?.libraryType || null;
			// @ts-ignore
			pathPrefixes.push({ path: Zotero.DataDirectory.dir, label: '<zotero-data>' });
			// @ts-ignore
			pathPrefixes.push({ path: Services.dirsvc.get('Home', Ci.nsIFile).path, label: '~' });
		} catch (_) {}
		return {
			plugin: {
				version: plugin.version || null,
				// @ts-ignore
				zoteroVersion: typeof Zotero !== 'undefined' ? Zotero.version : null,
				// @ts-ignore
				platform: typeof Zotero !== 'undefined' ? Zotero.platform : null,
			},
			backend: { urlHost, isLocal: typeof plugin.isLocalBackend === 'function' ? plugin.isLocalBackend() : false },
			library,
			pathPrefixes,
		};
	},

	/**
	 * Sync the select-all checkbox in the toolbar to reflect the current checkbox state.
	 * @returns {void}
	 */
	_updateSelectAllCheckbox() {
		const cb = /** @type {HTMLInputElement|null} */ (document.getElementById('select-all-cb'));
		if (!cb) return;
		const count = this.selected.size;
		const total = this.items.length;
		cb.checked = total > 0 && count === total;
		cb.indeterminate = count > 0 && count < total;
	},

	/**
	 * Return the currently selected row indices.
	 * @returns {number[]}
	 */
	getSelectedIndices() {
		return [...this.selected];
	},

	/**
	 * Focus the main Zotero window and select the attachment item in the item list.
	 * @param {AttachmentInfo} info
	 * @returns {void}
	 */
	selectItemInZotero(info) {
		try {
			const opener = window.opener;
			if (!opener) return;
			opener.focus();
			const pane = opener.Zotero && opener.Zotero.getActiveZoteroPane
				? opener.Zotero.getActiveZoteroPane()
				: null;
			if (!pane) return;
			pane.selectItem(info.attachmentItem.id);
		} catch (e) {
			console.error('selectItemInZotero failed:', e);
		}
	},

	/**
	 * Permanently delete the parent items of all selected rows after user confirmation.
	 * @returns {Promise<void>}
	 */
	async deleteSelected() {
		if (this.isRunning) return;
		const indices = this.getSelectedIndices();
		if (indices.length === 0) return;

		const confirmed = window.confirm(
			`Do you really want to permanently delete ${indices.length} item${indices.length !== 1 ? 's' : ''}? This cannot be undone.`
		);
		if (!confirmed) return;

		this.isRunning = true;
		this._setAllButtonsDisabled(true);
		this.setStatus(`Deleting ${indices.length} item${indices.length !== 1 ? 's' : ''}...`);

		try {
			const parentIDs = [...new Set(
				indices
					.map(i => this.items[i]?.parentItem?.id)
					.filter(/** @type {(id: any) => id is number} */ (id) => typeof id === 'number')
			)];
			// @ts-ignore - Zotero.Items.erase exists at runtime
			await Zotero.Items.erase(parentIDs);
			this.setStatus(`Deleted ${parentIDs.length} item${parentIDs.length !== 1 ? 's' : ''}.`);
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			this.setStatus(`Delete failed: ${msg}`);
			console.error('deleteSelected failed:', msg);
		}

		this.isRunning = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('close-btn')).disabled = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('refresh-btn')).disabled = false;
		await this.populateTable();
		try {
			if (window.opener && this.plugin) this.plugin._scanUnavailableCount(window.opener);
		} catch (_) {}
	},

	/**
	 * Run the search-and-fix operation on all selected rows.
	 * Phase 1 (parallel): try Zotero sync download for every selected item.
	 * Phase 2 (sequential): for items still unavailable, search other libraries by filename/MD5 and copy.
	 * @returns {Promise<void>}
	 */
	async searchAndFix() {
		if (this.isRunning) return;
		this.isRunning = true;
		this._setAllButtonsDisabled(true);

		const indices = this.getSelectedIndices();
		const parseErrorIndices  = indices.filter(i => this.items[i].isParseError);
		const timeoutIndices     = indices.filter(i => this.items[i].skipReason === 'timeout');
		const emptyTextIndices   = indices.filter(i => this.items[i].skipReason === 'no text');
		const linkedIndices      = indices.filter(i => !this.items[i].isParseError && !this.items[i].skipReason && this.items[i].isLinked);
		const importedIndices    = indices.filter(i => !this.items[i].isParseError && !this.items[i].skipReason && !this.items[i].isLinked);

		// Optional debug collection (observational only: never alters repair behaviour).
		const collectDebug = this._shouldCollectDebug(indices);
		/** @type {any} */
		let report = null;
		/** @type {Map<number, any>} */
		const itemHandles = new Map();
		const NO_DIAG_NOTE = 'server did not return diagnostics (backend may predate this feature)';
		if (collectDebug) {
			const env = this._debugEnvironment();
			report = ZoteroFixDebug.createReport({
				plugin: env.plugin, backend: env.backend, library: env.library, pathPrefixes: env.pathPrefixes,
				selectionCount: indices.length, totalRows: this.items.length,
			});
			for (const i of indices) {
				const info = this.items[i];
				const handle = report.startItem(info, { typeLabel: this._typeLabelFor(info), file: await this._describeFile(info) });
				itemHandles.set(i, handle);
			}
			for (const i of parseErrorIndices) itemHandles.get(i).skip('skipped_parse_error', 'file present but cannot be parsed (binary data)');
			for (const i of linkedIndices)     itemHandles.get(i).skip('skipped_linked_file', 'linked file — cannot be auto-downloaded');
		}

		for (const i of parseErrorIndices)  this.setRowStatus(i, 'not-found', 'Binary data — delete and replace');
		for (const i of linkedIndices)      this.setRowStatus(i, 'not-found', 'Linked file — fix path in Zotero');
		for (const i of importedIndices)    this.setRowStatus(i, 'searching', 'Queued...');
		for (const i of timeoutIndices)     this.setRowStatus(i, 'searching', 'Retrying with longer timeout...');
		for (const i of emptyTextIndices)   this.setRowStatus(i, 'searching', 'Re-checking (file may have changed)...');

		// Phase 0: retry skipReason='timeout' rows server-side with a doubled
		// extraction timeout. A 'timeout' row's file downloaded fine; only
		// Kreuzberg's parsing pass ran out of time, so a longer timeout can
		// plausibly succeed. Sequential (not batched like Phase 1) since these
		// are exactly the largest/slowest files — running several OCR-heavy
		// extractions concurrently risks the Kreuzberg sidecar's own memory
		// limits.
		/** @type {Array<number>} */
		const timeoutFixedIndices = [];
		let timeoutStillFailed = 0;
		if (timeoutIndices.length > 0) {
			this.setStatus(`Retrying ${timeoutIndices.length} timed-out file(s) with a longer timeout...`);
			for (const i of timeoutIndices) {
				const info = this.items[i];
				const step = itemHandles.get(i)?.addStep('timeout_retry');
				try {
					const result = await (collectDebug
						? this.plugin.retryTimeoutSkippedAttachment(info.attachmentItem, info.parentItem, this.libraryID, { includeDiagnostics: true })
						: this.plugin.retryTimeoutSkippedAttachment(info.attachmentItem, info.parentItem, this.libraryID));
					step?.finish(
						result.fixed ? 'fixed' : result.stillTimedOut ? 'still_timed_out' : 'error',
						{ ...(result.pluginDiag || {}), ...(result.error ? { error: result.error } : {}) },
						result.backendDiag ?? null, result.backendDiag ? null : NO_DIAG_NOTE
					);
					if (result.fixed) {
						this.setRowStatus(i, 'fixed', 'Fixed (longer timeout)');
						timeoutFixedIndices.push(i);
					} else if (result.stillTimedOut) {
						this.setRowStatus(i, 'not-found', 'Still times out — delete or raise the limit further');
						timeoutStillFailed++;
					} else {
						this.setRowStatus(i, 'error', `Retry failed: ${result.error}`, result.error);
						timeoutStillFailed++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(i, 'error', `Error: ${msg}`, msg);
					step?.finish('error', { error: msg });
					itemHandles.get(i)?.addError('plugin', e);
					timeoutStillFailed++;
					console.error(`fix-unavailable: timeout retry error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}
		if (timeoutFixedIndices.length > 0 && this.plugin?.removeSkippedServerItems) {
			try {
				await this.plugin.removeSkippedServerItems(
					this.backendLibraryId, timeoutFixedIndices.map(i => this.items[i].attachmentItem.key)
				);
			} catch (e) {
				console.error(`fix-unavailable: failed to prune fixed skipped-server entries: ${e}`);
			}
		}

		// Phase 0b: retry skipReason='no text' rows with a plain re-upload. A
		// "no text" skip reflects the attachment's content *at the time it was
		// last indexed* (e.g. a scanned PDF with no text layer) — if the user
		// has since OCR'd the same file in place (outside Zotero, e.g. via
		// ScanTailor), Zotero's own item.version often doesn't change, since a
		// manual file replace isn't a Zotero-tracked edit. The normal indexing
		// run's version cache would therefore never notice and keep skipping
		// it forever. Bypass that cache here by re-uploading directly, same as
		// the timeout retry above.
		/** @type {Array<number>} */
		const emptyTextFixedIndices = [];
		let emptyTextStillFailed = 0;
		if (emptyTextIndices.length > 0) {
			this.setStatus(`Re-checking ${emptyTextIndices.length} previously empty file(s)...`);
			for (const i of emptyTextIndices) {
				const info = this.items[i];
				const step = itemHandles.get(i)?.addStep('empty_text_retry');
				try {
					const result = await (collectDebug
						? this.plugin.retryEmptyTextSkippedAttachment(info.attachmentItem, info.parentItem, this.libraryID, { includeDiagnostics: true })
						: this.plugin.retryEmptyTextSkippedAttachment(info.attachmentItem, info.parentItem, this.libraryID));
					step?.finish(
						result.fixed ? 'fixed' : result.stillEmpty ? 'still_empty' : 'error',
						{ ...(result.pluginDiag || {}), ...(result.error ? { error: result.error } : {}) },
						result.backendDiag ?? null, result.backendDiag ? null : NO_DIAG_NOTE
					);
					if (result.fixed) {
						this.setRowStatus(i, 'fixed', 'Fixed (re-extracted)');
						emptyTextFixedIndices.push(i);
					} else if (result.stillEmpty) {
						this.setRowStatus(i, 'not-found', 'Not indexable — delete or replace file');
						emptyTextStillFailed++;
					} else {
						this.setRowStatus(i, 'error', `Retry failed: ${result.error}`, result.error);
						emptyTextStillFailed++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(i, 'error', `Error: ${msg}`, msg);
					step?.finish('error', { error: msg });
					itemHandles.get(i)?.addError('plugin', e);
					emptyTextStillFailed++;
					console.error(`fix-unavailable: empty-text retry error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}
		if (emptyTextFixedIndices.length > 0 && this.plugin?.removeSkippedServerItems) {
			try {
				await this.plugin.removeSkippedServerItems(
					this.backendLibraryId, emptyTextFixedIndices.map(i => this.items[i].attachmentItem.key)
				);
			} catch (e) {
				console.error(`fix-unavailable: failed to prune fixed skipped-server entries: ${e}`);
			}
		}

		// Phase 1: batched sync downloads for imported files only (10 at a time)
		const BATCH_SIZE = 10;
		/** @type {Array<{index: number, downloaded: boolean, reason?: string}>} */
		const downloadResults = [];

		for (let batchStart = 0; batchStart < importedIndices.length; batchStart += BATCH_SIZE) {
			const batch = importedIndices.slice(batchStart, batchStart + BATCH_SIZE);
			const batchEnd = Math.min(batchStart + BATCH_SIZE, importedIndices.length);
			this.setStatus(`Phase 1/2: downloading file(s) via Zotero sync (${batchEnd}/${importedIndices.length})...`);

			for (const i of batch) this.setRowStatus(i, 'searching', 'Downloading...');

			const batchOutcomes = await Promise.allSettled(
				batch.map(i =>
					this.plugin._tryDownloadAttachment(this.items[i].attachmentItem)
						.then(/** @type {(r: any) => any} */ r => ({ index: i, ...r }))
				)
			);

			for (const outcome of batchOutcomes) {
				if (outcome.status === 'rejected') {
					const index = /** @type {any} */ (outcome).index;
					this.setRowStatus(index, 'error', 'Download error');
					downloadResults.push({ index, downloaded: false, reason: 'rejected' });
					const rejection = /** @type {any} */ (outcome).reason;
					itemHandles.get(index)?.addStep('sync_download').finish('error', { reason: 'rejected', error: String(rejection) });
					if (rejection) itemHandles.get(index)?.addError('plugin', rejection);
				} else {
					const { index, downloaded, reason } = outcome.value;
					downloadResults.push({ index, downloaded, reason });
					itemHandles.get(index)?.addStep('sync_download').finish(
						downloaded ? 'fixed' : 'not_found',
						{ reason: reason ?? null, sync_enabled: reason !== 'sync-disabled' }
					);
					if (downloaded) {
						this.setRowStatus(index, 'fixed', 'Downloaded');
					} else {
						this.setRowStatus(index, 'searching',
							reason === 'sync-disabled' ? 'Sync off — searching...' : 'Searching...'
						);
					}
				}
			}
		}

		/** @type {Array<number>} */
		const stillMissing = downloadResults
			.filter(r => !r.downloaded && r.reason !== 'rejected')
			.map(r => r.index);

		// Phase 2: copy from another library for imported items still missing
		let fixed    = downloadResults.filter(r => r.downloaded).length + timeoutFixedIndices.length + emptyTextFixedIndices.length;
		let notFound = linkedIndices.length + parseErrorIndices.length + timeoutStillFailed + emptyTextStillFailed;
		let errors   = 0;

		/** @type {Array<number>} */
		const phase2FixedIndices = [];

		if (stillMissing.length > 0) {
			this.setStatus(`Phase 2/2: searching other libraries for ${stillMissing.length} remaining file(s)...`);
			for (const i of stillMissing) {
				const info = this.items[i];
				const step = itemHandles.get(i)?.addStep('other_library_search');
				try {
					const result = await (collectDebug
						? this.plugin._searchAndFixUnavailableAttachment(info.attachmentItem, (/** @type {string} */ n, /** @type {any} */ d) => step?.note(n, d))
						: this.plugin._searchAndFixUnavailableAttachment(info.attachmentItem));
					step?.finish(
						result.found && !result.error ? 'fixed' : result.found ? 'copy_failed' : 'not_found',
						{ via: result.via ?? null, ...(result.error ? { error: result.error } : {}) }
					);
					if (result.found && !result.error) {
						this.setRowStatus(i, 'fixed', `Fixed (${result.via})`);
						fixed++;
						phase2FixedIndices.push(i);
					} else if (result.found && result.error) {
						this.setRowStatus(i, 'error', `Copy failed: ${result.error}`, result.error);
						errors++;
					} else {
						// This is the terminal state for a server-reported download
						// failure once both Zotero sync and the other-library search
						// strategies have been tried — nothing left for the plugin to
						// attempt automatically. Most commonly the file has been
						// permanently removed from Zotero's cloud storage (e.g. a
						// storage-quota 404), which only the user can resolve.
						this.setRowStatus(
							i, 'not-found', 'Not found — re-upload required',
							'Could not be downloaded or located elsewhere. The file may no '
							+ 'longer be on Zotero’s servers (e.g. a storage-quota issue) '
							+ '— re-upload it to this item in Zotero, or check your storage '
							+ 'quota, then try again.'
						);
						notFound++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(i, 'error', `Error: ${msg}`, msg);
					step?.finish('error', { error: msg });
					itemHandles.get(i)?.addError('plugin', e);
					errors++;
					console.error(`fix-unavailable: error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}

		// Rows sourced from the server's download-failed report (see
		// UnavailableAttachmentInfo.serverDownloadFailed) are loaded from a
		// persistent per-library store, not derived live like the local
		// fileExists() check — a successful fix here doesn't make them
		// disappear from that store on its own, so without this they'd keep
		// reappearing (with their status reset) on every Refresh/reopen even
		// though the attachment is now available. Prune just the ones that
		// were actually fixed this run.
		const allFixedIndices = [
			...downloadResults.filter(r => r.downloaded).map(r => r.index),
			...phase2FixedIndices,
			...timeoutFixedIndices,
			...emptyTextFixedIndices,
		];
		const fixedDownloadFailedKeys = allFixedIndices
			.filter(i => this.items[i].serverDownloadFailed)
			.map(i => this.items[i].attachmentItem.key);
		if (fixedDownloadFailedKeys.length > 0 && this.plugin?.removeDownloadFailedItems) {
			try {
				await this.plugin.removeDownloadFailedItems(this.backendLibraryId, fixedDownloadFailedKeys);
			} catch (e) {
				console.error(`fix-unavailable: failed to prune fixed download-failed entries: ${e}`);
			}
		}

		// Record each selected row's final status for the debug report (before the
		// fixed rows are filtered out and indices shift).
		for (const [i, handle] of itemHandles) {
			const st = this.rowStatus.get(i);
			if (st) handle.setFinalStatus(st.cssClass, st.text);
		}

		// Drop fixed rows from the table immediately rather than waiting for a
		// manual Refresh — and do it by filtering this.items in place instead
		// of re-running populateTable(), which would also wipe rowStatus for
		// every row (including the 'error'/'not-found' status we just set on
		// rows that are NOT fixed) since that status only ever lives in memory
		// for the current session, never persisted.
		if (allFixedIndices.length > 0) {
			const fixedSet = new Set(allFixedIndices);
			/** @type {Array<AttachmentInfo>} */
			const survivingItems = [];
			/** @type {Map<number, RowStatus>} */
			const survivingRowStatus = new Map();
			/** @type {Set<number>} */
			const survivingSelected = new Set();
			for (let i = 0; i < this.items.length; i++) {
				if (fixedSet.has(i)) continue;
				const newIndex = survivingItems.length;
				survivingItems.push(this.items[i]);
				if (this.rowStatus.has(i)) survivingRowStatus.set(newIndex, this.rowStatus.get(i));
				if (this.selected.has(i)) survivingSelected.add(newIndex);
			}
			this.items = survivingItems;
			this.rowStatus = survivingRowStatus;
			this.selected = survivingSelected;
			if (this.plugin && typeof this.plugin.updateMissingFilesCount === 'function') {
				this.plugin.updateMissingFilesCount(this.backendLibraryId, this.items.length);
			}
			this.tableHelper?.render(undefined, () => this.updateActionButtons());
		}

		const parts = [];
		if (fixed    > 0) parts.push(`${fixed} fixed`);
		if (notFound > 0) parts.push(`${notFound} not found`);
		if (errors   > 0) parts.push(`${errors} error${errors !== 1 ? 's' : ''}`);
		const doneText = `Done. ${parts.join(', ')}.`;
		this.setStatus(doneText);

		if (report) {
			const result = await this._saveDebugReport(report, { fixed, not_found: notFound, errors });
			this.setStatus(`${doneText} ${result}`);
		}

		this.isRunning = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('close-btn')).disabled = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('refresh-btn')).disabled = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = false;
		this.updateActionButtons();

		try {
			if (window.opener && this.plugin) this.plugin._scanUnavailableCount(window.opener);
		} catch (_) {}
	},

	/**
	 * Finalize the debug report and save it via the native save dialog. Never
	 * throws; returns a short status sentence for the status bar.
	 * @param {any} report
	 * @param {{fixed: number, not_found: number, errors: number}} summary
	 * @returns {Promise<string>}
	 */
	async _saveDebugReport(report, summary) {
		try {
			const data = report.finalize(summary);
			this.setStatus('Choose where to save the debug file...');
			const saved = await ZoteroFixDebug.save(window, data, ZoteroFixDebug.fileName(this.backendLibraryId));
			return saved ? `Debug info saved to ${saved}.` : 'Debug info not saved.';
		} catch (e) {
			return `Failed to save debug info: ${(/** @type {any} */ (e))?.message ?? String(e)}`;
		}
	},

	/**
	 * Update a row's status and trigger a repaint of that row.
	 * @param {number} index
	 * @param {string} cssClass - one of: searching, fixed, not-found, error
	 * @param {string} text
	 * @param {string} [tooltip]
	 * @returns {void}
	 */
	setRowStatus(index, cssClass, text, tooltip = '') {
		this.rowStatus.set(index, { cssClass, text, tooltip });
		try {
			this.tableHelper?.treeInstance?.invalidateRow(index);
		} catch (_) {}
	},

	/**
	 * Return a short label for the attachment file type.
	 * @param {any} attachmentItem
	 * @returns {string}
	 */
	getFileTypeLabel(attachmentItem) {
		const mime = attachmentItem.attachmentContentType || '';
		if (mime === 'application/pdf')      return 'PDF';
		if (mime === 'text/html')            return 'HTML';
		if (mime === 'application/epub+zip') return 'EPUB';
		if (mime.startsWith('image/'))       return mime.slice(6).toUpperCase();
		const filename = attachmentItem.attachmentFilename || '';
		const ext = filename.includes('.') ? filename.split('.').pop().toUpperCase() : '';
		return ext || mime.split('/').pop() || '?';
	},

	/**
	 * Disable or enable all action/navigation buttons at once.
	 * @param {boolean} disabled
	 * @returns {void}
	 */
	_setAllButtonsDisabled(disabled) {
		for (const id of ['search-btn', 'delete-btn', 'close-btn', 'refresh-btn']) {
			/** @type {HTMLButtonElement} */ (document.getElementById(id)).disabled = disabled;
		}
		const debugCb = /** @type {HTMLInputElement|null} */ (document.getElementById('debug-download-cb'));
		if (debugCb) debugCb.disabled = disabled;
	},

	/**
	 * Set the status bar text.
	 * @param {string} text
	 * @returns {void}
	 */
	setStatus(text) {
		const bar = document.getElementById('status-bar');
		if (bar) bar.textContent = text;
	}
};

// Auto-init after load
ZoteroFixUnavailableDialog.init();
