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

	// Library visibility section
	const populateLibraryVisibilityList = () => {
		const container = doc.getElementById('zotero-rag-library-visibility-list');
		if (!container) return;
		container.innerHTML = '';

		const libraries = this.getLibraries();
		// @ts-ignore
		const storedRaw = /** @type {string|undefined} */ (Zotero.Prefs.get('extensions.zotero-rag.visibleLibraries', true) || undefined);
		/** @type {Set<string>} */
		let checkedIds;
		try {
			checkedIds = storedRaw ? new Set(JSON.parse(storedRaw)) : new Set(libraries.map(l => l.id));
		} catch (_) {
			checkedIds = new Set(libraries.map(l => l.id));
		}

		for (const library of libraries) {
			// @ts-ignore - createLibraryCheckboxRow added at runtime
			const { checkbox, nameSpan } = this.createLibraryCheckboxRow(
				doc, library, checkedIds.has(library.id),
				/** @param {string} libId @param {boolean} checked */
				(libId, checked) => {
					// @ts-ignore
					const currentRaw = /** @type {string|undefined} */ (Zotero.Prefs.get('extensions.zotero-rag.visibleLibraries', true) || undefined);
					/** @type {Set<string>} */
					let current;
					try {
						current = currentRaw ? new Set(JSON.parse(currentRaw)) : new Set(libraries.map(l => l.id));
					} catch (_) {
						current = new Set(libraries.map(l => l.id));
					}
					if (checked) {
						current.add(libId);
					} else {
						current.delete(libId);
					}
					// Clear the pref when all are selected (default state)
					if (current.size === libraries.length) {
						// @ts-ignore
						Zotero.Prefs.clear('extensions.zotero-rag.visibleLibraries', true);
					} else {
						// @ts-ignore
						Zotero.Prefs.set('extensions.zotero-rag.visibleLibraries', JSON.stringify([...current]), true);
					}
				}
			);
			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'library-checkbox';
			row.appendChild(checkbox);
			row.appendChild(nameSpan);
			container.appendChild(row);
		}

		// Wire up the "Select all / none" checkbox
		const selectAll = /** @type {HTMLInputElement|null} */ (doc.getElementById('zotero-rag-library-select-all'));
		if (!selectAll) return;

		const updateSelectAll = () => {
			const boxes = /** @type {NodeListOf<HTMLInputElement>} */ (container.querySelectorAll('input[type="checkbox"]'));
			const checkedCount = Array.from(boxes).filter(cb => cb.checked).length;
			selectAll.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
			selectAll.checked = checkedCount === boxes.length;
		};
		updateSelectAll();

		// Keep select-all in sync when individual rows change
		container.addEventListener('change', updateSelectAll);

		selectAll.addEventListener('change', () => {
			const boxes = /** @type {NodeListOf<HTMLInputElement>} */ (container.querySelectorAll('input[type="checkbox"]'));
			boxes.forEach(cb => { cb.checked = selectAll.checked; });
			if (selectAll.checked) {
				// @ts-ignore
				Zotero.Prefs.clear('extensions.zotero-rag.visibleLibraries', true);
			} else {
				// @ts-ignore
				Zotero.Prefs.set('extensions.zotero-rag.visibleLibraries', '[]', true);
			}
		});
	};
	populateLibraryVisibilityList();

	doc.getElementById('zotero-rag-clear-cache').addEventListener('click', async () => {
		// @ts-ignore - Services is a Zotero/Firefox global
		const confirmed = Services.prompt.confirm(
			_window,
			'Clear local index cache',
			'This will delete the local cache files that track which items have been indexed.\n\n' +
			'The next indexing run will re-check all items with the backend.\n\n' +
			'Continue?'
		);
		if (!confirmed) return;

		try {
			const cacheDir = PathUtils.join(Zotero.DataDirectory.dir, 'zotero-rag');
			let deleted = 0;
			try {
				const entries = await IOUtils.getChildren(cacheDir);
				for (const entry of entries) {
					const filename = PathUtils.filename(entry);
					if (filename.startsWith('index-cache-') || filename.startsWith('pending-cache-')) {
						await IOUtils.remove(entry);
						deleted++;
					}
				}
			} catch (_) {
				// Directory may not exist yet — nothing to clear
			}
			// @ts-ignore
			Services.prompt.alert(_window, 'Cache cleared', `Removed ${deleted} cache file${deleted === 1 ? '' : 's'}.`);
		} catch (e) {
			// @ts-ignore
			Services.prompt.alert(_window, 'Error', `Failed to clear cache: ${e}`);
		}
	});
};
