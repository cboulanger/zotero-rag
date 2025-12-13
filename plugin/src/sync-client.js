// @ts-check

/**
 * @file Sync API client for Zotero RAG plugin
 * Provides interface to backend vector synchronization endpoints
 */

/**
 * @typedef {Object} SyncResponse
 * @property {boolean} success - Whether operation succeeded
 * @property {string} message - Status message
 * @property {string} operation - Operation performed (pull/push/sync)
 * @property {string} library_id - Library ID
 * @property {number} [downloaded_bytes] - Bytes downloaded (pull)
 * @property {number} [uploaded_bytes] - Bytes uploaded (push)
 * @property {number} [chunks_restored] - Chunks restored (pull)
 * @property {number} [chunks_pushed] - Chunks pushed (push)
 * @property {number} [library_version] - Library version
 * @property {number} [restore_time] - Time to restore (pull)
 * @property {number} [snapshot_time] - Time to snapshot (push)
 */

/**
 * @typedef {Object} SyncStatus
 * @property {string} library_id - Library ID
 * @property {boolean} local_exists - Whether local vectors exist
 * @property {boolean} remote_exists - Whether remote vectors exist
 * @property {number|null} local_version - Local library version
 * @property {number|null} remote_version - Remote library version
 * @property {string} sync_status - Status (local_newer/remote_newer/same/diverged)
 * @property {number|null} local_chunks - Number of local chunks
 * @property {number|null} remote_chunks - Number of remote chunks
 * @property {boolean} needs_pull - Whether pull is recommended
 * @property {boolean} needs_push - Whether push is recommended
 */

/**
 * @typedef {Object} RemoteLibrary
 * @property {string} library_id - Library ID
 * @property {number} library_version - Library version
 * @property {string} snapshot_file - Snapshot filename
 * @property {string} uploaded_at - Upload timestamp (ISO)
 * @property {number} total_chunks - Total chunks
 * @property {number} total_items - Total items
 */

/**
 * @typedef {Object} SyncConfig
 * @property {boolean} enabled - Whether sync is enabled
 * @property {string} backend - Backend type (webdav/s3)
 * @property {boolean} auto_pull - Auto-pull on startup
 * @property {boolean} auto_push - Auto-push after indexing
 */

/**
 * Client for backend vector sync API.
 */
class ZoteroRAGSyncClient {
	/**
	 * @param {string} backendURL - Backend server URL
	 */
	constructor(backendURL) {
		/** @type {string} */
		this.backendURL = backendURL;
	}

	/**
	 * Check if sync is enabled on backend.
	 * @returns {Promise<SyncConfig>}
	 * @throws {Error} If request fails
	 */
	async checkSyncEnabled() {
		const response = await fetch(`${this.backendURL}/api/vectors/sync/enabled`);
		if (!response.ok) {
			throw new Error(`HTTP ${response.status}: ${response.statusText}`);
		}
		return await response.json();
	}

