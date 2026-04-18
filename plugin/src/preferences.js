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

	/**
	 * Render service API key input fields from the given list.
	 * @param {Array<{key_name: string, header_name: string, description: string, required_for: string[]}>} requiredKeys
	 */
	const renderApiKeyFields = (requiredKeys) => {
		const container = doc.getElementById('zotero-rag-service-keys-container');
		const placeholder = doc.getElementById('zotero-rag-service-keys-placeholder');
		if (!container) return;

		// Remove previously rendered dynamic rows
		container.querySelectorAll('.service-key-row, .service-key-desc').forEach(el => el.remove());

		if (!requiredKeys || requiredKeys.length === 0) {
			if (placeholder) placeholder.style.display = '';
			return;
		}
		if (placeholder) placeholder.style.display = 'none';

		for (const keyInfo of requiredKeys) {
			const prefKey = `extensions.zotero-rag.serviceApiKey.${keyInfo.key_name}`;
			const storedValue = Zotero.Prefs.get(prefKey, true) || '';

			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'setting-row service-key-row';

			const label = doc.createElementNS('http://www.w3.org/1999/xhtml', 'label');
			label.textContent = `${keyInfo.key_name}:`;
			label.setAttribute('for', `zotero-rag-key-${keyInfo.key_name}`);

			const input = /** @type {HTMLInputElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'input'));
			input.type = 'password';
			input.id = `zotero-rag-key-${keyInfo.key_name}`;
			input.className = 'setting-input';
			input.value = storedValue;
			input.placeholder = 'Enter API key';
			input.addEventListener('change', (e) => {
				Zotero.Prefs.set(prefKey, /** @type {HTMLInputElement} */ (e.target).value, true);
			});

			row.appendChild(label);
			row.appendChild(input);
			container.appendChild(row);

			if (keyInfo.description) {
				const desc = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
				desc.className = 'setting-description service-key-desc';
				desc.textContent = `${keyInfo.description} (used for: ${keyInfo.required_for.join(', ')})`;
				container.appendChild(desc);
			}
		}
	};

	// Render from cache immediately so fields appear without needing a server round-trip
	try {
		const cached = Zotero.Prefs.get('extensions.zotero-rag.requiredApiKeys', true) || '[]';
		renderApiKeyFields(JSON.parse(cached));
	} catch (_) {}

	// Refresh from server in background and re-render if the list has changed
	this.fetchRequiredApiKeys().then(() => renderApiKeyFields(this.requiredApiKeys));
};
