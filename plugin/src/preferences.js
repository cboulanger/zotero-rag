// Preferences pane logic — called via onload in preferences.xhtml

/**
 * Initialize the preferences pane. Called by the XUL onload attribute.
 * ZoteroRAG is already loaded in the global scope by bootstrap.js.
 * @param {Window} _window
 */
ZoteroRAGPlugin.prototype.initPrefPane = function(_window) {
	const doc = _window.document;

	const backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || '';
	const apiKey = Zotero.Prefs.get('extensions.zotero-rag.apiKey', true) || '';
	const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

	// Show stored value; leave blank so the placeholder shows when nothing is saved
	doc.getElementById('zotero-rag-backend-url').value = backendURL;
	doc.getElementById('zotero-rag-api-key').value = apiKey;
	doc.getElementById('zotero-rag-max-queries').value = maxQueries;

	doc.getElementById('zotero-rag-backend-url').addEventListener('change', (e) => {
		const value = /** @type {HTMLInputElement} */ (e.target).value.trim();
		if (value === '') {
			// Clearing the field resets to the default — remove the stored pref
			Zotero.Prefs.clear('extensions.zotero-rag.backendURL', true);
			this.backendURL = 'http://localhost:8119';
		} else {
			try {
				new URL(value);
				Zotero.Prefs.set('extensions.zotero-rag.backendURL', value, true);
				this.backendURL = value;
			} catch (_) {
				Zotero.debug('Zotero RAG: Invalid URL: ' + value);
			}
		}
	});

	doc.getElementById('zotero-rag-api-key').addEventListener('change', (e) => {
		const key = /** @type {HTMLInputElement} */ (e.target).value;
		Zotero.Prefs.set('extensions.zotero-rag.apiKey', key, true);
		this.apiKey = key;
	});

	doc.getElementById('zotero-rag-max-queries').addEventListener('change', (e) => {
		const value = parseInt(/** @type {HTMLInputElement} */ (e.target).value);
		if (value >= 1 && value <= 10) {
			Zotero.Prefs.set('extensions.zotero-rag.maxQueries', value, true);
			this.maxConcurrentQueries = value;
		}
	});
};
