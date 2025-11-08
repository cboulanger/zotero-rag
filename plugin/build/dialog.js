// Dialog script for Zotero RAG query interface

var ZoteroRAGDialog = {
	plugin: null,
	selectedLibraries: new Set(),
	indexingStreams: new Map(),

	init() {
		// Get plugin reference passed from main window
		if (window.arguments && window.arguments[0]) {
			this.plugin = window.arguments[0].plugin;
		} else {
			console.error('No plugin reference passed to dialog');
			return;
		}

		// Set up event listeners
		document.getElementById('submit-button').addEventListener('click', () => {
			this.submit();
		});

		document.getElementById('cancel-button').addEventListener('click', () => {
			window.close();
		});

		// Populate library list
		this.populateLibraries();
	},

	populateLibraries() {
		const libraries = this.plugin.getLibraries();
		const currentLibrary = this.plugin.getCurrentLibrary();
		const listContainer = document.getElementById('library-list');

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
				const libraryId = e.target.getAttribute('data-library-id');
				if (e.target.checked) {
					this.selectedLibraries.add(libraryId);
				} else {
					this.selectedLibraries.delete(libraryId);
				}
			});

			const labelText = document.createTextNode(library.name);

			checkboxLabel.appendChild(checkbox);
			checkboxLabel.appendChild(labelText);
			listContainer.appendChild(checkboxLabel);
		}
	},

	async submit() {
		const question = document.getElementById('question-input').value.trim();

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
			this.showStatus(`Error: ${error.message}`, 'error');
			this.setSubmitEnabled(true);
			this.hideProgress();
		}
	},

	async checkAndMonitorIndexing(libraryIds) {
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
				this.plugin.log(`Error checking library ${libraryId}: ${error.message}`);
				this.showStatus(`Error indexing library ${libraryId}: ${error.message}`, 'error');
				throw error; // Re-throw to stop the query process
			}
		}
	},

	monitorIndexingProgress(libraryId) {
		return new Promise((resolve, reject) => {
			const backendURL = this.plugin.backendURL;
			const eventSource = new EventSource(`${backendURL}/api/index/library/${libraryId}/progress`);

			this.indexingStreams.set(libraryId, eventSource);

			eventSource.onmessage = (event) => {
				try {
					const data = JSON.parse(event.data);

					switch (data.event) {
						case 'started':
							this.updateProgress(0, 'Starting indexing...', data.message);
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
							this.updateProgress(100, 'Completed', data.message);
							eventSource.close();
							this.indexingStreams.delete(libraryId);
							resolve();
							break;

						case 'error':
							this.updateProgress(0, 'Error');
							this.showStatus(`Error: ${data.message}`, 'error');
							eventSource.close();
							this.indexingStreams.delete(libraryId);
							reject(new Error(data.message));
							break;
					}
				} catch (error) {
					this.plugin.log(`Error parsing SSE data: ${error.message}`);
				}
			};

			eventSource.onerror = () => {
				this.plugin.log(`SSE connection error for library ${libraryId}`);
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

	updateProgress(percentage, label, message = null) {
		this.showProgress(label, message);
		const progressBar = document.getElementById('progress-bar');
		progressBar.value = percentage;
	},

	showProgress(label, message = null) {
		const progressSection = document.getElementById('progress-section');
		const statusSection = document.getElementById('status-section');
		const labelElement = document.getElementById('progress-label');
		const messageElement = document.getElementById('progress-message');

		// Show progress, hide status
		progressSection.style.display = '';
		statusSection.style.display = 'none';

		labelElement.textContent = label;
		messageElement.textContent = message || '';
	},

	hideProgress() {
		const labelElement = document.getElementById('progress-label');
		const messageElement = document.getElementById('progress-message');

		// Reset to ready state
		labelElement.textContent = 'Ready';
		messageElement.textContent = '';
		document.getElementById('progress-bar').value = 0;
	},

	showStatus(message, type = 'info') {
		// Show status section (for errors), hide progress
		if (type === 'error') {
			const progressSection = document.getElementById('progress-section');
			const statusSection = document.getElementById('status-section');
			progressSection.style.display = 'none';
			statusSection.style.display = '';
		}

		const container = document.getElementById('status-messages');
		const messageDiv = document.createElement('div');

		messageDiv.className = `status-message ${type}`;
		messageDiv.textContent = message;

		container.appendChild(messageDiv);
		container.scrollTop = container.scrollHeight;
	},

	clearStatusMessages() {
		const container = document.getElementById('status-messages');
		const statusSection = document.getElementById('status-section');
		const progressSection = document.getElementById('progress-section');

		container.innerHTML = '';
		statusSection.style.display = 'none';
		progressSection.style.display = '';
	},

	setSubmitEnabled(enabled) {
		const submitButton = document.getElementById('submit-button');
		submitButton.disabled = !enabled;
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