	/**
	 * Get sync status for a library.
	 * @param {string} libraryId - Library ID
	 * @returns {Promise<SyncStatus>}
	 * @throws {Error} If request fails or sync not enabled
	 */
	async getSyncStatus(libraryId) {
		const response = await fetch(`${this.backendURL}/api/vectors/${libraryId}/sync-status`);
		if (response.status === 400) {
			throw new Error('Sync not enabled on backend');
		}
		if (!response.ok) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || `HTTP ${response.status}`);
		}
		return await response.json();
	}

	/**
	 * List all remote libraries.
	 * @returns {Promise<Array<RemoteLibrary>>}
	 * @throws {Error} If request fails or sync not enabled
	 */
	async listRemoteLibraries() {
		const response = await fetch(`${this.backendURL}/api/vectors/remote`);
		if (response.status === 400) {
			throw new Error('Sync not enabled on backend');
		}
		if (!response.ok) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || `HTTP ${response.status}`);
		}
		const data = await response.json();
		return data.libraries || [];
	}

	/**
	 * Pull library vectors from remote storage.
	 * @param {string} libraryId - Library ID
	 * @param {boolean} [force=false] - Force pull even if not needed
	 * @returns {Promise<SyncResponse>}
	 * @throws {Error} If request fails
	 */
	async pullLibrary(libraryId, force = false) {
		const url = `${this.backendURL}/api/vectors/${libraryId}/pull?force=${force}`;
		const response = await fetch(url, { method: 'POST' });

		if (response.status === 400) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || 'Sync not enabled on backend');
		}
		if (!response.ok) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || `HTTP ${response.status}`);
		}
		return await response.json();
	}

	/**
	 * Push library vectors to remote storage.
	 * @param {string} libraryId - Library ID
	 * @param {boolean} [force=false] - Force push even if not needed
	 * @returns {Promise<SyncResponse>}
	 * @throws {Error} If request fails
	 */
	async pushLibrary(libraryId, force = false) {
		const url = `${this.backendURL}/api/vectors/${libraryId}/push?force=${force}`;
		const response = await fetch(url, { method: 'POST' });

		if (response.status === 400) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || 'Sync not enabled on backend');
		}
		if (!response.ok) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || `HTTP ${response.status}`);
		}
		return await response.json();
	}

	/**
	 * Auto-sync library (backend chooses pull or push).
	 * @param {string} libraryId - Library ID
	 * @param {'auto'|'pull'|'push'} [direction='auto'] - Sync direction
	 * @returns {Promise<SyncResponse>}
	 * @throws {Error} If request fails or conflict detected
	 */
	async syncLibrary(libraryId, direction = 'auto') {
		const url = `${this.backendURL}/api/vectors/${libraryId}/sync?direction=${direction}`;
		const response = await fetch(url, { method: 'POST' });

		if (response.status === 400) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || 'Sync not enabled on backend');
		}
		if (response.status === 409) {
			// Conflict - libraries have diverged
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || 'Sync conflict: libraries have diverged');
		}
		if (!response.ok) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || `HTTP ${response.status}`);
		}
		return await response.json();
	}

	/**
	 * Sync all indexed libraries.
	 * @param {'auto'|'pull'|'push'} [direction='auto'] - Sync direction
	 * @returns {Promise<{total_libraries: number, successful: number, failed: number, results: Array<SyncResponse>}>}
	 * @throws {Error} If request fails
	 */
	async syncAllLibraries(direction = 'auto') {
		const url = `${this.backendURL}/api/vectors/sync-all?direction=${direction}`;
		const response = await fetch(url, { method: 'POST' });

		if (response.status === 400) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || 'Sync not enabled on backend');
		}
		if (!response.ok) {
			const errorData = await response.json().catch(() => ({}));
			throw new Error(errorData.detail || `HTTP ${response.status}`);
		}
		return await response.json();
	}

	/**
	 * Format bytes as human-readable string.
	 * @param {number} bytes - Bytes
	 * @returns {string} Formatted string (e.g., "1.5 MB")
	 */
	formatBytes(bytes) {
		if (bytes === 0) return '0 B';
		const k = 1024;
		const sizes = ['B', 'KB', 'MB', 'GB'];
		const i = Math.floor(Math.log(bytes) / Math.log(k));
		return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
	}

	/**
	 * Format sync status as human-readable string.
	 * @param {string} status - Sync status (local_newer/remote_newer/same/diverged)
	 * @returns {string} Human-readable status
	 */
	formatSyncStatus(status) {
		const statusMap = {
			'local_newer': 'Local ahead',
			'remote_newer': 'Remote ahead',
			'same': 'In sync',
			'diverged': 'Conflict'
		};
		return statusMap[status] || status;
	}

	/**
	 * Get sync status icon character.
	 * @param {SyncStatus} status - Sync status object
	 * @returns {string} Icon character
	 */
	getSyncStatusIcon(status) {
		if (!status.local_exists && !status.remote_exists) {
			return '\u2205'; // Empty set - not synced anywhere
		}
		if (status.sync_status === 'same') {
			return '\u2713'; // Checkmark - in sync
		}
		if (status.sync_status === 'local_newer') {
			return '\u2191'; // Up arrow - needs push
		}
		if (status.sync_status === 'remote_newer') {
			return '\u2193'; // Down arrow - needs pull
		}
		if (status.sync_status === 'diverged') {
			return '\u26A0'; // Warning - conflict
		}
		return '\u003F'; // Question mark - unknown
	}

	/**
	 * Get sync status icon color.
	 * @param {SyncStatus} status - Sync status object
	 * @returns {string} CSS color
	 */
	getSyncStatusColor(status) {
		if (!status.local_exists && !status.remote_exists) {
			return '#999'; // Gray - not synced
		}
		if (status.sync_status === 'same') {
			return '#008000'; // Green - in sync
		}
		if (status.sync_status === 'local_newer' || status.sync_status === 'remote_newer') {
			return '#0066cc'; // Blue - needs sync
		}
		if (status.sync_status === 'diverged') {
			return '#cc6600'; // Orange - warning
		}
		return '#999'; // Gray - unknown
	}
}
