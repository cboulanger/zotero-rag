// Preferences pane logic — called via onload in preferences.xhtml

/**
 * Initialize the preferences pane. Called by the XUL onload attribute.
 * ZoteroRAG is already loaded in the global scope by bootstrap.js.
 * @param {Window} _window
 */
ZoteroRAGPlugin.prototype.initPrefPane = function(_window) {
	const doc = _window.document;

	const backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || '';
	const zoteroApiKey = Zotero.Prefs.get('extensions.zotero-rag.zoteroApiKey', true) || '';
	const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

	// Show stored value; leave blank so the placeholder shows when nothing is saved
	doc.getElementById('zotero-rag-backend-url').value = backendURL;
	doc.getElementById('zotero-rag-zotero-api-key').value = zoteroApiKey;
	doc.getElementById('zotero-rag-max-queries').value = maxQueries;

	const zoteroApiKeyStatus = doc.getElementById('zotero-rag-zotero-api-key-status');

	/**
	 * Validate the currently-configured Zotero API key against the backend
	 * and show the result in the status line below the field. Also refreshes
	 * the auto-indexing checkbox, since its availability depends on this key.
	 * @returns {Promise<void>}
	 */
	const refreshZoteroIdentityStatus = async () => {
		if (!zoteroApiKeyStatus) return;
		if (this.isLoopbackBackend()) {
			zoteroApiKeyStatus.textContent = 'Not required for a local server.';
			zoteroApiKeyStatus.className = 'setting-description';
			await refreshAutoindexToggle();
			return;
		}
		if (!this.zoteroApiKey) {
			zoteroApiKeyStatus.textContent = '';
			zoteroApiKeyStatus.className = 'setting-description';
			await refreshAutoindexToggle();
			return;
		}
		try {
			const result = await this.checkZoteroIdentity(this.zoteroApiKey);
			if (result.loopback) {
				zoteroApiKeyStatus.textContent = '✓ Key accepted (this server does not require Zotero-key authentication).';
			} else {
				const count = Array.isArray(result.targets) ? result.targets.length : 0;
				zoteroApiKeyStatus.textContent = `✓ Authenticated as ${result.username} — ${count} librar${count === 1 ? 'y' : 'ies'} accessible.`;
			}
			zoteroApiKeyStatus.className = 'setting-description status-ok';
		} catch (e) {
			zoteroApiKeyStatus.textContent = `✗ ${e instanceof Error ? e.message : String(e)}`;
			zoteroApiKeyStatus.className = 'setting-description status-error';
		}
		await refreshAutoindexToggle();
	};

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
		refreshZoteroIdentityStatus();
	});

	doc.getElementById('zotero-rag-zotero-api-key').addEventListener('change', (e) => {
		const key = /** @type {HTMLInputElement} */ (e.target).value;
		Zotero.Prefs.set('extensions.zotero-rag.zoteroApiKey', key, true);
		this.zoteroApiKey = key;
		refreshZoteroIdentityStatus();
	});

	doc.getElementById('zotero-rag-run-wizard').addEventListener('click', () => {
		this.openSetupWizard(_window);
	});

	doc.getElementById('zotero-rag-max-queries').addEventListener('change', (e) => {
		const value = parseInt(/** @type {HTMLInputElement} */ (e.target).value);
		if (value >= 1 && value <= 10) {
			Zotero.Prefs.set('extensions.zotero-rag.maxQueries', value, true);
			this.maxConcurrentQueries = value;
		}
	});

	// External links inside the preferences pane don't open in the system browser
	// on their own (target="_blank" is a no-op here). Route http(s) links through
	// Zotero.launchURL so they open in the user's default browser.
	doc.getElementById('zotero-rag-prefs-container').addEventListener('click', (e) => {
		const anchor = /** @type {Element} */ (e.target)?.closest?.('a[href]');
		if (!anchor) return;
		const href = anchor.getAttribute('href');
		if (href && /^https?:\/\//i.test(href)) {
			e.preventDefault();
			Zotero.launchURL(href);
		}
	});

	const serviceKeysContainer = doc.getElementById('zotero-rag-service-keys-container');
	const serviceKeysPlaceholder = doc.getElementById('zotero-rag-service-keys-placeholder');

	/**
	 * Show a per-field validation status message directly under a service API
	 * key's own input field — in addition to the broader Automatic Indexing
	 * status banner — so a rejected/unverified key is visible right where the
	 * user would look to fix it, not only in a separate section.
	 * @param {string} keyName
	 * @param {string|undefined} status - 'ok' | 'invalid' | 'unverified' | undefined
	 * @param {string} [errorMessage]
	 * @returns {void}
	 */
	const setServiceKeyStatus = (keyName, status, errorMessage) => {
		const el = doc.getElementById(`zotero-rag-key-status-${keyName}`);
		if (!el) return;
		if (status === 'invalid') {
			el.textContent = `✗ Rejected: ${errorMessage || 'invalid credentials'}`;
			el.className = 'setting-description service-key-status status-error';
		} else if (status === 'unverified') {
			el.textContent = '⚠ Could not be verified right now; will be retried automatically.';
			el.className = 'setting-description service-key-status status-warn';
		} else if (status === 'ok') {
			el.textContent = '✓ Key accepted.';
			el.className = 'setting-description service-key-status status-ok';
		} else {
			el.textContent = '';
			el.className = 'setting-description service-key-status';
		}
	};

	/**
	 * Re-sync the server-stored embedding key when the user edits it locally,
	 * so the cron auto-indexer's copy stays in sync without needing to toggle
	 * auto-indexing off and on again. autoindexToggle/setAutoindexStatus are
	 * declared further down in this same function but are safe to reference
	 * here since this callback only runs later, after user interaction.
	 * @param {{key_name: string, header_name: string, description: string, docs_url?: string|null, required_for: string[]}} keyInfo
	 * @param {string} value
	 * @returns {Promise<void>}
	 */
	const onServiceKeyChange = async (keyInfo, value) => {
		if (!keyInfo.required_for.includes('indexing')) return;
		if (!autoindexToggle || !autoindexToggle.checked) return;
		setAutoindexStatus('Updating embedding API key...', 'ok');
		try {
			const response = await fetch(`${this.backendURL}/api/autoindex/keys`, {
				method: 'POST',
				headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify({ api_key: this.zoteroApiKey, embedding_api_key: value }),
			});
			if (!response.ok) {
				const err = await response.json().catch(() => ({}));
				setAutoindexStatus(`Error updating embedding API key: ${err.detail || response.status}`, 'error');
				return;
			}
			/** @type {{embedding_key_status?: string, embedding_key_error?: string}} */
			const data = await response.json();
			setServiceKeyStatus(keyInfo.key_name, data.embedding_key_status, data.embedding_key_error);
			if (data.embedding_key_status === 'invalid') {
				setAutoindexStatus(`Embedding API key rejected: ${data.embedding_key_error || 'invalid credentials'}.`, 'warn');
			} else if (data.embedding_key_status === 'unverified') {
				setAutoindexStatus('Embedding API key could not be verified right now but was saved; it will be retried on the next run.', 'warn');
			} else if (!data.embedding_key_status) {
				setAutoindexStatus('Embedding key field cleared; nothing was synced to the server.', 'warn');
			} else if (data.embedding_key_status === 'ok') {
				setAutoindexStatus('Embedding API key updated.', 'ok');
			} else {
				setAutoindexStatus(`Embedding API key updated, but returned an unexpected status "${data.embedding_key_status}".`, 'warn');
			}
		} catch (e) {
			setAutoindexStatus(`Error updating embedding API key: ${e}`, 'error');
		}
	};

	// Render from cache immediately so fields appear without needing a server round-trip
	try {
		const cached = Zotero.Prefs.get('extensions.zotero-rag.requiredApiKeys', true) || '[]';
		this.renderServiceApiKeyFields(doc, serviceKeysContainer, serviceKeysPlaceholder, JSON.parse(cached), onServiceKeyChange);
	} catch (_) {}

	// Refresh from server in background and re-render if the list has changed
	this.fetchRequiredApiKeys().then(() =>
		this.renderServiceApiKeyFields(doc, serviceKeysContainer, serviceKeysPlaceholder, this.requiredApiKeys, onServiceKeyChange)
	);

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

		// Mark libraries that are auto-indexed on the server with a clock icon
		// (fire-and-forget — fetches the server registry, adds icons when it resolves)
		this.decorateAutoIndexedLibraries(doc, container);

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

	// Automatic indexing section: a single on/off toggle reusing the same
	// Zotero API key already configured above (no separate key entry).
	const autoindexToggle = /** @type {HTMLInputElement|null} */ (doc.getElementById('zotero-rag-autoindex-toggle'));
	const autoindexStatus = doc.getElementById('zotero-rag-autoindex-status');

	/**
	 * @param {string} message
	 * @param {'ok'|'warn'|'error'} [level='ok']
	 * @returns {void}
	 */
	const setAutoindexStatus = (message, level = 'ok') => {
		if (!autoindexStatus) return;
		autoindexStatus.textContent = message;
		autoindexStatus.className = `setting-description status-${level}`;
	};

	/**
	 * Reflect current auto-indexing state in the checkbox: checked if a key
	 * matching the caller's identity is already registered; disabled if no
	 * Zotero API key is configured yet (nothing to submit).
	 * @returns {Promise<void>}
	 */
	const refreshAutoindexToggle = async () => {
		if (!autoindexToggle) return;
		// Auto-indexing always needs a real Zotero key (it drives a cron job that
		// hits api.zotero.org), even when the backend connection itself is loopback
		// and needs no key for plugin auth — so this check is unconditional.
		if (!this.zoteroApiKey) {
			autoindexToggle.checked = false;
			autoindexToggle.disabled = true;
			setAutoindexStatus('Configure your Zotero API key above first.', 'warn');
			return;
		}
		try {
			const response = await fetch(`${this.backendURL}/api/autoindex/keys`, {
				headers: this.getAuthHeaders(),
			});
			if (!response.ok) {
				autoindexToggle.disabled = true;
				const err = await response.json().catch(() => ({}));
				setAutoindexStatus(err.detail || 'Auto-indexing is not available on this server.', 'error');
				return;
			}
			/** @type {{keys: Array<{has_embedding_key?: boolean, embedding_key_status?: string}>}} */
			const data = await response.json();
			autoindexToggle.disabled = false;
			autoindexToggle.checked = Array.isArray(data.keys) && data.keys.length > 0;
			if (!autoindexToggle.checked) {
				setAutoindexStatus('', 'ok');
			} else {
				const own = data.keys[0];
				const embeddingKeyInfo = this.requiredApiKeys.find(k => k.required_for.includes('indexing'));
				if (embeddingKeyInfo) {
					setServiceKeyStatus(embeddingKeyInfo.key_name, own.embedding_key_status);
				}
				if (!own.has_embedding_key) {
					setAutoindexStatus('Automatic indexing is enabled, but no embedding API key is configured — indexing will be skipped until you add one above.', 'warn');
				} else if (own.embedding_key_status === 'invalid') {
					setAutoindexStatus('Automatic indexing is enabled, but your embedding API key was rejected — indexing will be skipped until you add a valid key above.', 'warn');
				} else if (own.embedding_key_status === 'unverified') {
					setAutoindexStatus('Automatic indexing is enabled. Your embedding API key could not be verified yet; it will be retried on the next run.', 'warn');
				} else if (own.embedding_key_status === 'ok') {
					setAutoindexStatus('Automatic indexing is enabled.', 'ok');
				} else {
					setAutoindexStatus(`Automatic indexing is enabled, but your embedding key has an unexpected status "${own.embedding_key_status}".`, 'warn');
				}
			}
		} catch (e) {
			autoindexToggle.disabled = true;
			setAutoindexStatus(`Error: ${e}`, 'error');
		}
	};

	if (autoindexToggle) {
		autoindexToggle.addEventListener('change', async () => {
			const enabling = autoindexToggle.checked;
			const requestURL = `${this.backendURL}/api/autoindex/keys`;
			const embeddingKeyInfo = this.requiredApiKeys.find(k => k.required_for.includes('indexing'));
			setAutoindexStatus(enabling ? 'Enabling auto-indexing...' : 'Disabling auto-indexing...');
			try {
				/** @type {{api_key: string, embedding_api_key?: string}} */
				const body = { api_key: this.zoteroApiKey };
				if (enabling && embeddingKeyInfo) {
					const embeddingKeyValue = Zotero.Prefs.get(`extensions.zotero-rag.serviceApiKey.${embeddingKeyInfo.key_name}`, true) || '';
					if (embeddingKeyValue) body.embedding_api_key = embeddingKeyValue;
				}
				const response = await fetch(requestURL, {
					method: enabling ? 'POST' : 'DELETE',
					headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify(body),
				});
				if (!response.ok) {
					const err = await response.json().catch(() => ({}));
					setAutoindexStatus(`Error: ${err.detail || response.status}`, 'error');
					autoindexToggle.checked = !enabling;
					return;
				}
				if (enabling) {
					/** @type {{targets: string[], embedding_key_status?: string, embedding_key_error?: string}} */
					const data = await response.json();
					const count = Array.isArray(data.targets) ? data.targets.length : 0;
					const libraryText = count === 1 ? 'Auto-indexing enabled for 1 library.' : `Auto-indexing enabled for ${count} libraries.`;
					if (embeddingKeyInfo) {
						setServiceKeyStatus(embeddingKeyInfo.key_name, data.embedding_key_status, data.embedding_key_error);
					}
					// Fail closed: only 'ok' (or an absent status, e.g. no key was submitted)
					// gets plain success messaging. Any other truthy value — known
					// (invalid/unverified) or a status this plugin doesn't recognize yet —
					// must surface a warning rather than silently imply success.
					if (data.embedding_key_status === 'invalid') {
						setAutoindexStatus(`${libraryText} Warning: your embedding API key was rejected (${data.embedding_key_error || 'invalid credentials'}) — indexing will be skipped until you add a valid key.`, 'warn');
					} else if (data.embedding_key_status === 'unverified') {
						setAutoindexStatus(`${libraryText} Your embedding API key could not be verified right now but was saved; it will be retried on the next run.`, 'warn');
					} else if (!data.embedding_key_status) {
						setAutoindexStatus(`${libraryText} Warning: no embedding API key configured above — indexing will be skipped until you add one.`, 'warn');
					} else if (data.embedding_key_status === 'ok') {
						setAutoindexStatus(libraryText, 'ok');
					} else {
						setAutoindexStatus(`${libraryText} Warning: unexpected embedding key status "${data.embedding_key_status}" — check your embedding API key configuration.`, 'warn');
					}
				} else {
					setAutoindexStatus('Auto-indexing disabled.', 'ok');
				}
				this.invalidateAutoIndexedLibraryIds();
				populateLibraryVisibilityList();
			} catch (e) {
				setAutoindexStatus(`Error: ${e}`, 'error');
				autoindexToggle.checked = !enabling;
			}
		});
	}

	const autoindexMonitorButton = doc.getElementById('zotero-rag-autoindex-monitor');
	if (autoindexMonitorButton) {
		autoindexMonitorButton.addEventListener('click', () => {
			this.openAutoindexStatusDialog(_window);
		});
	}

	// Initial population, now that both closures above exist
	refreshZoteroIdentityStatus();
};
