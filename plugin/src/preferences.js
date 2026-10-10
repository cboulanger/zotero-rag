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
	 * Render `el`'s content as a bold, colored icon (✓/✗/⚠) followed by a text
	 * message. A plain "✓ "/"✗ " text prefix at 12px is easy to miss, so the
	 * icon itself is rendered larger/bolder via a dedicated .status-icon span
	 * instead of relying on the character alone for visibility.
	 * @param {HTMLElement|null} el
	 * @param {string|null} symbol - icon character, or null/empty to clear with no message
	 * @param {string} iconClass - e.g. 'status-icon-ok'
	 * @param {string} text
	 * @param {string} className - full className to apply to el (caller controls all classes)
	 * @returns {void}
	 */
	const setIconStatus = (el, symbol, iconClass, text, className) => {
		if (!el) return;
		el.textContent = '';
		if (symbol) {
			const icon = doc.createElementNS('http://www.w3.org/1999/xhtml', 'span');
			icon.className = `status-icon ${iconClass}`;
			icon.textContent = symbol;
			el.appendChild(icon);
			el.appendChild(doc.createTextNode(` ${text}`));
		} else if (text) {
			el.textContent = text;
		}
		el.className = className;
	};

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
			const text = result.loopback
				? 'Key accepted (this server does not require Zotero-key authentication).'
				: (() => {
					const count = Array.isArray(result.targets) ? result.targets.length : 0;
					return `Authenticated as ${result.username} — ${count} librar${count === 1 ? 'y' : 'ies'} accessible.`;
				})();
			setIconStatus(zoteroApiKeyStatus, '✓', 'status-icon-ok', text, 'setting-description status-ok');
		} catch (e) {
			setIconStatus(zoteroApiKeyStatus, '✗', 'status-icon-error',
				e instanceof Error ? e.message : String(e), 'setting-description status-error');
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

	// Retrieval Tuning section — one compact row per DIVERSITY_TUNING_FIELDS entry
	// (defined in zotero-rag.js, shared with getDiversityTuningPayload()).
	const diversityContainer = doc.getElementById('zotero-rag-diversity-tuning-container');
	if (diversityContainer) {
		for (const field of DIVERSITY_TUNING_FIELDS) {
			const prefKey = `extensions.zotero-rag.${field.prefKey}`;
			const inputId = `zotero-rag-diversity-${field.prefKey}`;

			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'diversity-tuning-row';

			const label = doc.createElementNS('http://www.w3.org/1999/xhtml', 'label');
			label.setAttribute('for', inputId);
			label.textContent = `${field.label}:`;

			const input = /** @type {HTMLInputElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'input'));
			input.id = inputId;
			input.type = 'number';
			input.min = '1';
			input.className = 'setting-input-small';
			input.placeholder = String(field.default);
			const stored = Zotero.Prefs.get(prefKey, true);
			input.value = stored !== undefined && stored !== null && stored !== '' ? String(stored) : '';

			const description = doc.createElementNS('http://www.w3.org/1999/xhtml', 'span');
			description.className = 'diversity-tuning-description';
			description.textContent = field.description;

			input.addEventListener('change', () => {
				const raw = input.value.trim();
				if (raw === '') {
					Zotero.Prefs.clear(prefKey, true);
					return;
				}
				const value = parseInt(raw, 10);
				if (Number.isFinite(value) && value >= 1) {
					Zotero.Prefs.set(prefKey, value, true);
				} else {
					// Invalid entry — revert the field rather than store garbage
					input.value = '';
					Zotero.Prefs.clear(prefKey, true);
				}
			});

			row.appendChild(label);
			row.appendChild(input);
			row.appendChild(description);
			diversityContainer.appendChild(row);
		}
	}

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
			setIconStatus(el, '✗', 'status-icon-error', `Rejected: ${errorMessage || 'invalid credentials'}`,
				'service-key-status status-error');
		} else if (status === 'unverified') {
			setIconStatus(el, '⚠', 'status-icon-warn', 'Could not be verified right now; will be retried automatically.',
				'service-key-status status-warn');
		} else if (status === 'ok') {
			setIconStatus(el, '✓', 'status-icon-ok', 'Key accepted.',
				'service-key-status status-ok');
		} else {
			setIconStatus(el, null, '', '', 'service-key-status');
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

	// --- Per-side model sections -------------------------------------------------
	// Everything vendor-specific comes from the backend's provider descriptors
	// (GET /api/config/providers); see provider-sections.js for the rendering.
	const Sections = ZoteroRAGProviderSections;
	const sectionsContainer = doc.getElementById('zotero-rag-provider-sections');
	const sectionRefs = sectionsContainer
		? Sections.ensureSections(doc, sectionsContainer, {
			onProvision: (side, key) => controller && controller.provision(side, key),
			onRetry: (side, key) => controller && controller.provision(side, key),
			onPause: (side, key) => controller && controller.pause(side, key),
		})
		: null;
	/**
	 * GET a backend JSON endpoint; null on any failure (the UI then keeps what it has).
	 * @param {string} path
	 * @returns {Promise<any>}
	 */
	const fetchJson = async (path) => {
		try {
			const response = await fetch(`${this.backendURL}${path}`, { headers: this.getAuthHeaders() });
			return response.ok ? await response.json() : null;
		} catch (e) {
			this.log(`Could not fetch ${path}: ${e}`);
			return null;
		}
	};
	/** @type {ReturnType<typeof ZoteroRAGProviderSections.createController> | null} */
	const controller = sectionRefs
		? Sections.createController({
			refs: sectionRefs,
			get: fetchJson,
			post: async (path, body) => {
				const response = await fetch(`${this.backendURL}${path}`, {
					method: 'POST',
					headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify(body),
				});
				return { ok: response.ok, status: response.status, data: await response.json().catch(() => ({})) };
			},
		})
		: null;
	const pollJob = async () => { if (controller) await controller.poll(); };
	const headerForKey = Sections.headerForKey;

	/** Render each required key under the first side that uses it. */
	const renderSideKeys = () => {
		if (!sectionRefs) return;
		const keys = this.requiredApiKeys || [];
		for (const side of Sections.SIDES) {
			const forSide = keys.filter((k) => {
				const sides = k.sides && k.sides.length ? k.sides : ['embedding'];
				return Sections.SIDES.find((s) => sides.includes(s)) === side;
			});
			this.renderServiceApiKeyFields(doc, sectionRefs[side].keys, null, forSide, onServiceKeyChange);
		}
		if (serviceKeysPlaceholder) serviceKeysPlaceholder.style.display = keys.length ? 'none' : '';
	};

	// Render from cache immediately so fields appear without needing a server round-trip
	try {
		this.requiredApiKeys = JSON.parse(Zotero.Prefs.get('extensions.zotero-rag.requiredApiKeys', true) || '[]');
	} catch (_) {}
	renderSideKeys();
	// Refresh from server in background and re-render if the list has changed
	this.fetchRequiredApiKeys().then(renderSideKeys);

	// --- Model preset group -------------------------------------------------------
	const presetSelect = /** @type {HTMLSelectElement | null} */ (doc.getElementById('zotero-rag-preset-select'));
	const presetLabel = doc.getElementById('zotero-rag-preset-label');
	const presetDescription = doc.getElementById('zotero-rag-preset-description');
	const presetStatus = doc.getElementById('zotero-rag-preset-status');
	const presetKeys = doc.getElementById('zotero-rag-preset-keys');
	const defaultRow = doc.getElementById('zotero-rag-default-row');
	const defaultHelp = doc.getElementById('zotero-rag-default-help');
	const defaultSelect = /** @type {HTMLSelectElement | null} */ (doc.getElementById('zotero-rag-default-select'));
	/** What the preset controls currently show: 'loopback' (one control, changes the default) or 'user'. */
	let presetMode = 'user';
	const setPresetStatus = (/** @type {string} */ text) => { if (presetStatus) presetStatus.textContent = text; };

	/**
	 * @param {HTMLSelectElement} select
	 * @param {Array<{value: string, text: string}>} options
	 * @param {string} selected
	 */
	const fillSelect = (select, options, selected) => {
		select.innerHTML = '';
		for (const o of options) {
			const option = doc.createElementNS('http://www.w3.org/1999/xhtml', 'option');
			option.value = o.value;
			option.textContent = o.text;
			select.appendChild(option);
		}
		select.value = selected;
	};

	/**
	 * Re-fetch the caller's preset state and repopulate both preset controls.
	 * @returns {Promise<void>}
	 */
	const refreshPresetState = async () => {
		const [my, cfg] = await Promise.all([fetchJson('/api/config/my-preset'), fetchJson('/api/config')]);
		if (!my || !cfg) return;
		const view = Sections.buildPresetControls(my, cfg);
		presetMode = view.mode;
		if (presetLabel) presetLabel.textContent = view.label;
		if (presetSelect) fillSelect(presetSelect, view.options, view.selected);
		if (presetDescription) presetDescription.textContent = cfg.preset_description || '';
		if (defaultRow) defaultRow.hidden = !view.showDefault;
		if (defaultHelp) defaultHelp.hidden = !view.showDefault;
		if (defaultSelect && view.showDefault) fillSelect(defaultSelect, view.defaultOptions, view.defaultSelected);
		if (view.note) setPresetStatus(view.note);
	};

	/** Refresh everything that depends on the preset after a change (made here or in another window). */
	const applyPresetSwitch = async () => {
		await refreshPresetState();
		await this.fetchRequiredApiKeys();
		renderSideKeys();
		await pollJob();
	};

	/**
	 * POST /api/config (changes the server default). Admins only.
	 * @param {string} name
	 * @returns {Promise<boolean>}
	 */
	const setServerDefault = async (name) => {
		setPresetStatus('Switching\u2026');
		try {
			const response = await fetch(`${this.backendURL}/api/config`, {
				method: 'POST',
				headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify({ preset_name: name }),
			});
			if (!response.ok) {
				const err = await response.json().catch(() => ({}));
				setPresetStatus(`Error: ${err.detail || response.status}`);
				return false;
			}
			setPresetStatus('Switched.');
			this.notifyPresetChanged('preferences');
			return true;
		} catch (e) {
			setPresetStatus(`Error: ${e}`);
			return false;
		}
	};

	/**
	 * PUT /api/config/my-preset with any keys entered for it in this request's headers.
	 * @param {string} name
	 * @param {Record<string,string>} keys - key name -> value for credentials the preset still needs
	 * @returns {Promise<boolean>}
	 */
	const chooseMyPreset = async (name, keys) => {
		setPresetStatus('Switching\u2026');
		/** @type {Record<string,string>} */
		const extra = { 'Content-Type': 'application/json' };
		for (const [keyName, value] of Object.entries(keys)) extra[headerForKey(keyName)] = value;
		try {
			const response = await fetch(`${this.backendURL}/api/config/my-preset`, {
				method: 'PUT',
				headers: this.getAuthHeaders(extra),
				body: JSON.stringify({ preset_name: name }),
			});
			if (!response.ok) {
				const err = await response.json().catch(() => ({}));
				setPresetStatus(`Error: ${err.detail || response.status}`);
				return false;
			}
			// Keep the keys that made the choice possible, so later requests carry them.
			for (const [keyName, value] of Object.entries(keys)) {
				Zotero.Prefs.set(`extensions.zotero-rag.serviceApiKey.${keyName}`, value, true);
			}
			setPresetStatus('Switched.');
			return true;
		} catch (e) {
			setPresetStatus(`Error: ${e}`);
			return false;
		}
	};

	/**
	 * Ask for the keys a chosen preset still needs, then choose it.
	 * @param {string} name
	 * @param {string[]} missing
	 */
	const promptForMissingKeys = (name, missing) => {
		if (!presetKeys) return;
		presetKeys.innerHTML = '';
		/** @type {Record<string, HTMLInputElement>} */
		const inputs = {};
		for (const keyName of missing) {
			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'setting-row';
			const label = doc.createElementNS('http://www.w3.org/1999/xhtml', 'label');
			label.textContent = `${keyName}:`;
			const input = /** @type {HTMLInputElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'input'));
			input.type = 'password';
			input.className = 'setting-input';
			input.placeholder = 'Enter your key to use this preset';
			inputs[keyName] = input;
			row.append(label, input);
			presetKeys.appendChild(row);
		}
		const go = /** @type {HTMLButtonElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'button'));
		go.textContent = `Use ${name}`;
		go.addEventListener('click', async () => {
			/** @type {Record<string,string>} */
			const keys = {};
			for (const [k, input] of Object.entries(inputs)) if (input.value.trim()) keys[k] = input.value.trim();
			if (Object.keys(keys).length < missing.length) {
				setPresetStatus('Enter all the keys listed above first.');
				return;
			}
			if (await chooseMyPreset(name, keys)) {
				presetKeys.innerHTML = '';
				this.notifyPresetChanged('preferences');
				await applyPresetSwitch();
			}
		});
		presetKeys.appendChild(go);
		setPresetStatus(`${name} needs ${missing.length === 1 ? 'a key' : 'keys'} you have not entered yet.`);
	};

	if (presetSelect) {
		// A switch from the auto-index status dialog must show up here too.
		this.observePresetChanged(_window, 'preferences', () => {
			setPresetStatus('');
			applyPresetSwitch();
		});
		presetSelect.addEventListener('change', async (e) => {
			const selected = /** @type {HTMLSelectElement} */ (e.target).value;
			if (presetKeys) presetKeys.innerHTML = '';
			if (presetMode === 'loopback') {
				if (await setServerDefault(selected)) await applyPresetSwitch();
				else await refreshPresetState();
				return;
			}
			const my = await fetchJson('/api/config/my-preset');
			const entry = my && (my.selectable || []).find((p) => p.name === selected);
			if (entry && entry.credentials === 'missing') {
				promptForMissingKeys(selected, entry.missing_keys || []);
				return;
			}
			if (await chooseMyPreset(selected, {})) {
				this.notifyPresetChanged('preferences');
				await applyPresetSwitch();
			} else {
				await refreshPresetState(); // revert the select to the preset still in effect
			}
		});
	}
	if (defaultSelect) {
		defaultSelect.addEventListener('change', async (e) => {
			const selected = /** @type {HTMLSelectElement} */ (e.target).value;
			if (await setServerDefault(selected)) await applyPresetSwitch();
			else await refreshPresetState();
		});
	}
	refreshPresetState();
	pollJob();
	// A saved shared value (e.g. a new provider API key) can change endpoint health and
	// which presets have credentials: re-check right away.
	if (sectionsContainer) {
		sectionsContainer.addEventListener('zotero-rag-shared-field-saved', () => {
			refreshPresetState();
			pollJob();
		});
	}

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
				const embeddingKeyInfo = this.requiredApiKeys.find(k => k.kind === 'api_key' && k.required_for.includes('indexing'));
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
			const embeddingKeyInfo = this.requiredApiKeys.find(k => k.kind === 'api_key' && k.required_for.includes('indexing'));
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

	// Indexed-status tags: opt-in real-time tagging (polled by IndexedTags) plus a
	// manual full reconciliation that reuses the key stored for automatic indexing.
	const indexedTagsToggle = /** @type {HTMLInputElement|null} */ (doc.getElementById('zotero-rag-indexed-tags-toggle'));
	const indexedTagsRefresh = /** @type {HTMLButtonElement|null} */ (doc.getElementById('zotero-rag-indexed-tags-refresh'));
	const indexedTagsStatus = doc.getElementById('zotero-rag-indexed-tags-status');

	/**
	 * @param {string} message
	 * @param {'ok'|'warn'|'error'} [level='ok']
	 * @returns {void}
	 */
	const setIndexedTagsStatus = (message, level = 'ok') => {
		if (!indexedTagsStatus) return;
		indexedTagsStatus.textContent = message;
		indexedTagsStatus.className = `setting-description status-${level}`;
	};

	if (indexedTagsToggle && indexedTagsRefresh) {
		indexedTagsToggle.checked = IndexedTags.isEnabled();
		indexedTagsRefresh.disabled = !indexedTagsToggle.checked;
		indexedTagsToggle.addEventListener('change', () => {
			Zotero.Prefs.set(IndexedTags.PREF_ENABLED, indexedTagsToggle.checked, true);
			indexedTagsRefresh.disabled = !indexedTagsToggle.checked;
			// Re-follow from "now" when re-enabled rather than replaying stale events.
			if (indexedTagsToggle.checked) IndexedTags.cursor = null;
		});

		indexedTagsRefresh.addEventListener('click', async () => {
			indexedTagsRefresh.disabled = true;
			setIndexedTagsStatus('Starting...');
			try {
				const stats = await IndexedTags.refresh({
					onProgress: (s) => {
						const lib = s.currentLibrary ? ` (${s.currentLibrary})` : '';
						setIndexedTagsStatus(
							`Library ${Math.min(s.librariesDone + 1, s.librariesTotal || 1)} of ${s.librariesTotal || '?'}${lib}: ` +
							`${s.attachmentsChecked} attachments checked, ${s.added} tagged, ${s.removed} untagged...`
						);
					},
				});
				const failed = stats.errors.length ? ` ${stats.errors.length} librar${stats.errors.length === 1 ? 'y' : 'ies'} failed: ${stats.errors.join('; ')}` : '';
				setIndexedTagsStatus(
					`Done: ${stats.attachmentsChecked} attachments checked, ${stats.added} tagged, ${stats.removed} untagged, ${stats.skipped} skipped.${failed}`,
					stats.errors.length ? 'warn' : 'ok'
				);
			} catch (e) {
				setIndexedTagsStatus(`Error: ${e instanceof Error ? e.message : e}`, 'error');
			} finally {
				indexedTagsRefresh.disabled = !indexedTagsToggle.checked;
			}
		});
	}

	// "Indexed Content" section: admin-only "Index Snapshots of webpages"
	// setting + purge action. This controls what gets indexed at all,
	// independent of how indexing was triggered (scheduled auto-indexing or
	// client-initiated, e.g. "Fix unavailable"), so it lives in its own
	// section rather than under "Automatic indexing" or the status-monitor
	// dialog's runtime controls.
	const indexedContentGroup = doc.getElementById('zotero-rag-indexed-content-group');
	const indexSnapshotsToggle = /** @type {HTMLInputElement|null} */ (doc.getElementById('zotero-rag-index-snapshots-toggle'));
	const purgeSnapshotsButton = /** @type {HTMLButtonElement|null} */ (doc.getElementById('zotero-rag-purge-snapshots-button'));
	const purgeSnapshotsMessage = doc.getElementById('zotero-rag-purge-snapshots-message');

	/**
	 * @param {string} text
	 * @param {'ok'|'error'} [level='ok']
	 * @returns {void}
	 */
	const setPurgeSnapshotsMessage = (text, level = 'ok') => {
		if (!purgeSnapshotsMessage) return;
		purgeSnapshotsMessage.textContent = text;
		purgeSnapshotsMessage.className = level === 'error' ? 'setting-description status-error' : 'setting-description';
	};

	/**
	 * Show the "Indexed Content" section only for an admin of the authorizing
	 * group (or always, on a loopback backend) — mirrors the is_admin flag
	 * already reported by /api/autoindex/status for the status-monitor
	 * dialog's own admin controls, reused here to avoid a separate
	 * admin-check endpoint. When shown, also loads the live index_snapshots
	 * value into the checkbox.
	 * @returns {Promise<void>}
	 */
	const refreshIndexedContentSection = async () => {
		if (!indexedContentGroup) return;
		try {
			const statusResponse = await fetch(`${this.backendURL}/api/autoindex/status`, {
				headers: this.getAuthHeaders(),
			});
			if (!statusResponse.ok) {
				indexedContentGroup.style.display = 'none';
				return;
			}
			const statusData = await statusResponse.json();
			if (statusData.is_admin !== true) {
				indexedContentGroup.style.display = 'none';
				return;
			}
			indexedContentGroup.style.display = '';
			if (indexSnapshotsToggle) {
				const settingsResponse = await fetch(`${this.backendURL}/api/admin/settings`, {
					headers: this.getAuthHeaders(),
				});
				if (settingsResponse.ok) {
					const settingsData = await settingsResponse.json();
					indexSnapshotsToggle.checked = settingsData.index_snapshots === true;
				}
			}
		} catch (_) {
			indexedContentGroup.style.display = 'none';
		}
	};

	/**
	 * Two sequential confirms ("delete them?", then "this cannot be undone"),
	 * only calling the purge endpoint if both are accepted. Disables the
	 * button for the duration and leaves a persistent result message next to
	 * it (not the transient autoindex status line, which other polling/async
	 * activity on this pane could otherwise overwrite).
	 * @returns {Promise<void>}
	 */
	const confirmAndPurgeSnapshots = async () => {
		// @ts-ignore - Services is a Zotero/Firefox global
		if (!Services.prompt.confirm(_window, 'Delete Snapshot entries', 'Delete all already-indexed Snapshot entries from the index now?')) return;
		// @ts-ignore
		if (!Services.prompt.confirm(_window, 'Delete Snapshot entries', 'This cannot be undone. Continue?')) return;

		const originalLabel = 'Delete indexed Snapshot entries…';
		setPurgeSnapshotsMessage('');
		if (purgeSnapshotsButton) {
			purgeSnapshotsButton.disabled = true;
			purgeSnapshotsButton.textContent = 'Deleting…';
		}
		try {
			const response = await fetch(`${this.backendURL}/api/admin/settings/purge-snapshots`, {
				method: 'POST',
				headers: this.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				setPurgeSnapshotsMessage(body.detail || `Could not purge Snapshot entries (HTTP ${response.status}).`, 'error');
				return;
			}
			const data = await response.json();
			setPurgeSnapshotsMessage(`Deleted ${data.deleted_chunks} chunk(s) across ${data.deleted_attachments} attachment(s).`);
		} catch (e) {
			setPurgeSnapshotsMessage(`Error: ${e}`, 'error');
		} finally {
			if (purgeSnapshotsButton) {
				purgeSnapshotsButton.disabled = false;
				purgeSnapshotsButton.textContent = originalLabel;
			}
		}
	};

	if (indexSnapshotsToggle) {
		indexSnapshotsToggle.addEventListener('change', async () => {
			const enabled = indexSnapshotsToggle.checked;
			let response;
			try {
				response = await fetch(`${this.backendURL}/api/admin/settings`, {
					method: 'PUT',
					headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify({ index_snapshots: enabled }),
				});
			} catch (e) {
				setPurgeSnapshotsMessage(`Error updating setting: ${e}`, 'error');
				return;
			}
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				indexSnapshotsToggle.checked = !enabled;
				setPurgeSnapshotsMessage(body.detail || `Could not update setting (HTTP ${response.status}).`, 'error');
				return;
			}
			setPurgeSnapshotsMessage('');
			if (!enabled) {
				await confirmAndPurgeSnapshots();
			}
		});
	}

	if (purgeSnapshotsButton) {
		purgeSnapshotsButton.addEventListener('click', () => confirmAndPurgeSnapshots());
	}

	// Initial population, now that both closures above exist
	refreshZoteroIdentityStatus();
	refreshIndexedContentSection();
};
