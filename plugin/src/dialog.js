// Dialog script for Zotero RAG query interface

// todo: can https://windingwind.github.io/zotero-plugin-toolkit/ be used?

// @ts-check

/// Import global types from scripts 
/// <reference path='./zotero-rag.js' />
/// <reference path='./remote_indexer.js' />

/**
 * @typedef {Object} LibraryIndexMetadata
 * @property {string} library_id - Library ID
 * @property {string} library_type - Library type (user/group)
 * @property {string} library_name - Library name
 * @property {number} last_indexed_version - Last indexed Zotero version
 * @property {string} last_indexed_at - ISO timestamp of last indexing
 * @property {number} total_items_indexed - Total items indexed
 * @property {number} total_chunks - Total chunks in vector store
 * @property {string} indexing_mode - Last indexing mode (full/incremental)
 * @property {boolean} force_reindex - Whether hard reset is pending
 */

/**
 * Dialog controller for Zotero RAG query interface.
 */
var ZoteroRAGDialog = {
	/** @type {ZoteroRAGPlugin|null} */
	plugin: null,

	/** @type {Set<string>} */
	selectedLibraries: new Set(),

	/** @type {Map<string, EventSource>} */

	/** @type {Map<string, LibraryIndexMetadata|null>} */
	libraryMetadata: new Map(),

	/** @type {boolean} */
	isOperationInProgress: false,

	/** @type {AbortController|null} */
	abortController: null,

	/**
	 * Cache of attachment Zotero key → local file path for attachments that have
	 * been downloaded in this dialog session.  Zotero's getFilePathAsync() can
	 * return null even after a successful download if the item's in-memory state
	 * is stale; we resolve the path immediately after download and store it here.
	 * @type {Map<string, string>}
	 */
	downloadedAttachmentPaths: new Map(),

	/** Zotero preference key for persisting permanently-failed download keys. */
	PREF_FAILED_DOWNLOADS: 'extensions.zotero-rag.failedDownloadKeys',

	/**
	 * Return the set of attachment keys that have permanently failed to download
	 * (i.e. the file does not exist on Zotero's sync server).
	 * @returns {Set<string>}
	 */
	_getFailedDownloadKeys() {
		try {
			return new Set(JSON.parse(Zotero.Prefs.get(this.PREF_FAILED_DOWNLOADS, true) || '[]'));
		} catch (_) {
			return new Set();
		}
	},

	/**
	 * Persist an attachment key as permanently failed so it is skipped in future sessions.
	 * @param {string} key
	 */
	_markDownloadFailed(key) {
		const keys = this._getFailedDownloadKeys();
		keys.add(key);
		Zotero.Prefs.set(this.PREF_FAILED_DOWNLOADS, JSON.stringify([...keys]), true);
	},

	/**
	 * Total number of indexable attachments per library, populated when a library
	 * is (re)selected.  Used to detect partial indexing by comparing against
	 * metadata.total_items_indexed.
	 * @type {Map<string, number>}
	 */
	libraryIndexableCount: new Map(),

	/**
	 * Number of attachments per library that are permanently unavailable (no local
	 * file could be obtained even after a download attempt).  Persisted in prefs
	 * and loaded when a library is selected.  Subtracted from totalIndexable so
	 * that a library is considered fully indexed even when some items have no file.
	 * @type {Map<string, number>}
	 */
	libraryUnavailableCount: new Map(),

	/**
	 * Initialize the dialog.
	 * @returns {void}
	 */
	init() {
		// Get plugin reference passed from main window
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (window.arguments && window.arguments[0]) {
			// @ts-ignore
			this.plugin = window.arguments[0].plugin;
		} else {
			console.error('No plugin reference passed to dialog');
			return;
		}

		// Set up event listeners
		const submitButton = document.getElementById('submit-button');
		if (submitButton) {
			submitButton.addEventListener('click', () => {
				this.submit();
			});
		}

		const cancelButton = document.getElementById('cancel-button');
		if (cancelButton) {
			cancelButton.addEventListener('click', () => {
				this.handleCancel();
			});
		}

		// Check if dev-mode is enabled and show force reindex checkbox if so
		this.initDevModeFeatures();

		// Set up similarity threshold slider
		const similaritySlider = document.getElementById('similarity-threshold');
		const similarityValue = document.getElementById('similarity-value');
		if (similaritySlider && similarityValue) {
			similaritySlider.addEventListener('input', (e) => {
				const value = /** @type {HTMLInputElement} */ (e.target).value;
				similarityValue.textContent = parseFloat(value).toFixed(1);
			});
		}

		// Set up sources-count slider
		const sourcesSlider = document.getElementById('sources-count');
		const sourcesValue = document.getElementById('sources-count-value');
		if (sourcesSlider && sourcesValue) {
			sourcesSlider.addEventListener('input', (e) => {
				sourcesValue.textContent = /** @type {HTMLInputElement} */ (e.target).value;
			});
		}

		// Load preset configuration and set default min_score
		this.loadPresetConfig();

		// Populate library list
		this.populateLibraries();
	},

	/**
	 * Initialize dev-mode features (force full reindex checkbox).
	 * @returns {void}
	 */
	initDevModeFeatures() {
		try {
			// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
			const isDevMode = Zotero.Prefs.get('extensions.zotero-plugin.dev-mode', false);

			if (isDevMode) {
				const forceReindexContainer = document.getElementById('force-reindex-container');
				if (forceReindexContainer) {
					forceReindexContainer.style.display = 'flex';
				}
			}
		} catch (error) {
			// Silently fail if pref is not available
			console.warn('Could not check dev-mode pref:', error);
		}
	},

	/**
	 * Load preset configuration from backend and set default similarity threshold.
	 * @returns {Promise<void>}
	 */
	async loadPresetConfig() {
		if (!this.plugin) return;

		try {
			const response = await fetch(`${this.plugin.backendURL}/api/config`, {
				headers: this.plugin.getAuthHeaders()
			});
			if (response.status === 401) {
				this.showStatus(
					'Authentication required: please set the API key in Zotero RAG preferences (Tools → Zotero RAG → Preferences).',
					'error'
				);
				return;
			}
			if (response.ok) {
				const config = await response.json();
				const defaultMinScore = config.default_min_score || 0.3;

				// Update slider and display
				const similaritySlider = /** @type {HTMLInputElement|null} */ (
					document.getElementById('similarity-threshold')
				);
				const similarityValue = document.getElementById('similarity-value');

				if (similaritySlider && similarityValue) {
					similaritySlider.value = defaultMinScore.toString();
					similarityValue.textContent = defaultMinScore.toFixed(1);
				}

				// Update sources-count slider from preset default_top_k
				const defaultTopK = config.default_top_k || 10;
				const sourcesSlider = /** @type {HTMLInputElement|null} */ (
					document.getElementById('sources-count')
				);
				const sourcesValue = document.getElementById('sources-count-value');
				if (sourcesSlider && sourcesValue) {
					sourcesSlider.value = defaultTopK.toString();
					sourcesValue.textContent = defaultTopK.toString();
				}

				this.plugin.log(`Loaded preset '${config.preset_name}' with min_score=${defaultMinScore}, top_k=${defaultTopK}`);
			}
		} catch (error) {
			// Silently fail and use hardcoded default (0.3)
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.plugin.log(`Could not load preset config: ${errorMessage}`);
		}
	},

	/**
	 * Fetch indexing metadata for a library.
	 * @param {string} libraryId - Library ID
	 * @returns {Promise<LibraryIndexMetadata|null>}
	 */
	async fetchLibraryMetadata(libraryId) {
		if (!this.plugin) return null;

		try {
			const response = await fetch(`${this.plugin.backendURL}/api/libraries/${libraryId}/index-status`, {
				headers: this.plugin.getAuthHeaders()
			});
			if (response.status === 404) {
				// Library not indexed yet
				return null;
			}
			if (response.status === 401) {
				throw new Error('Authentication required: please set the API key in Zotero RAG preferences (Tools → Zotero RAG → Preferences).');
			}
			if (!response.ok) {
				const ct = response.headers.get('content-type') || '';
				const body = ct.includes('application/json')
					? await response.json().catch(() => ({}))
					: { detail: (await response.text().catch(() => '')).slice(0, 300) };
				throw new Error(`GET /api/libraries/${libraryId}/index-status: HTTP ${response.status}${body.detail ? ` — ${body.detail}` : ''}`);
			}
			return await response.json();
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.plugin.log(`Error fetching metadata for library ${libraryId}: ${errorMessage}`);
			this.showStatus(errorMessage, 'error');
			return null;
		}
	},

	/**
	 * Populate the library selection list.
	 * @returns {Promise<void>}
	 */
	async populateLibraries() {
		if (!this.plugin) {
			return;
		}

		const libraries = this.plugin.getLibraries();
		const currentLibrary = this.plugin.getCurrentLibrary();
		const listContainer = document.getElementById('library-list');

		if (!listContainer) {
			return;
		}

		// Build UI without metadata - metadata will be loaded on selection
		for (let library of libraries) {

			const checkboxLabel = document.createElement('label');
			checkboxLabel.className = 'library-checkbox';

			const checkbox = document.createElement('input');
			checkbox.type = 'checkbox';
			checkbox.id = `library-${library.id}`;
			checkbox.setAttribute('data-library-id', library.id);

			// Check current library by default
			if (library.id === currentLibrary) {
				checkbox.checked = true;
				this.selectedLibraries.add(library.id);
			}

			// Load metadata when library is selected
			checkbox.addEventListener('change', async (e) => {
				const target = /** @type {HTMLInputElement} */ (e.target);
				const libraryId = target.getAttribute('data-library-id');
				if (libraryId) {
					if (target.checked) {
						this.selectedLibraries.add(libraryId);
						// Count indexable attachments so we can detect partial indexing.
						// Run in the background — result is used by updateLibraryStatusIcon.
						const lib = this.plugin ? this.plugin.getLibraries().find(l => l.id === libraryId) : null;
						const libraryType = lib ? lib.type : 'user';
						// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
						const unavailForLib = parseInt(Zotero.Prefs.get(`extensions.zotero-rag.unavailableItems.${libraryId}`, true) || '0') || 0;
						RemoteIndexer.countIndexableAttachments(libraryId, libraryType)
							.then(count => {
								this.libraryIndexableCount.set(libraryId, count);
								this.libraryUnavailableCount.set(libraryId, unavailForLib);
								this.updateLibraryStatusIcon(libraryId, this.libraryMetadata.get(libraryId) ?? null);
								this.updateSubmitButtonState();
							})
							.catch(() => {});
						// Fetch backend metadata
						if (!this.libraryMetadata.has(libraryId)) {
							await this.fetchAndUpdateLibraryMetadata(libraryId);
						} else {
							this.updateSubmitButtonState();
						}
					} else {
						this.selectedLibraries.delete(libraryId);
						this.updateSubmitButtonState();
					}
				}
			});

			// Status icon (hidden by CSS for now)
			const statusIcon = document.createElement('span');
			statusIcon.className = 'library-status-icon';
			statusIcon.id = `status-icon-${library.id}`;
			statusIcon.textContent = '\u2205'; // Empty set symbol for not indexed
			statusIcon.style.color = '#999';

			// Library name
			const nameSpan = document.createElement('span');
			nameSpan.className = 'library-name';
			nameSpan.textContent = library.name;

			// Metadata info (initially empty, will be populated on selection)
			const metaSpan = document.createElement('span');
			metaSpan.className = 'library-meta';
			metaSpan.id = `meta-${library.id}`;
			metaSpan.textContent = '';
			metaSpan.style.display = 'none'; // Hide until metadata is loaded

			// Re-index button (shown only when the library has been indexed before)
			const reindexBtn = document.createElement('button');
			reindexBtn.className = 'library-reindex-btn';
			reindexBtn.id = `reindex-btn-${library.id}`;
			reindexBtn.textContent = '\u267B'; // ♻ Recycling symbol
			reindexBtn.title = 'Re-index this library (keeps existing index, re-processes all items)';
			reindexBtn.style.cssText = 'display:none;background:none;border:none;outline:none;box-shadow:none;-moz-appearance:none;appearance:none;padding:0 3px;margin-left:4px;cursor:pointer;color:#888;font-size:13px;opacity:0.6;vertical-align:middle;';
			reindexBtn.addEventListener('click', (e) => {
				e.stopPropagation();
				e.preventDefault();
				this.reindexLibrary(library.id, library.name);
			});

			checkboxLabel.appendChild(checkbox);
			checkboxLabel.appendChild(statusIcon);
			checkboxLabel.appendChild(nameSpan);
			checkboxLabel.appendChild(metaSpan);
			checkboxLabel.appendChild(reindexBtn);
			listContainer.appendChild(checkboxLabel);
		}

		// Load metadata and indexable count for the currently selected library (if any)
		if (currentLibrary && this.selectedLibraries.has(currentLibrary)) {
			const currentLib = libraries.find(l => l.id === currentLibrary);
			const currentLibType = currentLib ? currentLib.type : 'user';
			// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
		const unavailForCurrent = parseInt(Zotero.Prefs.get(`extensions.zotero-rag.unavailableItems.${currentLibrary}`, true) || '0') || 0;
		// @ts-ignore - RemoteIndexer is a global in Zotero plugin context
		RemoteIndexer.countIndexableAttachments(currentLibrary, currentLibType)
				.then(count => {
					this.libraryIndexableCount.set(currentLibrary, count);
					this.libraryUnavailableCount.set(currentLibrary, unavailForCurrent);
					this.updateLibraryStatusIcon(currentLibrary, this.libraryMetadata.get(currentLibrary) ?? null);
					this.updateSubmitButtonState();
				})
				.catch(() => {});
			await this.fetchAndUpdateLibraryMetadata(currentLibrary);
			// updateSubmitButtonState() is called inside fetchAndUpdateLibraryMetadata
		} else {
			this.updateSubmitButtonState();
		}
	},

	/**
	 * Fetch metadata for a single library and update UI.
	 * @param {string} libraryId - Library ID
	 * @returns {Promise<void>}
	 */
	async fetchAndUpdateLibraryMetadata(libraryId) {
		try {
			const metaSpan = document.getElementById(`meta-${libraryId}`);
			if (metaSpan) {
				metaSpan.textContent = 'loading...';
				metaSpan.style.display = 'inline';
				metaSpan.style.fontStyle = 'italic';
				metaSpan.style.color = '#999';
			}

			const metadata = await this.fetchLibraryMetadata(libraryId);
			this.libraryMetadata.set(libraryId, metadata);

			// Update the UI
			this.updateLibraryStatusIcon(libraryId, metadata);
		} catch (error) {
			const metaSpan = document.getElementById(`meta-${libraryId}`);
			if (metaSpan) {
				metaSpan.textContent = 'error loading';
				metaSpan.style.color = '#cc0000';
			}
		}

		// Re-evaluate button state after metadata arrives
		this.updateSubmitButtonState();
	},

	/**
	 * Return true when every selected library needs (re-)indexing — either never
	 * indexed or only partially indexed (indexed count < indexable count).
	 * In this mode the question box is hidden and the button says "Index".
	 * @returns {boolean}
	 */
	isIndexOnlyMode() {
		if (this.selectedLibraries.size === 0) return false;
		for (const id of this.selectedLibraries) {
			// If metadata hasn't loaded yet, assume indexed (optimistic — avoids flicker)
			if (!this.libraryMetadata.has(id)) return false;
			const metadata = this.libraryMetadata.get(id);
			if (metadata == null) continue; // never indexed — counts as needing indexing
			// A library is "complete" when indexed count reaches the effective total
			// (total indexable minus permanently unavailable items).
			const totalIndexable = this.libraryIndexableCount.get(id);
			const unavailable = this.libraryUnavailableCount.get(id) || 0;
			const effectiveTotal = totalIndexable !== undefined ? totalIndexable - unavailable : undefined;
			if (effectiveTotal !== undefined && metadata.total_items_indexed >= effectiveTotal) return false;
			if (effectiveTotal === undefined && metadata.total_items_indexed > 0) return false;
			// indexed < effectiveTotal → still incomplete, needs (re-)indexing
		}
		return true;
	},

	/**
	 * Sync submit button label and question input state to current selection.
	 * - All selected libraries unindexed → button = "Index", question disabled
	 * - Otherwise                         → button = "Submit", question enabled
	 * @returns {void}
	 */
	updateSubmitButtonState() {
		const submitButton = /** @type {HTMLButtonElement|null} */ (
			document.getElementById('submit-button')
		);
		const questionInput = /** @type {HTMLTextAreaElement|null} */ (
			document.getElementById('question-input')
		);
		const questionLabel = document.querySelector('label[for="question-input"]');

		if (!submitButton) return;

		if (this.selectedLibraries.size === 0) {
			submitButton.disabled = true;
			submitButton.textContent = 'Submit';
			if (questionInput) {
				questionInput.disabled = false;
				questionInput.style.opacity = '';
				questionInput.placeholder = 'Enter your question here...';
			}
			if (questionLabel) /** @type {HTMLElement} */ (questionLabel).style.opacity = '';
		} else if (this.isIndexOnlyMode()) {
			submitButton.disabled = false;
			submitButton.textContent = 'Index';
			if (questionInput) {
				questionInput.disabled = true;
				questionInput.style.opacity = '0.4';
				questionInput.placeholder = 'Index the library first, then ask a question.';
			}
			if (questionLabel) /** @type {HTMLElement} */ (questionLabel).style.opacity = '0.4';
		} else {
			submitButton.disabled = false;
			submitButton.textContent = 'Submit';
			if (questionInput) {
				questionInput.disabled = false;
				questionInput.style.opacity = '';
				questionInput.placeholder = 'Enter your question here...';
			}
			if (questionLabel) /** @type {HTMLElement} */ (questionLabel).style.opacity = '';
		}
	},

	/**
	 * Update the status icon for a library after metadata is fetched.
	 * @param {string} libraryId - Library ID
	 * @param {LibraryIndexMetadata|null} metadata - Library metadata
	 * @returns {void}
	 */
	updateLibraryStatusIcon(libraryId, metadata) {
		const statusIcon = document.getElementById(`status-icon-${libraryId}`);
		const metaSpan = document.getElementById(`meta-${libraryId}`);

		const totalIndexable = this.libraryIndexableCount.get(libraryId);
		const unavailable = this.libraryUnavailableCount.get(libraryId) || 0;
		const effectiveTotal = totalIndexable !== undefined ? totalIndexable - unavailable : undefined;
		const indexed = metadata ? metadata.total_items_indexed : 0;
		const isPartial = metadata !== null
			&& effectiveTotal !== undefined
			&& indexed < effectiveTotal;

		if (statusIcon) {
			statusIcon.style.display = 'inline';
			if (!metadata) {
				statusIcon.textContent = '\u2205'; // Empty set — never indexed
				statusIcon.style.color = '#999';
			} else if (isPartial) {
				statusIcon.textContent = '\u26A0'; // Warning triangle — partially indexed
				statusIcon.style.color = '#e07800';
			} else {
				statusIcon.textContent = '\u2713'; // Checkmark — fully indexed
				statusIcon.style.color = '#008000';
			}
		}

		if (metaSpan) {
			metaSpan.style.display = 'inline';
			const unavailNote = unavailable > 0 ? ` · ${unavailable} unavailable` : '';
			if (!metadata) {
				metaSpan.textContent = 'not indexed';
				metaSpan.style.fontStyle = 'italic';
				metaSpan.style.color = '#999';
			} else if (isPartial) {
				const lastIndexed = new Date(metadata.last_indexed_at);
				const timeAgo = this.formatTimeAgo(lastIndexed);
				const total = effectiveTotal !== undefined ? `/${effectiveTotal}` : '';
				metaSpan.textContent = `${timeAgo} · ${indexed}${total} items (incomplete)${unavailNote}`;
				metaSpan.style.fontStyle = 'italic';
				metaSpan.style.color = '#e07800';
			} else {
				const lastIndexed = new Date(metadata.last_indexed_at);
				const timeAgo = this.formatTimeAgo(lastIndexed);
				const total = effectiveTotal !== undefined ? `/${effectiveTotal}` : '';
				metaSpan.textContent = `${timeAgo} · ${indexed}${total} items${unavailNote}`;
				metaSpan.style.fontStyle = 'normal';
				metaSpan.style.color = '#666';
			}
		}

		// Show the re-index button only when the library has been indexed at least once
		const reindexBtn = document.getElementById(`reindex-btn-${libraryId}`);
		if (reindexBtn) {
			reindexBtn.style.display = metadata ? 'inline' : 'none';
		}
	},

	/**
	 * Show a live progress text inside a library's list row.
	 * Pass null to restore the normal metadata display.
	 * @param {string} libraryId
	 * @param {string|null} progressText
	 */
	updateLibraryProgressText(libraryId, progressText) {
		const metaSpan = document.getElementById(`meta-${libraryId}`);
		const statusIcon = document.getElementById(`status-icon-${libraryId}`);
		if (!metaSpan) return;
		if (progressText === null) {
			// Restore normal metadata display
			this.updateLibraryStatusIcon(libraryId, this.libraryMetadata.get(libraryId) ?? null);
			return;
		}
		metaSpan.style.display = 'inline';
		metaSpan.textContent = progressText;
		metaSpan.style.fontStyle = 'italic';
		metaSpan.style.color = '#0066cc';
		if (statusIcon) {
			statusIcon.style.display = 'inline';
			statusIcon.textContent = '\u23F3'; // hourglass
			statusIcon.style.color = '#0066cc';
		}
	},

	/**
	 * Get library name by ID.
	 * @param {string} libraryId - Library ID
	 * @returns {string} Library name or ID if not found
	 */
	getLibraryName(libraryId) {
		if (!this.plugin) return libraryId;

		const libraries = this.plugin.getLibraries();
		const library = libraries.find(lib => lib.id === libraryId);
		return library ? library.name : libraryId;
	},

	/**
	 * Re-index a library after user confirmation.
	 * Keeps the existing vector store index; resets the unavailable-item count and
	 * runs a full indexing pass so every item (including previously skipped ones) is
	 * retried.  Any items that still can't be indexed are recorded as unavailable.
	 * @param {string} libraryId
	 * @param {string} [libraryName]
	 * @returns {Promise<void>}
	 */
	async reindexLibrary(libraryId, libraryName = '') {
		if (!this.plugin) return;
		if (this.isOperationInProgress) {
			this.showStatus('An operation is already in progress. Please wait or cancel it first.', 'error');
			return;
		}
		const name = libraryName || this.getLibraryName(libraryId);
		const confirmed = window.confirm(
			`Re-index "${name}"?\n\nThis will re-process all items and update the index. The existing index is kept — nothing will be deleted.`
		);
		if (!confirmed) return;

		// Reset the unavailable count so the fresh run starts clean
		this.libraryUnavailableCount.set(libraryId, 0);
		// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
		Zotero.Prefs.set(`extensions.zotero-rag.unavailableItems.${libraryId}`, 0, true);
		this.libraryMetadata.delete(libraryId);

		this.setSubmitEnabled(false);
		this.setCancelMode('abort');
		this.showProgress('Re-indexing...', `Starting re-index of "${name}"...`);

		try {
			await this.checkAndMonitorIndexing([libraryId], 'full');

			if (!this.isOperationInProgress) return;

			this.updateProgress(100, 'Re-indexing complete', `"${name}" has been re-indexed.`);

			// Refresh metadata and detect phantom gap
			this.libraryMetadata.delete(libraryId);
			await this.fetchAndUpdateLibraryMetadata(libraryId);
			const totalIndexable = this.libraryIndexableCount.get(libraryId);
			const freshMeta = this.libraryMetadata.get(libraryId);
			if (totalIndexable !== undefined && freshMeta != null) {
				const prevUnavail = this.libraryUnavailableCount.get(libraryId) || 0;
				const gap = Math.max(0, totalIndexable - freshMeta.total_items_indexed);
				const finalUnavail = Math.max(prevUnavail, gap);
				if (finalUnavail !== prevUnavail) {
					this.libraryUnavailableCount.set(libraryId, finalUnavail);
					// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
					Zotero.Prefs.set(`extensions.zotero-rag.unavailableItems.${libraryId}`, finalUnavail, true);
					this.updateLibraryStatusIcon(libraryId, freshMeta);
					this.updateSubmitButtonState();
				}
			}

			setTimeout(() => this.hideProgress(), 1500);
		} catch (error) {
			if (!this.isOperationInProgress) return;
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showStatus(`Error re-indexing "${name}": ${errorMessage}`, 'error');
		} finally {
			this.setCancelMode('close');
			this.setSubmitEnabled(true);
		}
	},

	/**
	 * Submit the query (or trigger indexing when all selected libraries are unindexed).
	 * @returns {Promise<void>}
	 */
	async submit() {
		this.showStatus(`[DEBUG] submit() called, plugin=${!!this.plugin}, libs=${this.selectedLibraries.size}`, 'info'); // DEBUG
		if (!this.plugin) return;

		if (this.selectedLibraries.size === 0) {
			this.showStatus('Please select at least one library.', 'error');
			return;
		}

		// Index-only mode: all selected libraries have never been indexed
		const indexOnly = this.isIndexOnlyMode();
		this.showStatus(`[DEBUG] isIndexOnlyMode=${indexOnly}`, 'info'); // DEBUG
		if (indexOnly) {
			await this.submitIndexOnly();
			return;
		}

		const questionInput = /** @type {HTMLTextAreaElement|null} */ (
			document.getElementById('question-input')
		);
		if (!questionInput) return;

		const question = questionInput.value.trim();

		// Get indexing mode from force reindex checkbox (dev mode only)
		const forceReindexCheckbox = /** @type {HTMLInputElement|null} */ (
			document.getElementById('force-full-reindex')
		);
		const indexingMode = (forceReindexCheckbox && forceReindexCheckbox.checked) ? 'full' : 'auto';

		// Get similarity threshold
		const similaritySlider = /** @type {HTMLInputElement|null} */ (
			document.getElementById('similarity-threshold')
		);
		const minScore = similaritySlider ? parseFloat(similaritySlider.value) : 0.3;

		// Get sources-to-consider count (top_k)
		const sourcesSlider = /** @type {HTMLInputElement|null} */ (
			document.getElementById('sources-count')
		);
		const topK = sourcesSlider ? parseInt(sourcesSlider.value, 10) : 10;

		// Validate input
		if (!question) {
			// @ts-ignore - Services is a Zotero/Firefox global
			Services.prompt.alert(window, 'Zotero RAG', 'Please enter a question.');
			return;
		}

		// Clear previous status messages
		this.clearStatusMessages();

		// Disable submit button and cancel button during operation
		this.setSubmitEnabled(false);
		this.setCancelMode('abort');
		this.showProgress('Processing request...', 'Submitting query...');

		try {
			const libraryIds = Array.from(this.selectedLibraries);

			// Skip attachment scan when all selected libraries are already fully indexed
			// and the user hasn't forced a full re-index. The cached metadata (loaded at
			// dialog-open time) already reflects any new items, so a mismatch between
			// total_items_indexed and effectiveTotal means something is new/changed.
			const allFullyIndexed = indexingMode !== 'full' && libraryIds.every(id => {
				if (!this.libraryMetadata.has(id)) return false;
				const metadata = this.libraryMetadata.get(id);
				if (metadata == null) return false;
				const totalIndexable = this.libraryIndexableCount.get(id);
				const unavailable = this.libraryUnavailableCount.get(id) || 0;
				const effectiveTotal = totalIndexable !== undefined ? totalIndexable - unavailable : undefined;
				if (effectiveTotal !== undefined) return metadata.total_items_indexed >= effectiveTotal;
				return metadata.total_items_indexed > 0;
			});

			if (!allFullyIndexed) {
				await this.checkAndMonitorIndexing(libraryIds, indexingMode);
			}

			// If cancelled mid-indexing, bail — abortOperation() already cleaned up.
			if (!this.isOperationInProgress) return;

			// Update progress for query phase
			this.updateProgress(0, 'Processing query', 'Sending query to backend...');

			const result = await this.plugin.submitQuery(question, libraryIds, {
				minScore: minScore,
				topK: topK
			});

			// Update progress for note creation phase
			this.updateProgress(50, 'Creating note', 'Formatting results...');

			await this.plugin.createResultNote(question, result, libraryIds);

			this.updateProgress(100, 'Complete', 'Note created successfully!');

			// Close dialog after successful completion
			setTimeout(() => {
				window.close();
			}, 1000);
		} catch (error) {
			// If cancelled, abortOperation() already cleaned up the UI — don't double-apply.
			if (!this.isOperationInProgress) return;

			const errorMessage = error instanceof Error ? error.message : String(error);
			// DEBUG
			this.plugin.log(`[DEBUG] submit() caught error: ${errorMessage}`);
			this.showStatus(`Error: ${errorMessage}`, 'error'); // DEBUG - show all errors directly

			// If the backend reports that a library has no indexed data, the vector
			// store is out of sync (e.g. indexing ran but extracted 0 chunks).
			// Clear the stale metadata so the UI reverts to "Index" mode.
			if (errorMessage.includes('None of the specified libraries have been indexed')) {
				this.showStatus('Index data is missing or corrupt — clearing cached index state...', 'error');
				await this.clearLibraryIndexState(Array.from(this.selectedLibraries));
				this.setSubmitEnabled(true);
				this.setCancelMode('close');
				this.hideProgress();
				return;
			}
			this.setSubmitEnabled(true);
			this.setCancelMode('close');
			this.hideProgress();
		}
	},

	/**
	 * Clear index state for a list of libraries in the vector store and refresh
	 * the UI so they show as unindexed.
	 * @param {string[]} libraryIds
	 * @returns {Promise<void>}
	 */
	async clearLibraryIndexState(libraryIds) {
		if (!this.plugin) return;
		const { backendURL } = this.plugin;
		this.showStatus(`[DEBUG] clearLibraryIndexState: ids=${libraryIds.join(',')} url=${backendURL}`, 'info'); // DEBUG

		for (const id of libraryIds) {
			try {
				const url = `${backendURL}/api/libraries/${encodeURIComponent(id)}/index`;
				this.showStatus(`[DEBUG] DELETE ${url}`, 'info'); // DEBUG
				const resp = await fetch(url, {
					method: 'DELETE',
					headers: this.plugin.getAuthHeaders(),
				});
				const body = await resp.text();
				this.showStatus(`[DEBUG] DELETE → ${resp.status}: ${body}`, 'info'); // DEBUG
			} catch (e) {
				this.showStatus(`[DEBUG] DELETE threw: ${e}`, 'error'); // DEBUG
			}
			// Remove from local cache so fetchAndUpdateLibraryMetadata re-fetches
			this.libraryMetadata.delete(id);
			await this.fetchAndUpdateLibraryMetadata(id);
		}
	},

	/**
	 * Index-only submit: triggered when all selected libraries are unindexed.
	 * Runs a full index, then refreshes metadata and switches to normal Submit mode.
	 * @returns {Promise<void>}
	 */
	async submitIndexOnly() {
		if (!this.plugin) return;

		this.clearStatusMessages();
		this.setSubmitEnabled(false);
		this.setCancelMode('abort');
		this.showProgress('Indexing...', 'Starting full index...');

		const libraryIds = Array.from(this.selectedLibraries);

		try {
			await this.checkAndMonitorIndexing(libraryIds, 'full');

			// If cancelled mid-indexing, checkAndMonitorIndexing already cleaned up the
			// local metadata state; abortOperation() already fixed the UI — bail out.
			if (!this.isOperationInProgress) return;

			this.updateProgress(100, 'Indexing complete', 'Libraries are ready to query.');

			// Refresh metadata for all indexed libraries so the button state updates
			for (const id of libraryIds) {
				this.libraryMetadata.delete(id);
				await this.fetchAndUpdateLibraryMetadata(id);
				// Phantom gap: items with local files that still couldn't be indexed count
				// as permanently unavailable (backend accepted them but didn't embed them).
				const totalIndexable = this.libraryIndexableCount.get(id);
				const freshMeta = this.libraryMetadata.get(id);
				if (totalIndexable !== undefined && freshMeta != null) {
					const prevUnavail = this.libraryUnavailableCount.get(id) || 0;
					const gap = Math.max(0, totalIndexable - freshMeta.total_items_indexed);
					const finalUnavail = Math.max(prevUnavail, gap);
					if (finalUnavail !== prevUnavail) {
						this.libraryUnavailableCount.set(id, finalUnavail);
						// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
						Zotero.Prefs.set(`extensions.zotero-rag.unavailableItems.${id}`, finalUnavail, true);
						this.updateLibraryStatusIcon(id, freshMeta);
						this.updateSubmitButtonState();
					}
				}
			}

			// updateSubmitButtonState() is called inside fetchAndUpdateLibraryMetadata,
			// so by now the button will read "Submit" and the question box is re-enabled.
			this.hideProgress();
			this.setCancelMode('close');
		} catch (error) {
			// If the error is due to cancellation, abortOperation() already cleaned up.
			if (!this.isOperationInProgress) return;
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showStatus(`Error: ${errorMessage}`, 'error');
			this.setCancelMode('close');
			this.hideProgress();
		}

		this.setSubmitEnabled(true);
	},

	/**
	 * Download missing attachments for items in a library.
	 * @param {string} libraryId - Library ID
	 * @param {string} libraryType - Library type ('user' or 'group')
	 * @param {string} [libraryName] - Human-readable library name for progress display
	 * @returns {Promise<void>}
	 */
	async downloadMissingAttachments(libraryId, libraryType, libraryName = '') {
		if (!this.plugin) return;

		const zoteroLibraryID = libraryType === 'group'
			? Zotero.Groups.get(parseInt(libraryId, 10))?.libraryID
			: parseInt(libraryId, 10);

		if (!zoteroLibraryID) {
			throw new Error(`Library ${libraryId} not found`);
		}

		// Check if sync storage is enabled for this library
		if (!Zotero.Sync.Storage.Local.getEnabledForLibrary(zoteroLibraryID)) {
			this.plugin.log(`Sync storage not enabled for library ${libraryId}, skipping attachment download`);
			return;
		}

		// Get all items in the library
		const search = new Zotero.Search();
		search.libraryID = zoteroLibraryID;
		const itemIDs = await search.search();

		if (itemIDs.length === 0) {
			return;
		}

		const items = await Zotero.Items.getAsync(itemIDs);

		// Collect all attachments that need downloading.
		// Only include indexable MIME types stored in Zotero (not URL-only links).
		const INDEXABLE_TYPES = new Set([
			'application/pdf',
			'text/html',
			'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
			'application/epub+zip',
		]);
		// Zotero link modes that store a file (imported_file=0, imported_url=1)
		const STORED_LINK_MODES = new Set([0, 1]);

		/** @type {Array<ZoteroItem>} */
		const attachmentsToDownload = [];
		const failedKeys = this._getFailedDownloadKeys();

		for (let item of items) {
			/** @type {Array<ZoteroItem>} */
			let attachments = [];
			if (item.isAttachment()) {
				attachments.push(item);
			} else if (item.isRegularItem()) {
				// @ts-ignore - loadDataType exists on Zotero items at runtime
				await item.loadDataType('childItems');
				attachments = Zotero.Items.get(item.getAttachments());
			} else {
				continue;
			}

			for (let attachment of attachments) {
				// Skip non-indexable types and URL-only attachments (no file to download)
				const mimeType = attachment.attachmentContentType || '';
				if (!INDEXABLE_TYPES.has(mimeType)) continue;
				if (!STORED_LINK_MODES.has(/** @type {any} */ (attachment).attachmentLinkMode)) continue;
				// Skip attachments that have permanently failed to download in a prior session
				if (failedKeys.has(attachment.key)) continue;

				const path = await attachment.getFilePathAsync();
				if (!path) {
					attachmentsToDownload.push(attachment);
				}
			}
		}

		if (attachmentsToDownload.length === 0) {
			this.plugin.log(`No missing attachments found for library ${libraryId}`);
			return;
		}

		this.plugin.log(`Found ${attachmentsToDownload.length} missing attachments for library ${libraryId}`);

		// Download attachments with progress updates
		let current = 0;
		const total = attachmentsToDownload.length;

		for (let attachment of attachmentsToDownload) {
			// Check if operation was cancelled
			if (!this.isOperationInProgress) {
				throw new Error('Download cancelled by user');
			}

			current++;
			const percentage = (current / total) * 100;
			const label = libraryName || libraryId;
			this.updateProgress(percentage, 'Downloading attachments', `${label}: ${current}/${total}`);
			this.updateLibraryProgressText(libraryId, `Downloading ${current}/${total}...`);

			try {
				await Zotero.Sync.Runner.downloadFile(attachment);
				// Cache the path right after download while the in-memory object is fresh.
				// getFilePathAsync() may return null later for stale item objects even
				// though the file is on disk.
				const downloadedPath = await attachment.getFilePathAsync();
				if (downloadedPath) {
					this.downloadedAttachmentPaths.set(attachment.key, downloadedPath);
				}
			} catch (error) {
				// Log error but continue with other attachments.
				// Persist the key so this attachment is never retried — the file is not
				// available on Zotero's sync server (e.g. linked file, never uploaded).
				const errorMessage = error instanceof Error ? error.message : String(error);
				this.plugin.log(`Error downloading attachment ${attachment.key}: ${errorMessage}`);
				this._markDownloadFailed(attachment.key);
			}
		}

		this.plugin.log(`Completed downloading ${total} attachments for library ${libraryId}`);
		this.updateLibraryProgressText(libraryId, null); // restore normal display
	},

	/**
	 * Check if libraries need indexing and monitor progress.
	 * @param {Array<string>} libraryIds - Library IDs to check
	 * @param {string} [mode='auto'] - Indexing mode (auto/incremental/full)
	 * @returns {Promise<void>}
	 */
	async checkAndMonitorIndexing(libraryIds, mode = 'auto') {
		if (!this.plugin) return;

		const backendURL = this.plugin.backendURL;
		if (!backendURL) return;
		const plugin = this.plugin;

		for (let libraryId of libraryIds) {
			try {
				const libraries = this.plugin.getLibraries();
				const library = libraries.find(lib => lib.id === libraryId);
				const libraryName = library ? library.name : libraryId;
				const libraryType = library ? library.type : 'user';

				this.showStatus(`Indexing library ${libraryName}...`, 'info');

				// Fresh AbortController for this library so cancel kills in-flight requests
				this.abortController = new AbortController();
				const indexResult = await RemoteIndexer.indexLibrary({
					libraryId,
					libraryType,
					libraryName,
					backendURL,
					mode,
					getAuthHeaders: (extra) => plugin.getAuthHeaders(extra),
					log: (msg) => plugin.log(msg),
					onProgress: ({ percentage, message, current, total }) => {
						const detail = total > 0 ? `${libraryName}: ${message} ${current}/${total}` : `${libraryName}: ${message}`;
						const phase = message.startsWith('Downloading') ? 'Downloading' : 'Indexing';
						this.updateProgress(total === 0 ? null : percentage, phase, detail);
						this.updateLibraryProgressText(
							libraryId,
							total > 0 ? `${current}/${total}` : null
						);
					},
					isCancelled: () => !this.isOperationInProgress,
					signal: this.abortController.signal,
					downloadedFilePaths: this.downloadedAttachmentPaths,
					downloadAttachment: async (zoteroItem) => {
						const key = zoteroItem.key;
						// BEGIN DEBUG
						plugin.log(`[DEBUG] downloadAttachment called for ${key} (libraryID=${zoteroItem.libraryID})`);
						// END DEBUG
						if (this._getFailedDownloadKeys().has(key)) {
							plugin.log(`[DEBUG] downloadAttachment: ${key} is in failed-download keys, skipping`); // DEBUG
							return null;
						}
						if (!Zotero.Sync.Storage.Local.getEnabledForLibrary(zoteroItem.libraryID)) {
							plugin.log(`[DEBUG] downloadAttachment: sync storage disabled for libraryID=${zoteroItem.libraryID}, skipping`); // DEBUG
							return null;
						}
						try {
							await Zotero.Sync.Runner.downloadFile(zoteroItem);
							const path = await zoteroItem.getFilePathAsync();
							if (path) this.downloadedAttachmentPaths.set(key, path);
							return path || null;
						} catch (error) {
							const msg = error instanceof Error ? error.message : String(error);
							plugin.log(`Error downloading attachment ${key}: ${msg}`);
							this._markDownloadFailed(key);
							return null;
						}
					},
				});
				this.abortController = null;
				this.updateLibraryProgressText(libraryId, null); // restore normal display

				// If the user cancelled while indexing was running, mark the library as
				// not-ready so the Index button reappears.  Backend data is kept intact
				// so the next run can resume incrementally.
				if (!this.isOperationInProgress) {
					this.libraryMetadata.set(libraryId, null);
					this.updateLibraryStatusIcon(libraryId, null);
					return;
				}

				// Persist unavailable count so status display survives dialog re-open
				const prevUnavail = this.libraryUnavailableCount.get(libraryId) || 0;
				const newUnavail = indexResult.noFile;
				if (newUnavail !== prevUnavail) {
					this.libraryUnavailableCount.set(libraryId, newUnavail);
					// @ts-ignore - Zotero.Prefs is available in Zotero plugin context
					Zotero.Prefs.set(`extensions.zotero-rag.unavailableItems.${libraryId}`, newUnavail, true);
				}

				// Report missing-file count as informational — never fatal
				if (newUnavail > 0) {
					const msg = `${newUnavail} attachment(s) in ${libraryName} have no local file and were skipped`;
					this.plugin.log(`[RemoteIndexer] ${msg}`);
					this.showStatus(msg, 'info');
				}
				// Upload errors are warnings but also non-fatal
				if (indexResult.errors > 0) {
					const detail = indexResult.firstError ? `: ${indexResult.firstError}` : '';
					this.plugin.log(`[RemoteIndexer] Warning: ${indexResult.errors} attachment(s) failed during indexing of ${libraryName}`);
					this.showStatus(`Warning: ${indexResult.errors} attachment(s) failed to index${detail}`, 'error');
				}

			} catch (error) {
				const errorMessage = error instanceof Error ? error.message : String(error);
				this.plugin.log(`Error indexing library ${libraryId}: ${errorMessage}`);
				throw error;
			}
		}
	},

	/**
	 * Monitor indexing progress via SSE.
	 * @param {string} libraryId - Library ID to monitor
	 * @returns {Promise<void>}
	 */
	/**
	 * Update progress bar with percentage and message.
	 * @param {number|null} percentage - Progress percentage (0-100), or null for indeterminate
	 * @param {string} label - Progress label
	 * @param {string|null} [message] - Optional detailed message
	 * @returns {void}
	 */
	updateProgress(percentage, label, message = null) {
		this.showProgress(label, message);
		const progressBar = /** @type {HTMLProgressElement|null} */ (
			document.getElementById('progress-bar')
		);
		if (progressBar) {
			if (percentage === null) {
				progressBar.removeAttribute('value'); // indeterminate
			} else {
				progressBar.value = percentage;
			}
		}
	},

	/**
	 * Show progress section with label and message.
	 * @param {string} label - Progress label
	 * @param {string|null} [message] - Optional detailed message
	 * @returns {void}
	 */
	showProgress(label, message = null) {
		const progressSection = document.getElementById('progress-section');
		const statusSection = document.getElementById('status-section');
		const labelElement = document.getElementById('progress-label');
		const messageElement = document.getElementById('progress-message');

		// Show progress, hide status
		if (progressSection) progressSection.style.display = '';
		if (statusSection) statusSection.style.display = 'none';

		if (labelElement) labelElement.textContent = label;
		if (messageElement) messageElement.textContent = message || '';
	},

	/**
	 * Hide progress section and reset to ready state.
	 * @returns {void}
	 */
	hideProgress() {
		const labelElement = document.getElementById('progress-label');
		const messageElement = document.getElementById('progress-message');
		const progressBar = /** @type {HTMLProgressElement|null} */ (
			document.getElementById('progress-bar')
		);

		// Reset to ready state
		if (labelElement) labelElement.textContent = 'Ready';
		if (messageElement) messageElement.textContent = '';
		if (progressBar) progressBar.value = 0;
	},

	/**
	 * Show status message.
	 * @param {string} message - Status message
	 * @param {'info'|'success'|'error'} [type] - Message type
	 * @returns {void}
	 */
	showStatus(message, type = 'info') {
		// Show status section (for errors), hide progress
		if (type === 'error') {
			const progressSection = document.getElementById('progress-section');
			const statusSection = document.getElementById('status-section');
			if (progressSection) progressSection.style.display = 'none';
			if (statusSection) statusSection.style.display = '';
		}

		const container = document.getElementById('status-messages');
		if (!container) return;

		const messageDiv = document.createElement('div');

		messageDiv.className = `status-message ${type}`;
		messageDiv.textContent = message;

		container.appendChild(messageDiv);
		container.scrollTop = container.scrollHeight;
	},

	/**
	 * Clear all status messages.
	 * @returns {void}
	 */
	clearStatusMessages() {
		const container = document.getElementById('status-messages');
		const statusSection = document.getElementById('status-section');
		const progressSection = document.getElementById('progress-section');

		if (container) container.innerHTML = '';
		if (statusSection) statusSection.style.display = 'none';
		if (progressSection) progressSection.style.display = '';
	},

	/**
	 * Enable or disable the submit button.
	 * @param {boolean} enabled - Whether button should be enabled
	 * @returns {void}
	 */
	setSubmitEnabled(enabled) {
		const submitButton = /** @type {HTMLButtonElement|null} */ (
			document.getElementById('submit-button')
		);
		if (submitButton) {
			submitButton.disabled = !enabled;
		}
	},

	/**
	 * Format timestamp as relative time.
	 * @param {Date} date - Date to format
	 * @returns {string} Relative time string
	 */
	formatTimeAgo(date) {
		const seconds = Math.floor((new Date().getTime() - date.getTime()) / 1000);

		if (seconds < 60) return 'just now';
		if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
		if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
		return `${Math.floor(seconds / 86400)} days ago`;
	},

	/**
	 * Handle cancel button click.
	 * @returns {Promise<void>}
	 */
	async handleCancel() {
		if (this.isOperationInProgress) {
			// Abort operation in progress
			await this.abortOperation();
		} else {
			// Just close the dialog
			window.close();
		}
	},

	/**
	 * Abort the current operation.
	 * @returns {Promise<void>}
	 */
	async abortOperation() {
		if (!this.plugin) return;

		this.showStatus('Cancelling operation...', 'info');

		// Abort any ongoing fetch requests
		if (this.abortController) {
			this.abortController.abort();
			this.abortController = null;
		}

		// Reset UI state
		this.isOperationInProgress = false;
		this.setSubmitEnabled(true);
		this.setCancelMode('close');
		this.showStatus('Operation cancelled by user.', 'info');
		this.hideProgress();
	},

	/**
	 * Set cancel button mode.
	 * @param {'close'|'abort'} mode - Button mode
	 * @returns {void}
	 */
	setCancelMode(mode) {
		const cancelButton = /** @type {HTMLButtonElement|null} */ (
			document.getElementById('cancel-button')
		);
		if (!cancelButton) return;

		if (mode === 'abort') {
			cancelButton.textContent = 'Cancel';
			cancelButton.title = 'Cancel the current operation';
			this.isOperationInProgress = true;
			this.setLibrarySelectionEnabled(false);
		} else {
			cancelButton.textContent = 'Close';
			cancelButton.title = 'Close this dialog';
			this.isOperationInProgress = false;
			this.setLibrarySelectionEnabled(true);
		}
	},

	/**
	 * Enable or disable library checkboxes and the library list container.
	 * @param {boolean} enabled
	 * @returns {void}
	 */
	setLibrarySelectionEnabled(enabled) {
		const listContainer = document.getElementById('library-list');
		if (!listContainer) return;

		const labels = /** @type {NodeListOf<HTMLElement>} */ (
			listContainer.querySelectorAll('label.library-checkbox')
		);
		for (const label of labels) {
			const cb = /** @type {HTMLInputElement|null} */ (label.querySelector('input[type="checkbox"]'));
			if (cb) cb.disabled = !enabled;
			label.style.opacity = enabled ? '' : '0.6';
			label.style.pointerEvents = enabled ? '' : 'none';
		}
	}
};

// Initialize when DOM is ready
if (document.readyState === 'loading') {
	document.addEventListener('DOMContentLoaded', () => {
		ZoteroRAGDialog.init();
	});
} else {
	ZoteroRAGDialog.init();
}
