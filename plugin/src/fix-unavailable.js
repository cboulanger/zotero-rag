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

var ZoteroFixUnavailableDialog = {
	/** @type {any} */
	plugin: null,

	/** @type {number|null} */
	libraryID: null,

	/** @type {Array<AttachmentInfo>} */
	items: [],

	/** @type {boolean} */
	isRunning: false,

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

		document.getElementById('close-btn').addEventListener('click', () => window.close());
		document.getElementById('search-btn').addEventListener('click', () => this.searchAndFix());
		document.getElementById('delete-btn').addEventListener('click', () => this.deleteSelected());
		document.getElementById('refresh-btn').addEventListener('click', () => { if (!this.isRunning) this.populateTable(); });
		document.getElementById('header-checkbox').addEventListener('change', (e) => {
			this.setAllChecked(/** @type {HTMLInputElement} */ (e.target).checked);
		});

		this.populateTable();
	},

	/**
	 * Load unavailable attachments from the plugin and render the table.
	 * @returns {Promise<void>}
	 */
	async populateTable() {
		this.setStatus('Loading unavailable attachments...');
		const searchBtn = /** @type {HTMLButtonElement} */ (document.getElementById('search-btn'));
		searchBtn.disabled = true;

		try {
			this.items = await this.plugin._getUnavailableAttachments(this.libraryID);
		} catch (e) {
			this.setStatus(`Error loading items: ${e instanceof Error ? e.message : String(e)}`);
			return;
		}

		const tbody = document.getElementById('items-tbody');
		tbody.innerHTML = '';

		if (this.items.length === 0) {
			const row = document.createElement('div');
			row.className = 'table-row';
			const cell = document.createElement('div');
			cell.style.flex = '1';
			cell.style.textAlign = 'center';
			cell.style.padding = '20px';
			cell.style.color = '#666';
			cell.textContent = 'No unavailable attachments found in this library.';
			row.appendChild(cell);
			tbody.appendChild(row);
			this.setStatus('');
			return;
		}

		for (let i = 0; i < this.items.length; i++) {
			const info = this.items[i];
			const row = document.createElement('div');
			row.className = 'table-row';
			row.dataset.index = String(i);

			// Checkbox
			const tdCheck = document.createElement('div');
			tdCheck.className = 'col-check';
			const cb = document.createElement('input');
			cb.type = 'checkbox';
			cb.checked = true;
			cb.dataset.index = String(i);
			cb.addEventListener('change', () => this.updateSearchButton());
			tdCheck.appendChild(cb);
			row.appendChild(tdCheck);

			// Author(s)
			const tdAuth = document.createElement('div');
			tdAuth.className = 'col-author';
			tdAuth.textContent = info.authors || '—';
			tdAuth.title = info.authors || '';
			row.appendChild(tdAuth);

			// Year
			const tdYear = document.createElement('div');
			tdYear.className = 'col-year';
			tdYear.textContent = info.year || '—';
			row.appendChild(tdYear);

			// Title
			const tdTitle = document.createElement('div');
			tdTitle.className = 'col-title';
			tdTitle.textContent = info.title || '—';
			tdTitle.title = info.title || '';
			row.appendChild(tdTitle);

			// Zotero ID
			const tdKey = document.createElement('div');
			tdKey.className = 'col-key';
			tdKey.textContent = info.zoteroID;
			row.appendChild(tdKey);

			// Filename
			const tdFile = document.createElement('div');
			tdFile.className = 'col-filename';
			const filename = info.attachmentItem.attachmentFilename || '';
			tdFile.textContent = filename;
			tdFile.title = filename;
			row.appendChild(tdFile);

			// File type
			const tdType = document.createElement('div');
			tdType.className = 'col-type';
			tdType.textContent = this.getFileTypeLabel(info.attachmentItem);
			row.appendChild(tdType);

			// Status
			const tdStatus = document.createElement('div');
			tdStatus.className = 'status-cell col-status';
			tdStatus.id = `status-${i}`;
			tdStatus.textContent = '';
			row.appendChild(tdStatus);

			// Select-in-Zotero button
			const tdSelect = document.createElement('div');
			tdSelect.className = 'col-select';
			const selectBtn = document.createElement('button');
			selectBtn.className = 'select-btn';
			selectBtn.textContent = '🔍';
			selectBtn.title = 'Select in Zotero';
			selectBtn.addEventListener('click', () => this.selectItemInZotero(info));
			tdSelect.appendChild(selectBtn);
			row.appendChild(tdSelect);

			tbody.appendChild(row);
		}

		// Sync header checkbox with row checkboxes (all checked by default)
		/** @type {HTMLInputElement} */ (document.getElementById('header-checkbox')).checked = this.items.length > 0;
		this.updateSearchButton();
		this.setStatus(`${this.items.length} unavailable attachment${this.items.length !== 1 ? 's' : ''} found.`);
	},

	/**
	 * Check or uncheck all row checkboxes.
	 * @param {boolean} checked
	 * @returns {void}
	 */
	setAllChecked(checked) {
		document.querySelectorAll('#items-tbody input[type="checkbox"]').forEach(cb => {
			/** @type {HTMLInputElement} */ (cb).checked = checked;
		});
		/** @type {HTMLInputElement} */ (document.getElementById('header-checkbox')).checked = checked;
		this.updateSearchButton();
	},



	/**
	 * Enable or disable the Search button based on whether any rows are selected.
	 * @returns {void}
	 */
	updateSearchButton() {
		if (this.isRunning) return;
		const anyChecked = Array.from(document.querySelectorAll('#items-tbody input[type="checkbox"]'))
			.some(cb => /** @type {HTMLInputElement} */ (cb).checked);
		const hasItems = this.items.length > 0;
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = !anyChecked || !hasItems;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = !anyChecked || !hasItems;
	},

	/**
	 * Focus the main Zotero window and select the attachment item (and its parent) in the item list.
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
			// Select the attachment item; Zotero will reveal it under its parent
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
		const checkboxes = /** @type {NodeListOf<HTMLInputElement>} */ (
			document.querySelectorAll('#items-tbody input[type="checkbox"]')
		);
		const selected = Array.from(checkboxes)
			.map((cb, i) => ({ cb, index: i }))
			.filter(({ cb }) => cb.checked);
		if (selected.length === 0) return;

		const confirmed = window.confirm(
			`Do you really want to permanently delete ${selected.length} item${selected.length !== 1 ? 's' : ''}? This cannot be undone.`
		);
		if (!confirmed) return;

		this.isRunning = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('close-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('refresh-btn')).disabled = true;
		this.setStatus(`Deleting ${selected.length} item${selected.length !== 1 ? 's' : ''}...`);

		try {
			// Collect unique parent item IDs (delete parent, not just the attachment)
			const parentIDs = [...new Set(
				selected
					.map(({ index }) => this.items[index].parentItem?.id)
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
		// Refresh the table — deleted items should no longer appear
		await this.populateTable();
		// Refresh toolbar badge
		try {
			if (window.opener && this.plugin) {
				this.plugin._scanUnavailableCount(window.opener);
			}
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
		/** @type {HTMLButtonElement} */ (document.getElementById('search-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('close-btn')).disabled = true;
		/** @type {HTMLButtonElement} */ (document.getElementById('refresh-btn')).disabled = true;

		const checkboxes = /** @type {NodeListOf<HTMLInputElement>} */ (
			document.querySelectorAll('#items-tbody input[type="checkbox"]')
		);
		const selected = Array.from(checkboxes)
			.map((cb, i) => ({ cb, index: i }))
			.filter(({ cb }) => cb.checked);

		// Mark all selected rows as queued
		for (const { index } of selected) {
			this.setRowStatus(index, 'searching', 'Queued...');
		}

		// Phase 1: batched sync downloads (10 at a time to avoid overwhelming the sync system)
		const BATCH_SIZE = 10;
		/** @type {Array<{index: number, downloaded: boolean, reason?: string}>} */
		const downloadResults = [];

		for (let batchStart = 0; batchStart < selected.length; batchStart += BATCH_SIZE) {
			const batch = selected.slice(batchStart, batchStart + BATCH_SIZE);
			const batchEnd = Math.min(batchStart + BATCH_SIZE, selected.length);
			this.setStatus(`Phase 1/2: downloading file(s) via Zotero sync (${batchEnd}/${selected.length})...`);

			for (const { index } of batch) {
				this.setRowStatus(index, 'searching', 'Downloading...');
			}

			const batchOutcomes = await Promise.allSettled(
				batch.map(({ index }) =>
					this.plugin._tryDownloadAttachment(this.items[index].attachmentItem)
						.then(r => ({ index, ...r }))
				)
			);

			for (const outcome of batchOutcomes) {
				if (outcome.status === 'rejected') {
					const { index } = /** @type {any} */ (outcome);
					this.setRowStatus(index, 'error', 'Download error');
					downloadResults.push({ index, downloaded: false, reason: 'rejected' });
				} else {
					const { index, downloaded, reason } = outcome.value;
					downloadResults.push({ index, downloaded, reason });
					if (downloaded) {
						this.setRowStatus(index, 'fixed', 'Downloaded');
					} else {
						this.setRowStatus(index, 'searching',
							reason === 'sync-disabled' ? 'Sync disabled — searching...' : 'Not downloaded — searching...'
						);
					}
				}
			}
		}

		// Collect items that still need the copy fallback
		/** @type {Array<{index: number}>} */
		const stillMissing = downloadResults
			.filter(r => !r.downloaded && r.reason !== 'rejected')
			.map(r => ({ index: r.index }));

		// Phase 2: copy from another library for items still missing
		let fixed = downloadResults.filter(r => r.downloaded).length;
		let notFound = 0;
		let errors = 0;

		if (stillMissing.length > 0) {
			this.setStatus(`Phase 2/2: searching other libraries for ${stillMissing.length} remaining file(s)...`);
			for (const { index } of stillMissing) {
				const info = this.items[index];
				try {
					const result = await this.plugin._searchAndFixUnavailableAttachment(info.attachmentItem);
					if (result.found && !result.error) {
						this.setRowStatus(index, 'fixed', `Fixed (${result.via})`);
						fixed++;
					} else if (result.found && result.error) {
						this.setRowStatus(index, 'error', `Copy failed: ${result.error}`, result.error);
						errors++;
					} else {
						this.setRowStatus(index, 'not-found', 'Not found');
						notFound++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(index, 'error', `Error: ${msg}`, msg);
					errors++;
					console.error(`fix-unavailable: error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}

		const parts = [];
		if (fixed > 0) parts.push(`${fixed} fixed`);
		if (notFound > 0) parts.push(`${notFound} not found`);
		if (errors > 0) parts.push(`${errors} error${errors !== 1 ? 's' : ''}`);
		this.setStatus(`Done. ${parts.join(', ')}.`);

		this.isRunning = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('close-btn')).disabled = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('refresh-btn')).disabled = false;
		/** @type {HTMLButtonElement} */ (document.getElementById('delete-btn')).disabled = false;
		this.updateSearchButton();

		// Refresh toolbar label count
		try {
			if (window.opener && this.plugin) {
				this.plugin._scanUnavailableCount(window.opener);
			}
		} catch (_) { /* opener may be gone */ }
	},

	/**
	 * Update a row's status cell.
	 * @param {number} index
	 * @param {string} cssClass - one of: searching, fixed, not-found, error
	 * @param {string} text
	 * @param {string} [tooltip]
	 * @returns {void}
	 */
	/**
	 * Return a short label for the attachment file type.
	 * @param {*} attachmentItem
	 * @returns {string}
	 */
	getFileTypeLabel(attachmentItem) {
		const mime = attachmentItem.attachmentContentType || '';
		if (mime === 'application/pdf') return 'PDF';
		if (mime === 'text/html') return 'HTML';
		if (mime === 'application/epub+zip') return 'EPUB';
		if (mime.startsWith('image/')) return mime.slice(6).toUpperCase();
		// Fall back to file extension
		const filename = attachmentItem.attachmentFilename || '';
		const ext = filename.includes('.') ? filename.split('.').pop().toUpperCase() : '';
		return ext || mime.split('/').pop() || '?';
	},

	setRowStatus(index, cssClass, text, tooltip = '') {
		const cell = document.getElementById(`status-${index}`);
		if (!cell) return;
		cell.textContent = text;
		cell.className = `status-cell ${cssClass}`;
		cell.title = tooltip;
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
