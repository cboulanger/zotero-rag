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

		/** @type {Array<any>} */
		const columns = [
			{
				dataKey: 'checkbox',
				label: '',
				fixedWidth: true,
				width: 28,
				ignoreInColumnPicker: true,
				renderer: (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
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
				},
			},
			{ dataKey: 'author',   label: 'Author(s)', flex: 2 },
			{ dataKey: 'year',     label: 'Year',      fixedWidth: true, width: 48 },
			{ dataKey: 'title',    label: 'Title',     flex: 3 },
			{ dataKey: 'zoteroID', label: 'Zotero ID', fixedWidth: true, width: 84 },
			{ dataKey: 'filename', label: 'Filename',  flex: 2 },
			{ dataKey: 'type',     label: 'Type',      fixedWidth: true, width: 50 },
			{
				dataKey: 'status',
				label: 'Status',
				flex: 2,
				renderer: (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
					const span = document.createElement('span');
					const status = this.rowStatus.get(index);
					span.className = `cell ${column.className}${status ? ' status-' + status.cssClass : ''}`;
					span.textContent = status ? status.text : '';
					if (status?.tooltip) span.title = status.tooltip;
					return span;
				},
			},
			{
				dataKey: 'select',
				label: '',
				fixedWidth: true,
				width: 28,
				ignoreInColumnPicker: true,
				renderer: (/** @type {number} */ index, /** @type {string} */ _data, /** @type {any} */ column) => {
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
				},
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
						type:   info.isParseError ? 'parse err' : (info.isLinked ? 'linked' : this.getFileTypeLabel(info.attachmentItem)),
						status: '', // rendered by column.renderer reading this.rowStatus
						select: '', // rendered by column.renderer
					};
				},
				onSelectionChange: () => {},
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

		if (this.items.length === 0) {
			if (this.plugin && typeof this.plugin.clearMissingFilesCount === 'function') {
				this.plugin.clearMissingFilesCount(this.backendLibraryId);
			}
			this.setStatus('No unavailable attachments found in this library.');
		} else {
			this.setStatus(`${this.items.length} unavailable attachment${this.items.length !== 1 ? 's' : ''} found.`);
		}

		// Pre-set status label for parse-error items so it's visible without running a search
		for (let i = 0; i < this.items.length; i++) {
			if (this.items[i].isParseError) {
				this.rowStatus.set(i, { cssClass: 'not-found', text: 'binary data', tooltip: 'File is present but cannot be parsed (binary data detected)' });
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
		if (this.isRunning) return;
		const count = this.selected.size;
		const hasItems = this.items.length > 0;
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = count === 0 || !hasItems;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = count === 0 || !hasItems;
		this._updateSelectAllCheckbox();
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
		const parseErrorIndices = indices.filter(i => this.items[i].isParseError);
		const linkedIndices     = indices.filter(i => !this.items[i].isParseError && this.items[i].isLinked);
		const importedIndices   = indices.filter(i => !this.items[i].isParseError && !this.items[i].isLinked);

		for (const i of parseErrorIndices) this.setRowStatus(i, 'not-found', 'Binary data — delete and replace');
		for (const i of linkedIndices)     this.setRowStatus(i, 'not-found', 'Linked file — fix path in Zotero');
		for (const i of importedIndices)   this.setRowStatus(i, 'searching', 'Queued...');

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
				} else {
					const { index, downloaded, reason } = outcome.value;
					downloadResults.push({ index, downloaded, reason });
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
		let fixed    = downloadResults.filter(r => r.downloaded).length;
		let notFound = linkedIndices.length + parseErrorIndices.length;
		let errors   = 0;

		if (stillMissing.length > 0) {
			this.setStatus(`Phase 2/2: searching other libraries for ${stillMissing.length} remaining file(s)...`);
			for (const i of stillMissing) {
				const info = this.items[i];
				try {
					const result = await this.plugin._searchAndFixUnavailableAttachment(info.attachmentItem);
					if (result.found && !result.error) {
						this.setRowStatus(i, 'fixed', `Fixed (${result.via})`);
						fixed++;
					} else if (result.found && result.error) {
						this.setRowStatus(i, 'error', `Copy failed: ${result.error}`, result.error);
						errors++;
					} else {
						this.setRowStatus(i, 'not-found', 'Not found');
						notFound++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(i, 'error', `Error: ${msg}`, msg);
					errors++;
					console.error(`fix-unavailable: error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}

		const parts = [];
		if (fixed    > 0) parts.push(`${fixed} fixed`);
		if (notFound > 0) parts.push(`${notFound} not found`);
		if (errors   > 0) parts.push(`${errors} error${errors !== 1 ? 's' : ''}`);
		this.setStatus(`Done. ${parts.join(', ')}.`);

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
