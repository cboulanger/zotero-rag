// Dialog script for Zotero RAG query interface

// @ts-check

// Type definitions (duplicated from zotero-rag.js for dialog context)

/**
 * @typedef {Object} Library
 * @property {string} id - Library ID
 * @property {string} name - Library name
 * @property {string} type - Library type (user/group)
 */

/**
 * @typedef {Object} QueryResult
 * @property {string} answer - Generated answer
 * @property {string} answer_format - Format of answer: "text", "html", or "markdown"
 * @property {Array<SourceCitation>} sources - Source citations
 */

/**
 * @typedef {Object} SourceCitation
 * @property {string} item_id - Zotero item ID
 * @property {string} library_id - Zotero library ID
 * @property {string} title - Document title
 * @property {number|null} page_number - Page number (if available)
 * @property {string|null} text_anchor - Text anchor (first 5 words)
 * @property {number} relevance_score - Relevance score
 */

/**
 * @typedef {Object} QueryOptions
 * @property {number} [topK] - Number of chunks to retrieve
 * @property {number} [minScore] - Minimum similarity score
 */

/**
 * @typedef {Object} ZoteroRAGPlugin
 * @property {string} backendURL - Backend server URL
 * @property {function(): Array<Library>} getLibraries - Get available libraries
 * @property {function(): string} getCurrentLibrary - Get current library ID
 * @property {function(string, Array<string>, QueryOptions=): Promise<QueryResult>} submitQuery - Submit RAG query
 * @property {function(string, QueryResult, Array<string>): Promise<void>} createResultNote - Create note with results
 * @property {function(string): void} log - Log message
 */

