// Preferences script for Zotero RAG plugin

var ZoteroRAGPreferences = {
	init() {
		// Load current preferences
		this.loadPreferences();

		// Add event listeners
		document.getElementById('zotero-rag-backend-url').addEventListener('change', (e) => {
			this.saveBackendURL(e.target.value);
		});

		document.getElementById('zotero-rag-max-queries').addEventListener('change', (e) => {
			this.saveMaxQueries(parseInt(e.target.value));
		});
	},

	loadPreferences() {
		const backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || 'http://localhost:8119';
		const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

		document.getElementById('zotero-rag-backend-url').value = backendURL;
		document.getElementById('zotero-rag-max-queries').value = maxQueries;
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
		} catch (e) {
			Zotero.debug(`Invalid URL: ${url}`);
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
