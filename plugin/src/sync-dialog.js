// @ts-check

/**
 * @file Sync dialog controller for Zotero RAG plugin
 * Manages vector database synchronization UI
 */

/**
 * @typedef {import('./sync-client.js').SyncStatus} SyncStatus
 * @typedef {import('./sync-client.js').SyncResponse} SyncResponse
 * @typedef {import('./sync-client.js').SyncConfig} SyncConfig
 */

/**
 * @typedef {Object} ZoteroRAGPlugin
 * @property {string} backendURL - Backend server URL
 * @property {function(string): void} log - Log message
 * @property {function(): Array<{id: string, name: string, type: string}>} getLibraries - Get libraries
 */

/**
 * Dialog controller for vector synchronization.
 */
var ZoteroRAGSyncDialog = {
	/** @type {ZoteroRAGPlugin|null} */
	plugin: null,

	/** @type {ZoteroRAGSyncClient|null} */
	syncClient: null,

	/** @type {Map<string, SyncStatus>} */
	libraryStatuses: new Map(),

	/** @type {boolean} */
	isOperationInProgress: false,

	/**
	 * Initialize the dialog.
	 * @returns {Promise<void>}
	 */
	async init() {
		// Get plugin reference passed from main window
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (window.arguments && window.arguments[0]) {
			// @ts-ignore
			this.plugin = window.arguments[0].plugin;
		} else {
			console.error('No plugin reference passed to sync dialog');
			return;
		}

		// Initialize sync client
		// @ts-ignore - ZoteroRAGSyncClient is loaded via script tag
		this.syncClient = new ZoteroRAGSyncClient(this.plugin.backendURL);

		// Set up event listeners
		const closeButton = document.getElementById('close-button');
		if (closeButton) {
			closeButton.addEventListener('click', () => {
				window.close();
			});
		}

		const refreshButton = document.getElementById('refresh-button');
		if (refreshButton) {
			refreshButton.addEventListener('click', async () => {
				await this.refreshLibraries();
			});
		}

		const syncAllButton = document.getElementById('sync-all-button');
		if (syncAllButton) {
			syncAllButton.addEventListener('click', async () => {
				await this.syncAllLibraries();
			});
		}

		// Check sync configuration and load libraries
		await this.checkSyncConfiguration();
	},

	/**
	 * Check if sync is enabled on backend.
	 * @returns {Promise<void>}
	 */
	async checkSyncConfiguration() {
		if (!this.syncClient) return;

		const configInfo = document.getElementById('sync-config-info');
		if (!configInfo) return;

		try {
			const config = await this.syncClient.checkSyncEnabled();

			if (config.enabled) {
				configInfo.innerHTML = `
					<span class="enabled">Sync Enabled</span>
					<span> | Backend: ${config.backend}</span>
					<span> | Auto-pull: ${config.auto_pull ? 'Yes' : 'No'}</span>
					<span> | Auto-push: ${config.auto_push ? 'Yes' : 'No'}</span>
				`;
				// Load libraries
				await this.loadLibraries();
			} else {
				configInfo.innerHTML = `
					<span class="disabled">Sync Not Enabled</span>
					<span> - Configure sync settings in backend .env file</span>
				`;
				this.showMessage('Sync is not enabled on the backend. Please configure sync settings.', 'error');
				this.disableButtons();
			}
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			configInfo.innerHTML = `<span class="disabled">Error checking sync configuration</span>`;
			this.showMessage(`Failed to check sync configuration: ${errorMessage}`, 'error');
			this.disableButtons();
		}
	},

	/**
	 * Disable all action buttons.
	 * @returns {void}
	 */
	disableButtons() {
		const syncAllButton = /** @type {HTMLButtonElement|null} */ (
			document.getElementById('sync-all-button')
		);
		const refreshButton = /** @type {HTMLButtonElement|null} */ (
			document.getElementById('refresh-button')
		);
		if (syncAllButton) syncAllButton.disabled = true;
		if (refreshButton) refreshButton.disabled = true;
	},

	/**
	 * Load and display libraries with sync status.
	 * @returns {Promise<void>}
	 */
	async loadLibraries() {
		if (!this.plugin || !this.syncClient) return;

		const listContainer = document.getElementById('library-list');
		if (!listContainer) return;

		listContainer.innerHTML = '<div class="loading">Loading libraries...</div>';

		try {
			// Get all libraries from Zotero
			const libraries = this.plugin.getLibraries();

			// Filter to only indexed libraries (those with vector data)
			const indexedLibraries = [];
			for (const lib of libraries) {
				try {
					// Check if library has index data
					const response = await fetch(`${this.plugin.backendURL}/api/libraries/${lib.id}/index-status`);
					if (response.ok) {
						indexedLibraries.push(lib);
					}
				} catch (error) {
					// Skip libraries that aren't indexed
					continue;
				}
			}

			if (indexedLibraries.length === 0) {
				listContainer.innerHTML = '<div class="loading">No indexed libraries found. Index a library first.</div>';
				return;
			}

			// Load sync status for each library
			listContainer.innerHTML = '';
			for (const lib of indexedLibraries) {
				await this.addLibraryItem(lib, listContainer);
			}
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.plugin.log(`Error loading libraries: ${errorMessage}`);
			listContainer.innerHTML = `<div class="loading" style="color: #cc0000;">Error loading libraries: ${errorMessage}</div>`;
		}
	},

	/**
	 * Add a library item to the list.
	 * @param {{id: string, name: string, type: string}} library - Library info
	 * @param {HTMLElement} container - Container element
	 * @returns {Promise<void>}
	 */
	async addLibraryItem(library, container) {
		if (!this.syncClient) return;

		// Create library item container
		const item = document.createElement('div');
		item.className = 'library-item';
		item.id = `library-item-${library.id}`;

		// Library info section
		const infoDiv = document.createElement('div');
		infoDiv.className = 'library-info';

		const nameDiv = document.createElement('div');
		nameDiv.className = 'library-name';
		nameDiv.textContent = library.name;

		const statusDiv = document.createElement('div');
		statusDiv.className = 'library-status';
		statusDiv.id = `status-${library.id}`;
		statusDiv.innerHTML = '<span style="color: #999; font-style: italic;">Loading status...</span>';

		infoDiv.appendChild(nameDiv);
		infoDiv.appendChild(statusDiv);

		// Actions section
		const actionsDiv = document.createElement('div');
		actionsDiv.className = 'library-actions';
		actionsDiv.id = `actions-${library.id}`;

		item.appendChild(infoDiv);
		item.appendChild(actionsDiv);
		container.appendChild(item);

		// Load sync status
		try {
			const status = await this.syncClient.getSyncStatus(library.id);
			this.libraryStatuses.set(library.id, status);
			this.updateLibraryStatus(library.id, status);
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			statusDiv.innerHTML = `<span style="color: #cc0000;">Error: ${errorMessage}</span>`;
		}
	},

	/**
	 * Update library status display.
	 * @param {string} libraryId - Library ID
	 * @param {SyncStatus} status - Sync status
	 * @returns {void}
	 */
	updateLibraryStatus(libraryId, status) {
		if (!this.syncClient) return;

		const statusDiv = document.getElementById(`status-${libraryId}`);
		const actionsDiv = document.getElementById(`actions-${libraryId}`);
		if (!statusDiv || !actionsDiv) return;

		// Update status display
		const statusBadge = this.createStatusBadge(status);
		const versionInfo = this.createVersionInfo(status);
		statusDiv.innerHTML = '';
		statusDiv.appendChild(statusBadge);
		statusDiv.appendChild(versionInfo);

		// Update action buttons
		actionsDiv.innerHTML = '';

		if (status.needs_pull) {
			const pullButton = this.createActionButton('Pull', 'primary', async () => {
				await this.pullLibrary(libraryId);
			});
			actionsDiv.appendChild(pullButton);
		}

		if (status.needs_push) {
			const pushButton = this.createActionButton('Push', 'primary', async () => {
				await this.pushLibrary(libraryId);
			});
			actionsDiv.appendChild(pushButton);
		}

		if (status.sync_status === 'same') {
			const syncButton = this.createActionButton('Sync', '', async () => {
				await this.syncLibrary(libraryId);
			});
			actionsDiv.appendChild(syncButton);
		}

		if (status.sync_status === 'diverged') {
			// Show both options for conflict resolution
			const pullButton = this.createActionButton('Force Pull', '', async () => {
				if (confirm('Force pull will overwrite local vectors. Continue?')) {
					await this.pullLibrary(libraryId, true);
				}
			});
			const pushButton = this.createActionButton('Force Push', '', async () => {
				if (confirm('Force push will overwrite remote vectors. Continue?')) {
					await this.pushLibrary(libraryId, true);
				}
			});
			actionsDiv.appendChild(pullButton);
			actionsDiv.appendChild(pushButton);
		}
	},

	/**
	 * Create status badge element.
	 * @param {SyncStatus} status - Sync status
	 * @returns {HTMLElement} Badge element
	 */
	createStatusBadge(status) {
		if (!this.syncClient) {
			const span = document.createElement('span');
			span.textContent = 'Unknown';
			return span;
		}

		const badge = document.createElement('span');
		badge.className = 'sync-status-badge';

		const icon = this.syncClient.getSyncStatusIcon(status);
		const statusText = this.syncClient.formatSyncStatus(status.sync_status);
		const color = this.syncClient.getSyncStatusColor(status);

		// Add specific class for styling
		if (status.sync_status === 'same') {
			badge.classList.add('in-sync');
		} else if (status.sync_status === 'local_newer') {
			badge.classList.add('needs-push');
		} else if (status.sync_status === 'remote_newer') {
			badge.classList.add('needs-pull');
		} else if (status.sync_status === 'diverged') {
			badge.classList.add('conflict');
		} else {
			badge.classList.add('not-synced');
		}

		badge.innerHTML = `<span style="color: ${color};">${icon}</span> ${statusText}`;

		return badge;
	},

	/**
	 * Create version info element.
	 * @param {SyncStatus} status - Sync status
	 * @returns {HTMLElement} Version info element
	 */
	createVersionInfo(status) {
		const span = document.createElement('span');
		span.style.fontSize = '11px';
		span.style.color = '#999';

		const parts = [];
		if (status.local_exists) {
			parts.push(`Local: v${status.local_version} (${status.local_chunks} chunks)`);
		}
		if (status.remote_exists) {
			parts.push(`Remote: v${status.remote_version} (${status.remote_chunks} chunks)`);
		}

		span.textContent = parts.join(' | ');
		return span;
	},

	/**
	 * Create action button.
	 * @param {string} label - Button label
	 * @param {string} className - Additional class name
	 * @param {() => Promise<void>} onClick - Click handler
	 * @returns {HTMLButtonElement} Button element
	 */
	createActionButton(label, className, onClick) {
		const button = document.createElement('button');
		button.className = `action-button ${className}`;
		button.textContent = label;
		button.addEventListener('click', async () => {
			if (this.isOperationInProgress) return;
			button.disabled = true;
			try {
				await onClick();
			} finally {
				button.disabled = false;
			}
		});
		return button;
	},

	/**
	 * Pull library from remote.
	 * @param {string} libraryId - Library ID
	 * @param {boolean} [force=false] - Force pull
	 * @returns {Promise<void>}
	 */
	async pullLibrary(libraryId, force = false) {
		if (!this.plugin || !this.syncClient) return;

		this.isOperationInProgress = true;
		this.showProgress(0, 'Pulling vectors from remote...', '');

		try {
			const result = await this.syncClient.pullLibrary(libraryId, force);

			if (result.success) {
				const sizeStr = this.syncClient.formatBytes(result.downloaded_bytes || 0);
				this.showMessage(
					`Successfully pulled library: ${result.chunks_restored} chunks (${sizeStr})`,
					'success'
				);

				// Refresh library status
				await this.refreshLibrary(libraryId);
			} else {
				this.showMessage(`Pull failed: ${result.message}`, 'error');
			}
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showMessage(`Pull failed: ${errorMessage}`, 'error');
		} finally {
			this.hideProgress();
			this.isOperationInProgress = false;
		}
	},

	/**
	 * Push library to remote.
	 * @param {string} libraryId - Library ID
	 * @param {boolean} [force=false] - Force push
	 * @returns {Promise<void>}
	 */
	async pushLibrary(libraryId, force = false) {
		if (!this.plugin || !this.syncClient) return;

		this.isOperationInProgress = true;
		this.showProgress(0, 'Pushing vectors to remote...', '');

		try {
			const result = await this.syncClient.pushLibrary(libraryId, force);

			if (result.success) {
				const sizeStr = this.syncClient.formatBytes(result.uploaded_bytes || 0);
				this.showMessage(
					`Successfully pushed library: ${result.chunks_pushed} chunks (${sizeStr})`,
					'success'
				);

				// Refresh library status
				await this.refreshLibrary(libraryId);
			} else {
				this.showMessage(`Push failed: ${result.message}`, 'error');
			}
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showMessage(`Push failed: ${errorMessage}`, 'error');
		} finally {
			this.hideProgress();
			this.isOperationInProgress = false;
		}
	},

	/**
	 * Auto-sync library (backend chooses direction).
	 * @param {string} libraryId - Library ID
	 * @returns {Promise<void>}
	 */
	async syncLibrary(libraryId) {
		if (!this.plugin || !this.syncClient) return;

		this.isOperationInProgress = true;
		this.showProgress(0, 'Syncing library...', '');

		try {
			const result = await this.syncClient.syncLibrary(libraryId, 'auto');

			if (result.success) {
				const operation = result.operation === 'pull' ? 'Pulled' : 'Pushed';
				this.showMessage(`Successfully synced library: ${operation}`, 'success');

				// Refresh library status
				await this.refreshLibrary(libraryId);
			} else {
				this.showMessage(`Sync failed: ${result.message}`, 'error');
			}
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showMessage(`Sync failed: ${errorMessage}`, 'error');
		} finally {
			this.hideProgress();
			this.isOperationInProgress = false;
		}
	},

	/**
	 * Sync all libraries.
	 * @returns {Promise<void>}
	 */
	async syncAllLibraries() {
		if (!this.plugin || !this.syncClient) return;

		this.isOperationInProgress = true;
		this.showProgress(0, 'Syncing all libraries...', '');

		try {
			const result = await this.syncClient.syncAllLibraries('auto');

			this.showMessage(
				`Synced ${result.successful} of ${result.total_libraries} libraries (${result.failed} failed)`,
				result.failed > 0 ? 'error' : 'success'
			);

			// Show individual results
			for (const libResult of result.results) {
				const status = libResult.success ? 'success' : 'error';
				this.showMessage(`  ${libResult.library_id}: ${libResult.message}`, status);
			}

			// Refresh all libraries
			await this.refreshLibraries();
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showMessage(`Sync all failed: ${errorMessage}`, 'error');
		} finally {
			this.hideProgress();
			this.isOperationInProgress = false;
		}
	},

	/**
	 * Refresh single library status.
	 * @param {string} libraryId - Library ID
	 * @returns {Promise<void>}
	 */
	async refreshLibrary(libraryId) {
		if (!this.syncClient) return;

		try {
			const status = await this.syncClient.getSyncStatus(libraryId);
			this.libraryStatuses.set(libraryId, status);
			this.updateLibraryStatus(libraryId, status);
		} catch (error) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			this.showMessage(`Failed to refresh library ${libraryId}: ${errorMessage}`, 'error');
		}
	},

	/**
	 * Refresh all libraries.
	 * @returns {Promise<void>}
	 */
	async refreshLibraries() {
		await this.loadLibraries();
	},

	/**
	 * Show progress bar.
	 * @param {number} percentage - Progress percentage (0-100)
	 * @param {string} label - Progress label
	 * @param {string} message - Progress message
	 * @returns {void}
	 */
	showProgress(percentage, label, message) {
		const progressSection = document.getElementById('progress-section');
		const progressBar = /** @type {HTMLProgressElement|null} */ (
			document.getElementById('progress-bar')
		);
		const progressLabel = document.getElementById('progress-label');
		const progressMessage = document.getElementById('progress-message');

		if (progressSection) progressSection.classList.add('active');
		if (progressBar) progressBar.value = percentage;
		if (progressLabel) progressLabel.textContent = label;
		if (progressMessage) progressMessage.textContent = message;
	},

	/**
	 * Hide progress bar.
	 * @returns {void}
	 */
	hideProgress() {
		const progressSection = document.getElementById('progress-section');
		if (progressSection) progressSection.classList.remove('active');
	},

	/**
	 * Show status message.
	 * @param {string} message - Message text
	 * @param {'info'|'success'|'error'} [type='info'] - Message type
	 * @returns {void}
	 */
	showMessage(message, type = 'info') {
		const statusSection = document.getElementById('status-section');
		if (!statusSection) return;

		// Show status section
		statusSection.style.display = '';

		// Create message element
		const messageDiv = document.createElement('div');
		messageDiv.className = `status-message ${type}`;
		messageDiv.textContent = message;

		statusSection.appendChild(messageDiv);

		// Auto-scroll to bottom
		statusSection.scrollTop = statusSection.scrollHeight;
	},

	/**
	 * Clear all status messages.
	 * @returns {void}
	 */
	clearMessages() {
		const statusSection = document.getElementById('status-section');
		if (statusSection) {
			statusSection.innerHTML = '';
			statusSection.style.display = 'none';
		}
	}
};

// Initialize when DOM is ready
if (document.readyState === 'loading') {
	document.addEventListener('DOMContentLoaded', async () => {
		await ZoteroRAGSyncDialog.init();
	});
} else {
	ZoteroRAGSyncDialog.init();
}