/**
 * @typedef {Object} SSEData
 * @property {string} event - Event type (started, progress, completed, error)
 * @property {string} [message] - Optional message
 * @property {number} [progress] - Progress percentage
 * @property {number} [current_item] - Current item number
 * @property {number} [total_items] - Total items
 */

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
	indexingStreams: new Map(),

	/** @type {Map<string, LibraryIndexMetadata|null>} */
	libraryMetadata: new Map(),

	/** @type {boolean} */
	isOperationInProgress: false,

	/** @type {AbortController|null} */
	abortController: null,

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
			const response = await fetch(`${this.plugin.backendURL}/api/config`);
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

				this.plugin.log(`Loaded preset '${config.preset_name}' with min_score=${defaultMinScore}`);
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
			const response = await fetch(`${this.plugin.backendURL}/api/libraries/${libraryId}/index-status`);
			if (response.status === 404) {
				// Library not indexed yet
				return null;
			}
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			return await response.json();
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.plugin.log(`Error fetching metadata for library ${libraryId}: ${errorMessage}`);
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
						// Fetch metadata if not already loaded
						if (!this.libraryMetadata.has(libraryId)) {
							await this.fetchAndUpdateLibraryMetadata(libraryId);
						}
					} else {
						this.selectedLibraries.delete(libraryId);
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

			checkboxLabel.appendChild(checkbox);
			checkboxLabel.appendChild(statusIcon);
			checkboxLabel.appendChild(nameSpan);
			checkboxLabel.appendChild(metaSpan);
			listContainer.appendChild(checkboxLabel);
		}

		// Load metadata for the currently selected library (if any)
		if (currentLibrary && this.selectedLibraries.has(currentLibrary)) {
			await this.fetchAndUpdateLibraryMetadata(currentLibrary);
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

		if (statusIcon) {
			if (metadata) {
				statusIcon.textContent = '\u2713'; // Checkmark for indexed
				statusIcon.style.color = '#008000';
			} else {
				statusIcon.textContent = '\u2205'; // Empty set symbol for not indexed
				statusIcon.style.color = '#999';
			}
		}

		if (metaSpan) {
			metaSpan.style.display = 'inline'; // Show the metadata span
			if (metadata) {
				const lastIndexed = new Date(metadata.last_indexed_at);
				const timeAgo = this.formatTimeAgo(lastIndexed);
				metaSpan.textContent = `${timeAgo} · ${metadata.total_items_indexed} items`;
				metaSpan.style.fontStyle = 'normal';
				metaSpan.style.color = '#666';
			} else {
				metaSpan.textContent = 'not indexed';
				metaSpan.style.fontStyle = 'italic';
				metaSpan.style.color = '#999';
			}
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
	 * Submit the query to the backend.
	 * @returns {Promise<void>}
	 */
	async submit() {
		if (!this.plugin) return;

		const questionInput = /** @type {HTMLInputElement|null} */ (
			document.getElementById('question-input')
		);
		if (!questionInput) return;

		const question = questionInput.value.trim();

		// Get indexing mode from force reindex checkbox (dev mode only)
		const forceReindexCheckbox = /** @type {HTMLInputElement|null} */ (
			document.getElementById('force-full-reindex')
		);
		const indexingMode = (forceReindexCheckbox && forceReindexCheckbox.checked) ? 'full' : 'incremental';

		// Get similarity threshold
		const similaritySlider = /** @type {HTMLInputElement|null} */ (
			document.getElementById('similarity-threshold')
		);
		const minScore = similaritySlider ? parseFloat(similaritySlider.value) : 0.3;

		// Validate input
		if (!question) {
			this.showStatus('Please enter a question.', 'error');
			return;
		}

		if (this.selectedLibraries.size === 0) {
			this.showStatus('Please select at least one library.', 'error');
			return;
		}

		// Clear previous status messages
		this.clearStatusMessages();

		// Disable submit button and cancel button during operation
		this.setSubmitEnabled(false);
		this.setCancelMode('abort'); // Change cancel button to abort mode
		this.showProgress('Processing request...', 'Submitting query...');

		try {
			const libraryIds = Array.from(this.selectedLibraries);
			await this.checkAndMonitorIndexing(libraryIds, indexingMode);

			// Update progress for query phase
			this.updateProgress(0, 'Processing query', 'Sending query to backend...');

			const result = await this.plugin.submitQuery(question, libraryIds, {
				minScore: minScore
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
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showStatus(`Error: ${errorMessage}`, 'error');
			this.setSubmitEnabled(true);
			this.setCancelMode('close'); // Restore cancel button to close mode
			this.hideProgress();
		}
	},

	/**
	 * Download missing attachments for items in a library.
	 * @param {string} libraryId - Library ID
	 * @param {string} libraryType - Library type ('user' or 'group')
	 * @returns {Promise<void>}
	 */
	async downloadMissingAttachments(libraryId, libraryType) {
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

		// Collect all attachments that need downloading
		/** @type {Array<ZoteroItem>} */
		const attachmentsToDownload = [];

		for (let item of items) {
			/** @type {Array<ZoteroItem>} */
			let attachments = [];
			if (item.isAttachment()) {
				attachments.push(item);
			} else if (item.isRegularItem()) {
				attachments = Zotero.Items.get(item.getAttachments());
			} else {
				continue;
			}

			for (let attachment of attachments) {
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
			this.updateProgress(
				percentage,
				'Downloading attachments',
				`Downloading attachment ${current} of ${total}`
			);

			try {
				await Zotero.Sync.Runner.downloadFile(attachment);
			} catch (error) {
				// Log error but continue with other attachments
				const errorMessage = error instanceof Error ? error.message : String(error);
				this.plugin.log(`Error downloading attachment ${attachment.id}: ${errorMessage}`);
			}
		}

		this.plugin.log(`Completed downloading ${total} attachments for library ${libraryId}`);
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

		for (let libraryId of libraryIds) {
			try {
				const statusResponse = await fetch(`${backendURL}/api/libraries/${libraryId}/status`);
				const status = await statusResponse.json();

				// For incremental mode, always trigger indexing to catch updates
				// For full mode, force reindexing
				// For auto mode, let backend decide
				const shouldIndex = !status.indexed || status.item_count === 0 || mode !== 'auto';

				if (shouldIndex) {
					// Get library name and type for user-friendly messages
					const libraries = this.plugin.getLibraries();
					const library = libraries.find(lib => lib.id === libraryId);
					const libraryName = library ? library.name : libraryId;
					const libraryType = library ? library.type : 'user';

					// Download missing attachments before indexing
					this.showStatus(`Checking attachments for ${libraryName}...`, 'info');
					await this.downloadMissingAttachments(libraryId, libraryType);

					this.showStatus(`Indexing library ${libraryName}...`, 'info');

					// Build URL with mode parameter
					const indexURL = `${backendURL}/api/index/library/${libraryId}?mode=${mode}&library_type=${libraryType}&library_name=${encodeURIComponent(libraryName)}`;

					const indexResponse = await fetch(indexURL, {
						method: 'POST'
					});

					if (!indexResponse.ok) {
						const errorData = await indexResponse.json().catch(() => ({}));

						// If indexing is already in progress (409 conflict), reconnect to it
						if (indexResponse.status === 409) {
							this.plugin.log(`Library ${libraryId} is already being indexed, reconnecting to progress stream...`);
							this.showStatus(`Reconnecting to indexing for ${libraryName}...`, 'info');
						} else {
							// For other errors, throw
							const errorMsg = errorData.detail || `HTTP ${indexResponse.status}`;
							throw new Error(`Failed to start indexing: ${errorMsg}`);
						}
					}

					// Monitor progress (whether we just started it or reconnected to existing)
					await this.monitorIndexingProgress(libraryId);
				}
			} catch (error) {
				const errorMessage = error instanceof Error ? error.message : String(error);
				const libraryName = this.getLibraryName(libraryId);
				this.plugin.log(`Error checking library ${libraryId}: ${errorMessage}`);
				this.showStatus(`Error indexing library ${libraryName}: ${errorMessage}`, 'error');
				throw error; // Re-throw to stop the query process
			}
		}
	},

	/**
	 * Monitor indexing progress via SSE.
	 * @param {string} libraryId - Library ID to monitor
	 * @returns {Promise<void>}
	 */
	monitorIndexingProgress(libraryId) {
		return new Promise((resolve, reject) => {
			if (!this.plugin) {
				reject(new Error('Plugin not initialized'));
				return;
			}

			const backendURL = this.plugin.backendURL;
			const eventSource = new EventSource(`${backendURL}/api/index/library/${libraryId}/progress`);

			this.indexingStreams.set(libraryId, eventSource);

			eventSource.onmessage = (event) => {
				try {
					const data = /** @type {SSEData} */ (JSON.parse(event.data));

					switch (data.event) {
						case 'started':
							this.updateProgress(0, 'Starting indexing...', data.message || '');
							break;

						case 'progress':
							const percentage = data.progress || 0;
							const current = data.current_item || 0;
							const total = data.total_items || 0;

							// Determine label and detailed message
							let label, detailedMessage;

							// Priority: document count > custom message > generic progress
							if (total > 0) {
								// Show document count
								label = 'Indexing';
								detailedMessage = `Indexing document ${current} of ${total}`;
							} else if (data.message) {
								// Backend provided a message (like "Loading embedding model...")
								label = 'Indexing';
								detailedMessage = data.message;
							} else {
								// Generic progress
								label = 'Processing';
								detailedMessage = `${percentage.toFixed(0)}% complete`;
							}

							this.updateProgress(percentage, label, detailedMessage);
							break;

						case 'completed':
							this.updateProgress(100, 'Completed', data.message || '');
							eventSource.close();
							this.indexingStreams.delete(libraryId);
							resolve();
							break;

						case 'error':
							this.updateProgress(0, 'Error', '');
							this.showStatus(`Error: ${data.message || 'Unknown error'}`, 'error');
							eventSource.close();
							this.indexingStreams.delete(libraryId);
							reject(new Error(data.message || 'Indexing failed'));
							break;
					}
				} catch (error) {
					const errorMessage = error instanceof Error ? error.message : String(error);
					if (this.plugin) {
						this.plugin.log(`Error parsing SSE data: ${errorMessage}`);
					}
				}
			};

			eventSource.onerror = () => {
				if (this.plugin) {
					this.plugin.log(`SSE connection error for library ${libraryId}`);
				}
				eventSource.close();
				this.indexingStreams.delete(libraryId);
				resolve();
			};
		});
	},

	/**
	 * Update progress bar with percentage and message.
	 * @param {number} percentage - Progress percentage (0-100)
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
			progressBar.value = percentage;
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

		// Close all active SSE streams
		for (const [libraryId, eventSource] of this.indexingStreams.entries()) {
			eventSource.close();

			// Send abort signal to backend
			try {
				await fetch(`${this.plugin.backendURL}/api/index/library/${libraryId}/cancel`, {
					method: 'POST'
				});
			} catch (error) {
				// Log but don't throw - cancellation should always succeed
				const errorMessage = error instanceof Error ? error.message : String(error);
				this.plugin.log(`Error cancelling indexing for library ${libraryId}: ${errorMessage}`);
			}
		}
		this.indexingStreams.clear();

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
		} else {
			cancelButton.textContent = 'Close';
			cancelButton.title = 'Close this dialog';
			this.isOperationInProgress = false;
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
