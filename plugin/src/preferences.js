// Preferences script for Zotero RAG plugin

var ZoteroRAGPreferences = {
	init() {
		// Load current preferences
		this.loadPreferences();

		// Add event listeners
		const urlInput = document.getElementById('zotero-rag-backend-url');
		urlInput.addEventListener('change', (e) => {
			this.saveBackendURL(/** @type {HTMLInputElement} */ (e.target).value);
		});
		// Show/hide API key row depending on whether the URL is remote
		urlInput.addEventListener('input', (e) => {
			this.updateApiKeyVisibility(/** @type {HTMLInputElement} */ (e.target).value);
		});

		document.getElementById('zotero-rag-api-key').addEventListener('change', (e) => {
			this.saveApiKey(/** @type {HTMLInputElement} */ (e.target).value);
		});

		document.getElementById('zotero-rag-max-queries').addEventListener('change', (e) => {
			this.saveMaxQueries(parseInt(/** @type {HTMLInputElement} */ (e.target).value));
		});
	},

	loadPreferences() {
		const backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || 'http://localhost:8119';
		const apiKey = Zotero.Prefs.get('extensions.zotero-rag.apiKey', true) || '';
		const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

		document.getElementById('zotero-rag-backend-url').value = backendURL;
		document.getElementById('zotero-rag-api-key').value = apiKey;
		document.getElementById('zotero-rag-max-queries').value = maxQueries;

		this.updateApiKeyVisibility(backendURL);
	},

	/**
	 * Show the API key row when the backend URL is remote (not localhost / 127.0.0.1).
	 * @param {string} url
	 */
	updateApiKeyVisibility(url) {
		const isLocal = url.includes('localhost') || url.includes('127.0.0.1');
		const rowStyle = isLocal ? 'none' : '';
		const row = document.getElementById('zotero-rag-api-key-row');
		const desc = document.getElementById('zotero-rag-api-key-desc');
		if (row) row.style.display = isLocal ? 'none' : '';
		if (desc) desc.style.display = isLocal ? 'none' : '';
	},

	saveBackendURL(url) {
		// Validate URL format
		try {
			new URL(url);
			Zotero.Prefs.set('extensions.zotero-rag.backendURL', url, true);

			// Update the plugin's backend URL
			if (typeof ZoteroRAG !== 'undefined') {
				ZoteroRAG.backendURL = url;
			}

			this.updateApiKeyVisibility(url);
		} catch (e) {
			Zotero.debug(`Invalid URL: ${url}`);
		}
	},

	saveApiKey(key) {
		Zotero.Prefs.set('extensions.zotero-rag.apiKey', key, true);
		if (typeof ZoteroRAG !== 'undefined') {
			ZoteroRAG.apiKey = key;
		}
	},

	saveMaxQueries(value) {
		if (value >= 1 && value <= 10) {
			Zotero.Prefs.set('extensions.zotero-rag.maxQueries', value, true);

			// Update the plugin's max queries
			if (typeof ZoteroRAG !== 'undefined') {
				ZoteroRAG.maxConcurrentQueries = value;
			}
		}
	}
};

// Initialize when preferences pane loads
document.addEventListener('DOMContentLoaded', () => {
	ZoteroRAGPreferences.init();
});
