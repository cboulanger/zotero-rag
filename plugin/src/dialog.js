// Dialog script for Zotero RAG query interface

// @ts-check

/**
 * @typedef {Object} Library
 * @property {string} id - Library ID
 * @property {string} name - Library name
 * @property {string} type - Library type (user/group)
 */

/**
 * @typedef {Object} QueryResult
 * @property {string} answer - Generated answer
 * @property {Array<SourceCitation>} sources - Source citations
 */

/**
 * @typedef {Object} SourceCitation
 * @property {string} item_id - Zotero item ID
 * @property {string} title - Document title
 * @property {number|null} page_number - Page number (if available)
 * @property {string|null} text_anchor - Text anchor (first 5 words)
 * @property {number} relevance_score - Relevance score
 */

/**
 * @typedef {Object} ZoteroRAGPlugin
 * @property {string} backendURL - Backend server URL
 * @property {function(): Array<Library>} getLibraries - Get available libraries
 * @property {function(): string} getCurrentLibrary - Get current library ID
 * @property {function(string, Array<string>): Promise<QueryResult>} submitQuery - Submit RAG query
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
 * Dialog controller for Zotero RAG query interface.
 */
var ZoteroRAGDialog = {
	/** @type {ZoteroRAGPlugin|null} */
	plugin: null,

	/** @type {Set<string>} */
	selectedLibraries: new Set(),

	/** @type {Map<string, EventSource>} */
	indexingStreams: new Map(),

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
				window.close();
			});
		}

		// Populate library list
		this.populateLibraries();
	},

	/**
	 * Populate the library selection list.
	 * @returns {void}
	 */
	populateLibraries() {
		if (!this.plugin) return;

		const libraries = this.plugin.getLibraries();
		const currentLibrary = this.plugin.getCurrentLibrary();
		const listContainer = document.getElementById('library-list');
		if (!listContainer) return;

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

			checkbox.addEventListener('change', (e) => {
				const target = /** @type {HTMLInputElement} */ (e.target);
				const libraryId = target.getAttribute('data-library-id');
				if (libraryId) {
					if (target.checked) {
						this.selectedLibraries.add(libraryId);
					} else {
						this.selectedLibraries.delete(libraryId);
					}
				}
			});

			const labelText = document.createTextNode(library.name);

			checkboxLabel.appendChild(checkbox);
			checkboxLabel.appendChild(labelText);
			listContainer.appendChild(checkboxLabel);
		}
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

		// Disable submit button
		this.setSubmitEnabled(false);
		this.showProgress('Processing request...', 'Submitting query...');

		try {
			const libraryIds = Array.from(this.selectedLibraries);
			await this.checkAndMonitorIndexing(libraryIds);

			const result = await this.plugin.submitQuery(question, libraryIds);
			await this.plugin.createResultNote(question, result, libraryIds);

			this.showStatus('Note created successfully!', 'success');

			setTimeout(() => {
				window.close();
			}, 1000);
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showStatus(`Error: ${errorMessage}`, 'error');
			this.setSubmitEnabled(true);
			this.hideProgress();
		}
	},

	/**
	 * Check if libraries need indexing and monitor progress.
	 * @param {Array<string>} libraryIds - Library IDs to check
	 * @returns {Promise<void>}
	 */
	async checkAndMonitorIndexing(libraryIds) {
		if (!this.plugin) return;

		const backendURL = this.plugin.backendURL;

		for (let libraryId of libraryIds) {
			try {
				const statusResponse = await fetch(`${backendURL}/api/libraries/${libraryId}/status`);
				const status = await statusResponse.json();

				if (!status.indexed || status.item_count === 0) {
					this.showStatus(`Indexing library ${libraryId}...`, 'info');

					const indexResponse = await fetch(`${backendURL}/api/index/library/${libraryId}`, {
						method: 'POST'
					});

					if (!indexResponse.ok) {
						const errorData = await indexResponse.json().catch(() => ({}));
						const errorMsg = errorData.detail || `HTTP ${indexResponse.status}`;
						throw new Error(`Failed to start indexing: ${errorMsg}`);
					}

					await this.monitorIndexingProgress(libraryId);
				}
			} catch (error) {
				const errorMessage = error instanceof Error ? error.message : String(error);
				this.plugin.log(`Error checking library ${libraryId}: ${errorMessage}`);
				this.showStatus(`Error indexing library ${libraryId}: ${errorMessage}`, 'error');
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

							if (data.message) {
								// Backend provided a message (like "Loading embedding model...")
								label = 'Indexing';
								detailedMessage = data.message;
							} else if (total > 0) {
								// Show document count
								label = 'Indexing';
								detailedMessage = `Processing ${current} of ${total} documents`;
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

			setTimeout(() => {
				if (this.indexingStreams.has(libraryId)) {
					eventSource.close();
					this.indexingStreams.delete(libraryId);
					reject(new Error('Indexing timeout'));
				}
			}, 300000);
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
