// Main plugin code

// todo: can https://windingwind.github.io/zotero-plugin-toolkit/ be used?

// @ts-check
/// <reference path="./zotero-types.d.ts" />
/// <reference path="./toolkit.d.ts" />

// Wire console methods so messages appear in the Browser Console.
// log/info → logStringMessage (neutral); warn/error → nsIScriptError with severity flag.
// zotero-rag.js is loaded via loadSubScript where console may not exist — create it if needed.
;(function() {
	/** @type {Record<string, number>} */
	const nsFlags = { warn: 0x1, error: 0x0 };
	const makeLogger = (/** @type {string} */ level) => (/** @type {any[]} */ ...args) => {
		const msg = "[Zotero RAG] " + args.join(" ");
		if (level === "log" || level === "info") {
			// @ts-ignore
			Services.console.logStringMessage(msg);
		} else {
			// @ts-ignore
			const e = Cc["@mozilla.org/scripterror;1"].createInstance(Ci.nsIScriptError);
			// @ts-ignore
			e.init(msg, "", null, 0, 0, nsFlags[level], "chrome javascript");
			// @ts-ignore
			Services.console.logMessage(e);
		}
	};
	// @ts-ignore
	if (typeof console === "undefined") {
		// @ts-ignore - globalThis is available in Gecko
		globalThis.console = { log: makeLogger("log"), info: makeLogger("info"), warn: makeLogger("warn"), error: makeLogger("error") };
	} else {
		["log", "info", "warn", "error"].forEach(level => { /** @type {any} */ (console)[level] = makeLogger(level); });
	}
})();


/**
 * @typedef {Object} Library
 * @property {string} id - Library ID
 * @property {string} name - Library name
 * @property {'user'|'group'} type - Library type
 */

/**
 * @typedef {Object} QueryResult
 * @property {string} question - Original question
 * @property {string} answer - Generated answer
 * @property {string} answer_format - Format of answer: "text", "html", or "markdown"
 * @property {Array<SourceCitation>} sources - Source citations
 * @property {Array<string>} library_ids - Libraries queried
 */


/**
 * @typedef {Object} SourceCitation
 * @property {string} item_id - Zotero item ID
 * @property {string} library_id - Zotero library ID
 * @property {string} title - Document title
 * @property {number|null} page_number - Page number (if available)
 * @property {string|null} text_anchor - Text anchor (first 5 words)
 * @property {number} relevance_score - Relevance score
 */

/**
 * @typedef {Object} QueryOptions
 * @property {number} [topK] - Number of chunks to retrieve (default: 5)
 * @property {number} [minScore] - Minimum similarity score (default: 0.5)
 */


/**
 * @typedef {Object} BackendVersion
 * @property {string} api_version - Backend API version string
 * @property {string} service - Service name
 */

/**
 * Main plugin class for Zotero RAG integration.
 */
class ZoteroRAGPlugin {
	constructor() {
		/** @type {string|null} */
		this.id = null;

		/** @type {string|null} */
		this.version = null;

		/** @type {string|null} */
		this.rootURI = null;

		/** @type {boolean} */
		this.initialized = false;

		/** @type {Array<string>} */
		this.addedElementIDs = [];

		/** @type {string|null} */
		this.backendURL = null;

		/** @type {string} */
		this.apiKey = '';

		/** @type {Set<string>} */
		this.activeQueries = new Set();

		/** @type {number} */
		this.maxConcurrentQueries = 5;

		/**
		 * API key requirements fetched from the backend. Cached in Zotero prefs.
		 * @type {Array<{key_name: string, header_name: string, description: string, required_for: string[]}>}
		 */
		this.requiredApiKeys = [];

		/** @type {Window|null} */
		this._dialogWindow = null;

		/** @type {string|null} */
		this._notifierID = null;

		/** @type {import('./toolkit.d.ts').Toolkit} */
		// @ts-ignore - Initialized in init() method
		this.toolkit = null;
	}

	/**
	 * Initialize the plugin.
	 * @param {Object} config - Plugin configuration
	 * @param {string} config.id - Plugin ID
	 * @param {string} config.version - Plugin version
	 * @param {string} config.rootURI - Plugin root URI
	 * @returns {void}
	 */
	init({ id, version, rootURI }) {
		if (this.initialized) return;
		this.id = id;
		this.version = version;
		this.rootURI = rootURI;
		this.initialized = true;

		// Initialize Zotero Plugin Toolkit
		// The bundle creates a global var ZoteroPluginToolkit
		// @ts-ignore - ZoteroPluginToolkit is a global variable created by the bundled toolkit
		if (typeof ZoteroPluginToolkit !== 'undefined') {
			// @ts-ignore - ZoteroPluginToolkit is a global variable
			this.toolkit = ZoteroPluginToolkit.createToolkit({ id, version, rootURI });
			this.log('Toolkit initialized successfully');
		} else {
			this.log('WARNING: Toolkit bundle not loaded');
		}

		// Load backend URL from preferences (default: localhost:8119)
		this.backendURL = (Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || 'http://localhost:8119').replace(/\/+$/, '');

		// Load optional API key (required when backend is on a remote host)
		this.apiKey = Zotero.Prefs.get('extensions.zotero-rag.apiKey', true) || '';

		// Load cached required API keys list (refreshed on each successful backend connection)
		try {
			const cached = Zotero.Prefs.get('extensions.zotero-rag.requiredApiKeys', true) || '[]';
			this.requiredApiKeys = JSON.parse(cached);
		} catch (_) {
			this.requiredApiKeys = [];
		}

		// Watch for permanent item deletions and remove their indexed chunks from the backend.
		this._notifierID = Zotero.Notifier.registerObserver(
			{
				notify: (/** @type {string} */ event, /** @type {string} */ type, /** @type {number[]} */ ids, /** @type {Record<number, {libraryID: number, key: string}>} */ extraData) => {
					if (event !== 'delete' || type !== 'item') return;
					for (const id of ids) {
						const { libraryID, key } = extraData[id] || {};
						if (!libraryID || !key) continue;
						const url = `${this.backendURL}/api/libraries/${libraryID}/items/${key}/chunks`;
						fetch(url, { method: 'DELETE', headers: this.getAuthHeaders() })
							.catch(e => console.warn(`Failed to delete chunks for item ${key}: ${e.message}`));
					}
				}
			},
			['item']
		);
	}

	/**
	 * Return HTTP headers to include in all backend requests.
	 * Adds X-API-Key when an API key is configured.
	 * @param {Record<string, string>} [extra] - Additional headers to merge
	 * @returns {Record<string, string>}
	 */
	getAuthHeaders(extra = {}) {
		/** @type {Record<string, string>} */
		const headers = { ...extra };
		if (this.apiKey) {
			headers['X-API-Key'] = this.apiKey;
		}
		// Include any service API keys the user has configured
		for (const keyInfo of this.requiredApiKeys) {
			const value = Zotero.Prefs.get(`extensions.zotero-rag.serviceApiKey.${keyInfo.key_name}`, true) || '';
			headers[keyInfo.header_name] = value;
		}
		return headers;
	}

	/**
	 * Append the API key as a query parameter to a URL.
	 * Used for SSE (EventSource) endpoints that cannot set request headers.
	 * @param {string} url - Base URL
	 * @returns {string} URL with api_key appended when configured
	 */
	addApiKeyParam(url) {
		if (!this.apiKey) return url;
		const sep = url.includes('?') ? '&' : '?';
		return `${url}${sep}api_key=${encodeURIComponent(this.apiKey)}`;
	}

	/**
	 * Log a debug message.
	 * @param {string} msg - Message to log
	 * @returns {void}
	 */
	log(msg) {
		console.log(msg);
	}

	/**
	 * Add plugin UI elements to a window.
	 * @param {Window} window - Zotero window
	 * @returns {void}
	 */
	addToWindow(window) {
		let doc = window.document;

		// Add menu item under Tools menu
		// Note: Menu items still need to use XUL elements as they integrate with Zotero's existing XUL menus
		// @ts-ignore - createXULElement is available in Zotero/Firefox XUL context
		let menuitem = doc.createXULElement('menuitem');
		menuitem.id = 'zotero-rag-ask-question';
		menuitem.setAttribute('label', 'Zotero RAG: Ask Question...');
		menuitem.addEventListener('command', async () => {
			await this.openQueryDialog(window);
		});

		// Add to Tools menu
		let toolsMenu = doc.getElementById('menu_ToolsPopup');
		if (toolsMenu) {
			toolsMenu.appendChild(menuitem);
			this.storeAddedElement(menuitem);
		}

		// Inject a badge-style toolbar button showing the count of unavailable attachments.
		const toolbar = doc.getElementById('zotero-items-toolbar');
		if (toolbar) {
			// @ts-ignore
			const btn = /** @type {any} */ (doc.createXULElement('toolbarbutton'));
			btn.id = 'zotero-rag-unavailable-btn';
			btn.setAttribute('hidden', 'true');
			btn.setAttribute('tooltiptext', 'Missing attachment files — click to find and fix');
			btn.style.cssText = [
				'position:relative',
				'margin-left:6px',
				'padding:2px 4px',
				'min-width:0',
				'-moz-appearance:none',
				'appearance:none',
				'background:none',
				'border:none',
				'cursor:pointer',
			].join(';');

			// Icon character (paperclip-like warning)
			// @ts-ignore
			const icon = /** @type {any} */ (doc.createXULElement('label'));
			icon.setAttribute('value', '\u26A0');  // ⚠
			icon.style.cssText = 'font-size:14px; color:#cc6600; pointer-events:none;';
			btn.appendChild(icon);

			// Badge overlay showing the count
			// @ts-ignore
			const badge = /** @type {any} */ (doc.createXULElement('label'));
			badge.id = 'zotero-rag-unavailable-badge';
			badge.setAttribute('value', '');
			badge.style.cssText = [
				'position:absolute',
				'top:-4px',
				'right:-6px',
				'min-width:14px',
				'height:14px',
				'background:#cc0000',
				'color:#fff',
				'font-size:9px',
				'font-weight:bold',
				'border-radius:7px',
				'text-align:center',
				'padding:0 3px',
				'pointer-events:none',
				'line-height:14px',
			].join(';');
			btn.appendChild(badge);

			btn.addEventListener('click', () => this.openFixUnavailableDialog(window));
			toolbar.appendChild(btn);
			this.storeAddedElement(btn);
			// Initial async scan
			this._scanUnavailableCount(window);
		}

		// Wrap ZoteroPane.onCollectionSelected to detect both collection and library switches.
		// This is the only reliable hook — the Notifier 'collection' select event does not fire
		// when switching between libraries (only within a library).
		const pane = /** @type {any} */ (window.ZoteroPane);
		if (pane && !pane._zoteroRagOrigOnCollectionSelected) {
			pane._zoteroRagOrigOnCollectionSelected = pane.onCollectionSelected;
			pane.onCollectionSelected = async (/** @type {any[]} */ ...args) => {
				const result = await pane._zoteroRagOrigOnCollectionSelected.apply(pane, args);
				clearTimeout(this._unavailableScanTimeout);
				this._unavailableScanTimeout = setTimeout(() => this._scanUnavailableCount(window), 150);
				return result;
			};
		}
	}

	/**
	 * Add plugin UI to all open Zotero windows.
	 * @returns {void}
	 */
	addToAllWindows() {
		var windows = Zotero.getMainWindows();
		for (let win of windows) {
			if (!win.ZoteroPane) continue;
			this.addToWindow(win);
		}
	}

	/**
	 * Store reference to added DOM element for cleanup.
	 * @param {Element} elem - Element with id attribute
	 * @returns {void}
	 */
	storeAddedElement(elem) {
		if (!elem.id) {
			throw new Error("Element must have an id");
		}
		this.addedElementIDs.push(elem.id);
	}

	/**
	 * Remove plugin UI from a window.
	 * @param {Window} window - Zotero window
	 * @returns {void}
	 */
	removeFromWindow(window) {
		var doc = window.document;
		// Remove all elements added to DOM
		for (let id of this.addedElementIDs) {
			doc.getElementById(id)?.remove();
		}
		doc.querySelector('[href="zotero-rag.ftl"]')?.remove();
		// Restore wrapped ZoteroPane.onCollectionSelected
		const pane = /** @type {any} */ (window.ZoteroPane);
		if (pane && pane._zoteroRagOrigOnCollectionSelected) {
			pane.onCollectionSelected = pane._zoteroRagOrigOnCollectionSelected;
			delete pane._zoteroRagOrigOnCollectionSelected;
		}
	}

	/**
	 * Remove plugin UI from all open Zotero windows.
	 * @returns {void}
	 */
	removeFromAllWindows() {
		var windows = Zotero.getMainWindows();
		for (let win of windows) {
			if (!win.ZoteroPane) continue;
			this.removeFromWindow(win);
		}
		if (this._notifierID) {
			Zotero.Notifier.unregisterObserver(this._notifierID);
			this._notifierID = null;
		}
	}

	/**
	 * Main plugin entry point.
	 * @returns {Promise<void>}
	 */
	async main() {
		this.log(`Plugin initialized with backend URL: ${this.backendURL}`);

		// Check backend version compatibility
		try {
			await this.checkBackendVersion();
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			this.log(`Backend not available: ${errorMessage}`);
			return;
		}

		// Log backend service configuration and DB statistics
		try {
			await this.logServerInfo();
		} catch (e) {
			this.log(`Could not fetch server info: ${e instanceof Error ? e.message : String(e)}`);
		}
	}

	/**
	 * Check backend version for compatibility.
	 * @returns {Promise<string>} Backend version string
	 * @throws {Error} If backend is not reachable or returns error
	 */
	async checkBackendVersion() {
		if (!this.backendURL) {
			throw new Error('Backend URL not configured');
		}

		try {
			const response = await fetch(`${this.backendURL}/api/version`, {
				headers: this.getAuthHeaders()
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				throw new Error(`GET /api/version: HTTP ${response.status}${body.detail ? ` — ${body.detail}` : ''}`);
			}
			const data = /** @type {BackendVersion} */ (await response.json());
			this.log(`Backend version: ${data.api_version}`);

			// TODO: Add version compatibility checking
			// Refresh the list of required API keys from the server
			await this.fetchRequiredApiKeys();
			return data.api_version;
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			throw new Error(`Failed to check backend version: ${errorMessage}`);
		}
	}

	/**
	 * Fetch root endpoint and log service configuration and vector DB statistics.
	 * @returns {Promise<void>}
	 */
	async logServerInfo() {
		const response = await fetch(`${this.backendURL}/`, { headers: this.getAuthHeaders() });
		if (!response.ok) return;
		/** @type {{preset:{name:string,description:string,memory_budget_gb:number}, embedding:{model_type:string,model_name:string,base_url:string|null,embedding_dim:number,distance:string}, llm:{model_type:string,model_name:string,base_url:string|null,max_context_length:number,temperature:number}, rag:{top_k:number,score_threshold:number,max_chunk_size:number}, vector_db:{path:string,chunks:number,indexed_documents:number,libraries:number}}} */
		const info = await response.json();
		const { preset, embedding, llm, rag, vector_db } = info;
		this.log(`Preset: ${preset.name} — ${preset.description} (${preset.memory_budget_gb} GB)`);
		this.log(`Embedding: [${embedding.model_type}] ${embedding.model_name}${embedding.base_url ? ` @ ${embedding.base_url}` : ''} (${embedding.embedding_dim}-dim, ${embedding.distance})`);
		this.log(`LLM: [${llm.model_type}] ${llm.model_name}${llm.base_url ? ` @ ${llm.base_url}` : ''} (ctx ${llm.max_context_length}, temp ${llm.temperature})`);
		this.log(`RAG: top_k=${rag.top_k}, score_threshold=${rag.score_threshold}, chunk_size=${rag.max_chunk_size}`);
		this.log(`Vector DB: ${vector_db.chunks} chunks, ${vector_db.indexed_documents} documents, ${vector_db.libraries} libraries (${vector_db.path})`);
	}

	/**
	 * Fetch the list of API keys required by the backend preset and cache them.
	 * Silently no-ops if the server is unreachable.
	 * @returns {Promise<void>}
	 */
	async fetchRequiredApiKeys() {
		try {
			const resp = await fetch(`${this.backendURL}/api/required-keys`, {
				headers: {
					'Content-Type': 'application/json',
					...(this.apiKey ? { 'X-API-Key': this.apiKey } : {}),
				},
			});
			if (!resp.ok) return;
			const data = await resp.json();
			this.requiredApiKeys = data.keys || [];
			Zotero.Prefs.set('extensions.zotero-rag.requiredApiKeys', JSON.stringify(this.requiredApiKeys), true);
		} catch (e) {
			this.log('Could not fetch required API keys: ' + e);
		}
	}

	/**
	 * Open the query dialog.
	 * @param {Window} window - Parent window
	 * @returns {Promise<void>}
	 */
	async openQueryDialog(window) {
		// Check concurrent query limit
		if (this.activeQueries.size >= this.maxConcurrentQueries) {
			this.showError(`Maximum concurrent queries (${this.maxConcurrentQueries}) reached. Please wait for existing queries to complete.`);
			return;
		}
		// If dialog is already open, focus it instead of opening a new one
		if (this._dialogWindow && !this._dialogWindow.closed) {
			this._dialogWindow.focus();
			return;
		}

		// Open dialog window using chrome:// URL
		const dialogURL = 'chrome://zotero-rag/content/dialog.xhtml';
		const dialogFeatures = 'chrome,centerscreen,resizable=yes,width=600,height=600';

		// @ts-ignore - openDialog is available in XUL/Firefox extension context
		this._dialogWindow = window.openDialog(
			dialogURL,
			'zotero-rag-dialog',
			dialogFeatures,
			{ plugin: this }
		);
	}

	/**
	 * Return true if the configured backend URL is a localhost address.
	 * @returns {boolean}
	 */
	isLocalBackend() {
		try {
			const h = new URL(this.backendURL||'')?.hostname;
			return h === 'localhost' || h === '127.0.0.1';
		} catch {
			return true;
		}
	}

	/**
	 * Return the numeric zotero.org user ID of the currently synced user, or null.
	 * @returns {number|null}
	 */
	getCurrentZoteroUserId() {
		const id = Zotero.Users.getCurrentUserID();
		return id ? Number(id) : null;
	}

	/**
	 * Register a library and the current zotero.org user with the backend.
	 *
	 * @param {string} libraryId
	 * @param {string} libraryName
	 * @returns {Promise<{exists: boolean}>}
	 * @throws {Error} If the user has no Zotero sync account or registration fails
	 */
	async registerLibrary(libraryId, libraryName) {
		const userId = this.getCurrentZoteroUserId();
		const username = Zotero.Users.getCurrentUsername();
		if (!userId || !username) {
			throw new Error(
				'Zotero user not found. Please set up synchronization with zotero.org ' +
				'in Zotero Preferences > Sync.'
			);
		}
		const response = await fetch(`${this.backendURL}/api/register`, {
			method: 'POST',
			headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
			body: JSON.stringify({ library_id: libraryId, library_name: libraryName, user_id: userId, username })
		});
		if (!response.ok) {
			const err = await response.json().catch(() => ({}));
			throw new Error(`Registration failed: ${err.detail || response.status}`);
		}
		return response.json();
	}

	/**
	 * Show error message to user.
	 * @param {string} message - Error message
	 * @returns {void}
	 */
	showError(message) {
		this.toolkit.showError(message);
	}

	/**
	 * Get all available libraries.
	 * @returns {Array<Library>} List of libraries
	 */
	getLibraries() {
		/** @type {Array<Library>} */
		const libraries = [];
		const userLibraryID = Zotero.Libraries.userLibraryID;
		const userLibrary = Zotero.Libraries.get(userLibraryID);

		const userId = this.getCurrentZoteroUserId();
		const username = Zotero.Users.getCurrentUsername();
		libraries.push({
			id: userId ? `u${userId}` : String(userLibraryID),
			name: `${username}'s library`,
			type: 'user'
		});

		// Get all group libraries
		// NOTE: For groups, we need to send the group ID (not library ID) to the backend
		// The backend needs group ID to access /api/groups/{GROUP_ID}/items
		const groups = Zotero.Groups.getAll();
		for (let group of groups) {
			libraries.push({
				id: String(group.id),  // Use group.id instead of group.libraryID
				name: group.name,
				type: /** @type {'group'} */ ('group')
			});
		}

		return libraries;
	}

	/**
	 * Get currently selected library/collection.
	 * @returns {string|null} Library ID or null if none selected
	 */
	getCurrentLibrary() {
		const zoteroPane = Zotero.getActiveZoteroPane();
		if (!zoteroPane) return null;

		const libraryID = zoteroPane.getSelectedLibraryID();
		if (!libraryID) return null;

		// For group libraries, return the group ID instead of library ID
		const library = Zotero.Libraries.get(libraryID);
		// @ts-ignore - libraryType exists on ZoteroLibrary at runtime
		if (library && library.libraryType === 'group') {
			// Get the group associated with this library
			// @ts-ignore - getByLibraryID exists on Zotero.Groups at runtime
			const group = Zotero.Groups.getByLibraryID(libraryID);
			if (group) {
				return String(group.id);  // Return group ID for backend
			}
		}

		// For user library, use "u{zoteroUserId}" when synced; fall back to raw ID on localhost-no-registration
		const userId = this.getCurrentZoteroUserId();
		return userId ? `u${userId}` : String(libraryID);
	}

	/**
	 * Submit a query to the backend.
	 * @param {string} question - Question to ask
	 * @param {Array<string>} libraryIDs - Library IDs to query
	 * @param {QueryOptions} [options] - Query options
	 * @returns {Promise<QueryResult>} Query result with answer and sources
	 * @throws {Error} If query fails
	 */
	async submitQuery(question, libraryIDs, options = {}) {
		if (!this.backendURL) {
			throw new Error('Backend URL not configured');
		}

		const queryId = Date.now().toString();
		this.activeQueries.add(queryId);

		try {
			// Submit query directly - the backend should be available if indexing succeeded
			// Build payload - only include optional params if explicitly set (let backend use preset defaults)
			/** @type {Record<string, any>} */
			const payload = {
				question,
				library_ids: libraryIDs
			};

			if (options.topK !== undefined) {
				payload.top_k = options.topK;
			}
			if (options.minScore !== undefined) {
				payload.min_score = options.minScore;
			}

			const response = await fetch(`${this.backendURL}/api/query`, {
				method: 'POST',
				headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify(payload)
			});

			if (!response.ok) {
				const ct = response.headers.get('content-type') || '';
				const errorData = ct.includes('application/json')
					? /** @type {any} */ (await response.json().catch(() => ({})))
					: { detail: (await response.text().catch(() => '')).slice(0, 300) };
				throw new Error(`POST /api/query: HTTP ${response.status}${errorData.detail ? ` — ${errorData.detail}` : ''}`);
			}

			const result = /** @type {QueryResult} */ (await response.json());
			return result;
		} finally {
			this.activeQueries.delete(queryId);
		}
	}

	/**
	 * Create a note in the current collection with the query result.
	 * @param {string} question - Original question
	 * @param {QueryResult} result - Query result
	 * @param {Array<string>} libraryIDs - Libraries that were queried
	 * @returns {Promise<*>} Created note item
	 * @throws {Error} If note creation fails
	 */
	async createResultNote(question, result, libraryIDs) {
		const zoteroPane = Zotero.getActiveZoteroPane();
		if (!zoteroPane) {
			throw new Error('No active Zotero pane');
		}

		// Get current library/collection
		const libraryID = zoteroPane.getSelectedLibraryID();
		const collectionID = zoteroPane.getSelectedCollection()?.id;

		// Create standalone note
		const note = new Zotero.Item('note');
		if (libraryID !== null) {
			note.libraryID = libraryID;
		}

		// Format note content as HTML
		const html = this.formatNoteHTML(question, result, libraryIDs);
		note.setNote(html);

		// Add to collection before saving so it's included in the same transaction
		if (collectionID) {
			// @ts-ignore - addToCollection exists on Zotero.Item at runtime
			note.addToCollection(collectionID);
		}

		// Save note
		await note.saveTx();

		// Open the note in a separate window and resize it
		zoteroPane.openNoteWindow(note.id);
		const noteWin = zoteroPane.findNoteWindow(note.id);
		if (noteWin) noteWin.resizeTo(900, 700);

		return note;
	}

	/**
	 * Get Zotero library ID from library/group ID used by backend.
	 * @param {string} backendLibraryId - Library or group ID used by backend
	 * @param {string} libraryType - Library type ('user' or 'group')
	 * @returns {number|null} Zotero library ID or null if not found
	 */
	getZoteroLibraryID(backendLibraryId, libraryType) {
		if (libraryType === 'group') {
			// For groups, convert group ID back to library ID
			// @ts-ignore - getByLibraryID exists on Zotero.Groups at runtime
			const group = Zotero.Groups.get(parseInt(backendLibraryId, 10));
			return group ? group.libraryID : null;
		} else {
			// User library: backend ID may be "u<userId>" — always use the local userLibraryID.
			return Zotero.Libraries.userLibraryID;
		}
	}

	/**
	 * Get Zotero item by key from specified library.
	 * @param {string} itemKey - Item key (e.g., "6YDQPV8I")
	 * @param {number} libraryID - Zotero library ID
	 * @returns {*|null} Zotero item or null if not found
	 */
	getZoteroItem(itemKey, libraryID) {
		try {
			// @ts-ignore - Zotero.Items.getByLibraryAndKey exists at runtime
			return Zotero.Items.getByLibraryAndKey(libraryID, itemKey);
		} catch (e) {
			this.log(`Failed to get item ${itemKey} from library ${libraryID}: ${e}`);
			return null;
		}
	}

	/**
	 * Format citation display text from Zotero item (Author, Year format).
	 * @param {*} item - Zotero item
	 * @returns {string} Formatted citation text (e.g., "Smith, 2020" or "Title")
	 */
	formatCitationDisplayText(item) {
		if (!item) {
			return 'Unknown';
		}

		try {
			// Get first creator (author/editor)
			const creators = item.getCreators();
			let authorName = '';

			if (creators && creators.length > 0) {
				const firstCreator = creators[0];
				// Use lastName if available, otherwise full name
				authorName = firstCreator.lastName || firstCreator.name || firstCreator.firstName || '';

				// If multiple authors, add "et al."
				if (creators.length > 1) {
					authorName += ' et al.';
				}
			}

			// Get year from date field
			const date = item.getField('date');
			let year = '';
			if (date) {
				// Extract year from date string (handles formats like "2020", "2020-01-01", "January 2020")
				const yearMatch = date.match(/\b(\d{4})\b/);
				if (yearMatch) {
					year = yearMatch[1];
				}
			}

			// Format as "Author, Year" or fallback to title
			if (authorName && year) {
				return `${authorName}, ${year}`;
			} else if (authorName) {
				return authorName;
			} else if (year) {
				return year;
			} else {
				// Fallback to title
				return item.getField('title') || 'Unknown';
			}
		} catch (e) {
			this.log(`Error formatting citation display: ${e}`);
			return item.getField('title') || 'Unknown';
		}
	}

	/**
	 * Build a zotero://select/ URI for a source citation.
	 * @param {SourceCitation} source - Source citation metadata
	 * @param {'user'|'group'} libraryType - Library type
	 * @returns {string} zotero://select/ URI
	 */
	buildZoteroSelectURI(source, libraryType) {
		if (libraryType === 'group') {
			return `zotero://select/groups/${source.library_id}/items/${source.item_id}`;
		} else {
			return `zotero://select/library/items/${source.item_id}`;
		}
	}

	/**
	 * Build a zotero://open-pdf/ URI pointing at the best PDF attachment of an item.
	 * Falls back to zotero://select/ if no PDF attachment is found.
	 * @param {SourceCitation} source - Source citation metadata
	 * @param {'user'|'group'} libraryType - Library type
	 * @param {*} item - Zotero item (may be null)
	 * @param {number|null} [pageOverride] - Page number from the inline citation (e.g. from [N:P]),
	 *   takes precedence over source.page_number so the link opens the exact cited page.
	 * @returns {string} zotero://open-pdf/ or zotero://select/ URI
	 */
	buildZoteroPDFURI(source, libraryType, item, pageOverride = null) {
		const page = pageOverride !== null ? pageOverride : (source.page_number || '1');
		if (item) {
			try {
				const attachmentIDs = item.getAttachments();
				for (const attachmentID of attachmentIDs) {
					// @ts-ignore - Zotero.Items exists at runtime
					const attachment = Zotero.Items.get(attachmentID);
					if (attachment && attachment.attachmentContentType === 'application/pdf') {
						const key = attachment.key;
						if (libraryType === 'group') {
							return `zotero://open-pdf/groups/${source.library_id}/items/${key}?page=${page}`;
						} else {
							return `zotero://open-pdf/library/items/${key}?page=${page}`;
						}
					}
				}
			} catch (e) {
				this.log(`Error getting PDF attachment for ${source.item_id}: ${e}`);
			}
		}
		// Fallback: open the item in the Zotero library view
		return this.buildZoteroSelectURI(source, libraryType);
	}

	/**
	 * Format an inline citation as a zotero://open-pdf/ hyperlink.
	 * Page number is shown without quotes; text anchor (quote context) is shown in quotes
	 * only when no page number is available.
	 * @param {string} uri - zotero://open-pdf/ or zotero://select/ URI
	 * @param {string} displayText - Display text (e.g., "Smith et al., 2020")
	 * @param {number|null} [_page] - Optional page number (currently unused)
	 * @param {string|null} [textAnchor] - Optional quote context (used only when page is absent)
	 * @returns {string} HTML anchor element
	 */
	formatCitationHTML(uri, displayText, _page = null, textAnchor = null) {
		let label = this.escapeHTML(displayText);
		let title = "";

		// this is the page index which is uninformative without the page offset in the PDF, which don't have
		// if (page !== null && page !== undefined) {
		// 	label += `, p. ${page}`;
		// }  
			
		if (textAnchor) {
			title = this.escapeHTML(textAnchor);
		}
		return `<a href="${uri}" title="${title}">(${label})</a>`;
	}

	/**
	 * Look up source metadata by source number (1-indexed).
	 * @param {number} sourceNum - Source number (1-indexed)
	 * @param {Array<SourceCitation>} sources - Array of source citations
	 * @returns {SourceCitation|null} Source citation or null if not found
	 */
	lookupSource(sourceNum, sources) {
		const index = sourceNum - 1;
		if (index >= 0 && index < sources.length) {
			return sources[index];
		}
		return null;
	}

	/**
	 * Get library type for a given library/group ID.
	 * @param {string} libraryId - Library or group ID
	 * @param {Map<string, {name: string, type: 'user'|'group'}>} libraryMap - Map of library info
	 * @returns {'user'|'group'} Library type
	 */
	getLibraryType(libraryId, libraryMap) {
		const libInfo = libraryMap.get(libraryId);
		return libInfo ? libInfo.type : 'user';
	}

	/**
	 * Replace inline citation references with proper Zotero citation format.
	 * Pattern: [<source number>] or [<source number>:<page>] or [1,2,3] or [1:10,2:20]
	 * @param {string} text - Text with inline citation references
	 * @param {Array<SourceCitation>} sources - Array of source citations
	 * @param {Map<string, {name: string, type: 'user'|'group'}>} libraryMap - Map of library info
	 * @returns {string} Text with citations replaced by HTML citation spans
	 */
	replaceCitationsInText(text, sources, libraryMap) {
		// Normalise legacy "Source N" word form → [SN]
		const sourceWordPattern = /\*{0,2}Source\s+(\d+)\*{0,2}/g;
		text = text.replace(sourceWordPattern, (_m, n) => `[S${n}]`);

		// Primary pattern: [S1], [S1:10], [S1:p.10], [S1,S2,S3], [S1:10,S2:20]
		// Fallback pattern: [1], [1:10] — kept for older cached responses
		// Page part tolerates an optional "p." or "p " prefix that some LLMs insert.
		const pageToken = '(?::p\\.?\\s*\\d+|:\\d+)?';
		const sRef = `[Ss]\\d+${pageToken}`;
		const nRef = `\\d+(?::\\d+)?`;
		const citationPattern = new RegExp(
			`\\[(${sRef}(?:,\\s*${sRef})*|${nRef}(?:,\\s*${nRef})*)\\]`, 'g'
		);

		return text.replace(citationPattern, (_match, citationList) => {
			// Parse comma-separated citations
			const citations = citationList.split(/,\s*/);
			const citationSpans = [];

			for (let citation of citations) {
				// Strip optional leading S/s prefix, then parse number and optional page
				const normalised = citation.replace(/^[Ss]/, '');
				// Accept "p. 3", "p.3", or plain "3" after the colon
				const colonIdx = normalised.indexOf(':');
				const sourceNum = parseInt(colonIdx >= 0 ? normalised.slice(0, colonIdx) : normalised, 10);
				const pageRaw = colonIdx >= 0 ? normalised.slice(colonIdx + 1).replace(/^p\.?\s*/i, '') : null;
				const page = pageRaw ? parseInt(pageRaw, 10) : null;

				// Look up source metadata
				const source = this.lookupSource(sourceNum, sources);
				if (!source) {
					// If source not found, keep original citation
					citationSpans.push(`[${citation}]`);
					continue;
				}

				// Get library type
				const libraryType = this.getLibraryType(source.library_id, libraryMap);

				// Get Zotero library ID to fetch item metadata
				const zoteroLibraryID = this.getZoteroLibraryID(source.library_id, libraryType);
				let displayText = source.title || 'Unknown';
				let item = null;
				if (zoteroLibraryID !== null) {
					item = this.getZoteroItem(source.item_id, zoteroLibraryID);
					displayText = this.formatCitationDisplayText(item);
				}

				// Build zotero://open-pdf/ URI using the LLM-cited page when available,
				// falling back to the chunk's stored page_number.
				const uri = this.buildZoteroPDFURI(source, libraryType, item, page);

				// Generate citation HTML (page unquoted; text_anchor quoted only when no page)
				const citationHTML = this.formatCitationHTML(uri, displayText, page, source.text_anchor);
				citationSpans.push(citationHTML);
			}

			// Join multiple citations with space
			return citationSpans.join(' ');
		});
	}

	/**
	 * Format a bibliography entry label for a Zotero item.
	 * Returns "LastName [et al.] (Year) \"Title\"" or a fallback.
	 * @param {*} item - Zotero item (may be null)
	 * @param {SourceCitation} source - Source citation fallback data
	 * @returns {string} Formatted bibliography label (plain text, not HTML-escaped)
	 */
	formatBibliographyLabel(item, source) {
		if (!item) {
			return source.title || 'Unknown';
		}
		try {
			const creators = item.getCreators();
			let authorPart = '';
			if (creators && creators.length > 0) {
				const first = creators[0];
				authorPart = first.lastName || first.name || first.firstName || '';
				if (creators.length > 1) {
					authorPart += ' et al.';
				}
			}
			const date = item.getField('date');
			let year = '';
			if (date) {
				const m = date.match(/\b(\d{4})\b/);
				if (m) year = m[1];
			}
			const title = item.getField('title') || source.title || '';
			const authorYear = [authorPart, year ? `(${year})` : ''].filter(Boolean).join(' ');
			return title ? `${authorYear} "${title}"` : authorYear || title;
		} catch (e) {
			this.log(`Error formatting bibliography label: ${e}`);
			return source.title || 'Unknown';
		}
	}

	/**
	 * Return the sort key (lowercase last name of first author) for a Zotero item.
	 * @param {*} item - Zotero item (may be null)
	 * @param {SourceCitation} source - Fallback source
	 * @returns {string}
	 */
	bibliographySortKey(item, source) {
		if (!item) return (source.title || '').toLowerCase();
		try {
			const creators = item.getCreators();
			if (creators && creators.length > 0) {
				const first = creators[0];
				return (first.lastName || first.name || first.firstName || '').toLowerCase();
			}
		} catch (_) { /* ignore */ }
		return (source.title || '').toLowerCase();
	}

	/**
	 * Build an HTML bibliography section for all sources in the result.
	 * Entries are sorted by first-author last name and linked via zotero://select/.
	 * @param {Array<SourceCitation>} sources - Source citations
	 * @param {Map<string, {name: string, type: 'user'|'group'}>} libraryMap - Library info map
	 * @returns {string} HTML string for the bibliography section
	 */
	formatBibliographyHTML(sources, libraryMap) {
		if (!sources || sources.length === 0) return '';

		// Deduplicate by item_id
		/** @type {Map<string, SourceCitation>} */
		const seen = new Map();
		for (const source of sources) {
			if (!seen.has(source.item_id)) {
				seen.set(source.item_id, source);
			}
		}
		const unique = Array.from(seen.values());

		// Enrich with Zotero item data
		const entries = unique.map(source => {
			const libraryType = this.getLibraryType(source.library_id, libraryMap);
			const zoteroLibraryID = this.getZoteroLibraryID(source.library_id, libraryType);
			const item = zoteroLibraryID !== null
				? this.getZoteroItem(source.item_id, zoteroLibraryID)
				: null;
			const uri = this.buildZoteroPDFURI(source, libraryType, item);
			const label = this.formatBibliographyLabel(item, source);
			const sortKey = this.bibliographySortKey(item, source);
			return { uri, label, sortKey };
		});

		// Sort by first-author last name
		entries.sort((a, b) => a.sortKey.localeCompare(b.sortKey));

		let html = `<hr/><p><strong>References</strong></p><ul>`;
		for (const entry of entries) {
			html += `<li><a href="${entry.uri}">${this.escapeHTML(entry.label)}</a></li>`;
		}
		html += `</ul>`;
		return html;
	}

	/**
	 * Format the query result as HTML for the note.
	 * @param {string} question - Original question
	 * @param {QueryResult} result - Query result
	 * @param {Array<string>} libraryIDs - Libraries that were queried
	 * @returns {string} HTML content
	 */
	formatNoteHTML(question, result, libraryIDs) {
		const timestamp = new Date().toLocaleString();

		// Build map of library ID to library info for source URI generation
		/**
		 * @typedef {Object} LibraryInfo
		 * @property {string} name - Library name
		 * @property {'user'|'group'} type - Library type
		 */

		/** @type {Map<string, LibraryInfo>} */
		const libraryMap = new Map();

		for (let id of libraryIDs) {
			const libraries = this.getLibraries();
			const lib = libraries.find((/** @type {Library} */ l) => l.id === id);
			if (lib) {
				libraryMap.set(id, {
					name: lib.name,
					type: lib.type
				});
			}
		}

		const libraryNames = Array.from(libraryMap.values()).map(info => info.name).join(', ');

		let html = `<div>`;
		html += `<h2>${this.escapeHTML(question)}</h2>`;
		html += `<p><strong>Answer:</strong></p>`;

		// Process answer text to replace inline citations
		let answerHTML = '';
		if (result.answer_format === 'html') {
			answerHTML = this.replaceCitationsInText(result.answer, result.sources || [], libraryMap);
		} else {
			const escapedAnswer = this.escapeHTML(result.answer);
			answerHTML = `<p>${this.replaceCitationsInText(escapedAnswer, result.sources || [], libraryMap)}</p>`;
		}
		html += answerHTML;

		// Add bibliography
		html += this.formatBibliographyHTML(result.sources || [], libraryMap);

		// Add metadata
		html += `<hr/>`;
		html += `<p style="font-size: 0.9em; color: #666;">`;
		html += `<em>Generated: ${timestamp}<br/>`;
		html += `Libraries: ${this.escapeHTML(libraryNames)}</em>`;
		html += `</p>`;
		html += `</div>`;

		return html;
	}

	// ── Fix Unavailable Attachments ──────────────────────────────────────────

	/**
	 * Open the fix-unavailable-attachments dialog.
	 * @param {Window} win - Parent Zotero main window
	 * @returns {void}
	 */
	/**
	 * @param {Window} win - Zotero main window
	 * @param {number} [libraryID] - Library to show; defaults to the currently selected library
	 */
	openFixUnavailableDialog(win, libraryID) {
		// If the dialog is already open, bring it to the front instead of opening a new one.
		if (this._fixUnavailableWindow && !this._fixUnavailableWindow.closed) {
			this._fixUnavailableWindow.focus();
			return;
		}
		if (!libraryID) {
			const pane = Zotero.getActiveZoteroPane();
			libraryID = (pane ? pane.getSelectedLibraryID() : null) ?? Zotero.Libraries.userLibraryID;
		}
		// @ts-ignore - openDialog is available in XUL/Firefox extension context
		this._fixUnavailableWindow = win.openDialog(
			'chrome://zotero-rag/content/fix-unavailable.xhtml',
			'zotero-rag-fix-unavailable',
			'chrome,centerscreen,resizable=yes,width=720,height=520',
			{ plugin: this, libraryID }
		);
	}

	/**
	 * Scan the current library for unavailable attachments and update the toolbar label.
	 * @param {Window} win - Zotero main window containing the label
	 * @returns {Promise<void>}
	 */
	async _scanUnavailableCount(win) {
		const btn = /** @type {any} */ (win.document.getElementById('zotero-rag-unavailable-btn'));
		const badge = /** @type {any} */ (win.document.getElementById('zotero-rag-unavailable-badge'));
		if (!btn || !badge) return;
		const pane = Zotero.getActiveZoteroPane();
		if (!pane) return;
		const libraryID = pane.getSelectedLibraryID();
		if (!libraryID) {
			btn.setAttribute('hidden', 'true');
			return;
		}
		try {
			const count = await this._countUnavailableInLibrary(libraryID);
			if (count > 0) {
				badge.setAttribute('value', String(count));
				btn.removeAttribute('hidden');
			} else {
				btn.setAttribute('hidden', 'true');
			}
		} catch (e) {
			this.log(`[_scanUnavailableCount] error: ${e}`);
			btn.setAttribute('hidden', 'true');
		}
	}

	/**
	 * Count imported attachments in a library whose file is missing locally.
	 * Checks all imported-file attachments regardless of sync state.
	 * @param {number} libraryID - Zotero internal library ID
	 * @returns {Promise<number>}
	 */
	async _countUnavailableInLibrary(libraryID) {
		// linkMode 0 = imported_file, 1 = imported_url (both stored in Zotero storage).
		// No storageHash filter — files added without going through sync have no hash.
		// Exclude attachments and their parent items that are in the trash (deletedItems).
		const sql = `
			SELECT ia.itemID FROM itemAttachments ia
			JOIN items i ON i.itemID = ia.itemID
			WHERE i.libraryID = ?
			AND ia.linkMode IN (0, 1, 2)
			AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
			AND (ia.parentItemID IS NULL OR ia.parentItemID NOT IN (SELECT itemID FROM deletedItems))
		`;
		// @ts-ignore - Zotero.DB exists at runtime
		const ids = await Zotero.DB.columnQueryAsync(sql, [libraryID]);
		if (!ids || ids.length === 0) return 0;
		const items = /** @type {any[]} */ (/** @type {unknown} */ (await Zotero.Items.getAsync(ids)));
		let count = 0;
		for (const item of items) {
			try {
				if (!(await item.fileExists())) count++;
			} catch (_) {
				count++; // unrecognized path (e.g. Windows UNC on Mac) → treat as missing
			}
		}
		// Keep the prefs-based cache in sync so the RAG dialog doesn't show stale counts.
		this._syncUnavailableCountToPrefs(libraryID, count);
		return count;
	}

	/**
	 * Write the live unavailable count into prefs and refresh any open RAG dialog.
	 * @param {number} libraryID
	 * @param {number} count
	 */
	_syncUnavailableCountToPrefs(libraryID, count) {
		const prefKey = `extensions.zotero-rag.unavailableItems.${libraryID}`;
		try {
			// @ts-ignore
			const cached = parseInt(Zotero.Prefs.get(prefKey, true) || '0') || 0;
			if (cached === count) return; // nothing changed
			// @ts-ignore
			Zotero.Prefs.set(prefKey, count, true);
			// Push update into the open RAG dialog if it's showing this library.
			if (this._dialogWindow && !this._dialogWindow.closed) {
				const dlg = /** @type {any} */ (this._dialogWindow).ZoteroRAGDialog;
				if (dlg && typeof dlg.onUnavailableCountUpdated === 'function') {
					dlg.onUnavailableCountUpdated(String(libraryID), count);
				}
			}
		} catch (_) {}
	}

	/**
	 * Clear the missing-files count for a library and notify the open RAG dialog.
	 * Called when the fix-unavailable dialog confirms there are no missing files.
	 * @param {number} libraryID
	 */
	clearMissingFilesCount(libraryID) {
		try {
			// @ts-ignore
			Zotero.Prefs.set(`extensions.zotero-rag.missingFiles.${libraryID}`, 0, true);
			if (this._dialogWindow && !this._dialogWindow.closed) {
				const dlg = /** @type {any} */ (this._dialogWindow).ZoteroRAGDialog;
				if (dlg && typeof dlg.onUnavailableCountUpdated === 'function') {
					dlg.onUnavailableCountUpdated(String(libraryID), 0);
				}
			}
		} catch (_) {}
	}

	/**
	 * @typedef {Object} UnavailableAttachmentInfo
	 * @property {*} parentItem - Parent Zotero item
	 * @property {*} attachmentItem - Attachment Zotero item
	 * @property {string} authors - Comma-separated author last names
	 * @property {string} year - Publication year (4 digits) or empty string
	 * @property {string} title - Item title
	 * @property {string} zoteroID - Parent item key
	 * @property {boolean} isLinked - True for linked files (linkMode=2); can't be auto-downloaded
	 */

	/**
	 * Return full detail records for all unavailable attachments in a library.
	 * @param {number} libraryID - Zotero internal library ID
	 * @returns {Promise<Array<UnavailableAttachmentInfo>>}
	 */
	async _getUnavailableAttachments(libraryID) {
		const sql = `
			SELECT ia.itemID FROM itemAttachments ia
			JOIN items i ON i.itemID = ia.itemID
			WHERE i.libraryID = ?
			AND ia.linkMode IN (0, 1, 2)
			AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
			AND (ia.parentItemID IS NULL OR ia.parentItemID NOT IN (SELECT itemID FROM deletedItems))
		`;
		// @ts-ignore - Zotero.DB exists at runtime
		const ids = await Zotero.DB.columnQueryAsync(sql, [libraryID]);
		if (!ids || ids.length === 0) return [];
		const attachments = /** @type {any[]} */ (/** @type {unknown} */ (await Zotero.Items.getAsync(ids)));
		/** @type {Array<UnavailableAttachmentInfo>} */
		const result = [];
		for (const attachment of attachments) {
			let exists = false;
			try {
				exists = await attachment.fileExists();
			} catch (_) {
				// fileExists() throws for paths it can't parse (e.g. Windows UNC on Mac)
				exists = false;
			}
			if (exists) continue;
			if (!attachment.parentItemID) continue;
			const parentItem = /** @type {any} */ (await Zotero.Items.getAsync(attachment.parentItemID));
			if (!parentItem) continue;
			const creators = parentItem.getCreators();
			// getCreators() returns {creatorTypeID, firstName, lastName, name, fieldMode}
			// — no creatorType string, so filter by all creators (not just 'author')
			const authors = creators
				.map((/** @type {any} */ c) => c.lastName || c.name || '')
				.filter(/** @type {(s: string) => boolean} */ (s) => s.length > 0)
				.join(', ');
			const dateField = parentItem.getField('date') || '';
			const yearMatch = dateField.match(/\b(\d{4})\b/);
			result.push({
				parentItem,
				attachmentItem: attachment,
				authors,
				year: yearMatch ? yearMatch[1] : '',
				title: parentItem.getField('title') || '',
				zoteroID: parentItem.key,
				isLinked: (attachment.attachmentLinkMode ?? 0) === 2,
			});
		}
		return result;
	}

	/**
	 * Try to download an attachment via Zotero's sync runner (WebDAV or Zotero Storage).
	 * @param {*} attachmentItem
	 * @returns {Promise<{downloaded: boolean, reason?: string}>}
	 */
	async _tryDownloadAttachment(attachmentItem) {
		// @ts-ignore - Zotero.Sync.Storage.Local exists at runtime
		if (!Zotero.Sync.Storage.Local.getEnabledForLibrary(attachmentItem.libraryID)) {
			return { downloaded: false, reason: 'sync-disabled' };
		}
		try {
			// @ts-ignore - Zotero.Sync.Runner exists at runtime
			await Zotero.Sync.Runner.downloadFile(attachmentItem);
			if (await attachmentItem.fileExists()) {
				return { downloaded: true };
			}
			return { downloaded: false, reason: 'still-missing' };
		} catch (e) {
			return { downloaded: false, reason: e instanceof Error ? e.message : String(e) };
		}
	}

	/**
	 * Search all libraries / online sources for the missing attachment file and fix it.
	 * Strategies attempted in order:
	 *   1. MD5 hash match across all libraries (Zotero File Storage only)
	 *   2. Filename match across all libraries
	 *   3. owl:sameAs relation — copy from a linked item in another library
	 *   4. Direct URL download — re-fetch the attachment's stored URL
	 *   5. Zotero resolver — DOI / OA lookup via Find-Available-File pipeline
	 * @param {*} attachmentItem
	 * @returns {Promise<{found: boolean, via: string, error?: string}>}
	 */
	async _searchAndFixUnavailableAttachment(attachmentItem) {
		const key = attachmentItem.key;
		const parentItem = attachmentItem.parentItemID
			? /** @type {any} */ (await Zotero.Items.getAsync(attachmentItem.parentItemID))
			: null;

		// Strategy 1: MD5 storageHash (Zotero File Storage only)
		const hash = attachmentItem.attachmentSyncedHash;
		if (hash) {
			this.log(`[fix] ${key}: trying MD5 hash ${hash}`);
			// @ts-ignore
			const ids = await Zotero.DB.columnQueryAsync(
				`SELECT itemID FROM itemAttachments WHERE storageHash = ? AND itemID != ?`,
				[hash, attachmentItem.id]
			);
			this.log(`[fix] ${key}: MD5 candidates: ${ids ? ids.length : 0}`);
			const result = await this._tryCopyFromCandidates(ids, attachmentItem, 'md5');
			if (result) { this.log(`[fix] ${key}: fixed via md5`); return result; }
		} else {
			this.log(`[fix] ${key}: no storageHash, skipping MD5`);
		}

		// Strategy 2: filename match across all libraries
		const filename = attachmentItem.attachmentFilename;
		if (filename) {
			this.log(`[fix] ${key}: trying filename match "${filename}"`);
			// @ts-ignore
			const ids = await Zotero.DB.columnQueryAsync(
				`SELECT itemID FROM itemAttachments WHERE path = ? AND itemID != ?`,
				[`storage:${filename}`, attachmentItem.id]
			);
			this.log(`[fix] ${key}: filename candidates: ${ids ? ids.length : 0}`);
			const result = await this._tryCopyFromCandidates(ids, attachmentItem, 'filename');
			if (result) { this.log(`[fix] ${key}: fixed via filename`); return result; }
		}

		// Strategy 3: owl:sameAs relation
		if (parentItem) {
			this.log(`[fix] ${key}: trying owl:sameAs relations`);
			const result = await this._tryFixViaRelations(attachmentItem, parentItem);
			if (result) { this.log(`[fix] ${key}: fixed via owl:sameAs`); return result; }
		}

		// Strategy 4: Direct URL re-download
		const url = attachmentItem.getField('url');
		this.log(`[fix] ${key}: trying direct URL download (url=${url || 'none'})`);
		const result4 = await this._tryFixViaDirectURL(attachmentItem);
		if (result4) { this.log(`[fix] ${key}: fixed via direct URL`); return result4; }

		// Strategy 5: Zotero resolver (DOI / OA lookup)
		if (parentItem) {
			const doi = parentItem.getField('DOI') || parentItem.getExtraField('DOI');
			this.log(`[fix] ${key}: trying Zotero resolver (DOI=${doi || 'none'})`);
			const result = await this._tryFixViaResolver(attachmentItem, parentItem);
			if (result) { this.log(`[fix] ${key}: fixed via resolver`); return result; }
		}

		this.log(`[fix] ${key}: all strategies exhausted — not found`);
		return { found: false, via: 'not-found' };
	}

	/**
	 * Strategy 3: copy from a linked item (owl:sameAs) in any library.
	 * @param {*} attachmentItem
	 * @param {*} parentItem
	 * @returns {Promise<{found: boolean, via: string, error?: string}|null>}
	 */
	async _tryFixViaRelations(attachmentItem, parentItem) {
		try {
			// @ts-ignore
			const predicate = Zotero.Relations.linkedObjectPredicate; // 'owl:sameAs'
			const uris = parentItem.getRelationsByPredicate(predicate);
			for (const uri of uris) {
				// @ts-ignore
				const relatedItem = await Zotero.URI.getURIItem(uri);
				if (!relatedItem || relatedItem.deleted) continue;
				for (const attID of relatedItem.getAttachments()) {
					const att = /** @type {any} */ (Zotero.Items.get(attID));
					if (!att) continue;
					if (att.attachmentContentType !== attachmentItem.attachmentContentType) continue;
					if (!(await att.fileExists())) continue;
					const sourcePath = await att.getFilePathAsync();
					if (!sourcePath) continue;
					try {
						await this._copyAttachmentFile(attachmentItem, sourcePath);
						return { found: true, via: 'owl:sameAs' };
					} catch (e) {
						return { found: true, via: 'owl:sameAs', error: e instanceof Error ? e.message : String(e) };
					}
				}
			}
		} catch (e) {
			this.log(`_tryFixViaRelations error: ${e}`);
		}
		return null;
	}

	/**
	 * Strategy 4: Re-download from the attachment's stored URL via Zotero.HTTP.request.
	 * @param {*} attachmentItem
	 * @returns {Promise<{found: boolean, via: string, error?: string}|null>}
	 */
	async _tryFixViaDirectURL(attachmentItem) {
		const url = attachmentItem.getField('url');
		if (!url) return null;
		this.log(`[fix] ${attachmentItem.key}: downloading from URL: ${url}`);
		try {
			// @ts-ignore - Zotero.HTTP exists at runtime
			const req = await Zotero.HTTP.request('GET', url, { responseType: 'blob' });
			if (!req || req.status !== 200 || !req.response) return null;
			// Write blob to a temp file
			// @ts-ignore - Zotero.getTempDirectory() exists at runtime
			const tmpDir = /** @type {any} */ (Zotero.getTempDirectory()).path;
			// @ts-ignore
			const tmpPath = PathUtils.join(tmpDir, `zotero-rag-dl-${Date.now()}`);
			const buf = await req.response.arrayBuffer();
			// @ts-ignore
			await IOUtils.write(tmpPath, new Uint8Array(buf));
			try {
				await this._copyAttachmentFile(attachmentItem, tmpPath);
				return { found: true, via: 'url' };
			} catch (e) {
				return { found: true, via: 'url', error: e instanceof Error ? e.message : String(e) };
			} finally {
				// @ts-ignore
				await IOUtils.remove(tmpPath).catch(() => {});
			}
		} catch (e) {
			this.log(`_tryFixViaDirectURL error: ${e}`);
			return null;
		}
	}

	/**
	 * Strategy 5: Use Zotero's Find-Available-File resolver (DOI/OA/URL lookup).
	 * Bypasses canFindFileForItem() so it works even when the attachment item already exists.
	 * @param {*} attachmentItem
	 * @param {*} parentItem
	 * @returns {Promise<{found: boolean, via: string, error?: string}|null>}
	 */
	async _tryFixViaResolver(attachmentItem, parentItem) {
		try {
			// @ts-ignore
			const resolvers = Zotero.Attachments.getFileResolvers(parentItem);
			if (!resolvers || resolvers.length === 0) {
				this.log(`[fix] ${attachmentItem.key}: resolver: no resolvers available`);
				return null;
			}
			this.log(`[fix] ${attachmentItem.key}: resolver: ${resolvers.length} resolver(s) found`);
			// @ts-ignore
			const tmpDir = (await Zotero.Attachments.createTemporaryStorageDirectory()).path;
			// @ts-ignore
			const tmpPath = PathUtils.join(tmpDir, 'file.tmp');
			try {
				// @ts-ignore
				const dl = await Zotero.Attachments.downloadFirstAvailableFile(resolvers, tmpPath, {});
				if (!dl || !dl.url) return null;
				try {
					await this._copyAttachmentFile(attachmentItem, tmpPath);
					return { found: true, via: 'resolver' };
				} catch (e) {
					return { found: true, via: 'resolver', error: e instanceof Error ? e.message : String(e) };
				}
			} finally {
				// @ts-ignore
				await IOUtils.remove(tmpDir, { recursive: true }).catch(() => {});
			}
		} catch (e) {
			this.log(`_tryFixViaResolver error: ${e}`);
			return null;
		}
	}

	/**
	 * Try each candidate attachment ID; copy the first one that has a local file.
	 * @param {number[]|null} ids
	 * @param {*} attachmentItem
	 * @param {string} via
	 * @returns {Promise<{found: boolean, via: string, sourcePath?: string, error?: string}|null>}
	 */
	async _tryCopyFromCandidates(ids, attachmentItem, via) {
		if (!ids || ids.length === 0) return null;
		for (const id of ids) {
			const candidate = /** @type {any} */ (Zotero.Items.get(id));
			if (!candidate) continue;
			if (!(await candidate.fileExists())) continue;
			const sourcePath = await candidate.getFilePathAsync();
			if (!sourcePath) continue;
			try {
				await this._copyAttachmentFile(attachmentItem, sourcePath);
				return { found: true, via, sourcePath };
			} catch (e) {
				return { found: true, via, sourcePath, error: e instanceof Error ? e.message : String(e) };
			}
		}
		return null;
	}

	/**
	 * Copy a file into the Zotero storage directory for a missing attachment.
	 * @param {*} attachmentItem - The attachment item whose file is missing
	 * @param {string} sourcePath - Full path of the source file to copy
	 * @returns {Promise<void>}
	 */
	async _copyAttachmentFile(attachmentItem, sourcePath) {
		// @ts-ignore - Zotero.Attachments exists at runtime
		const storageDir = Zotero.Attachments.getStorageDirectory(attachmentItem);
		// @ts-ignore - IOUtils is a global in Firefox/Zotero
		await IOUtils.makeDirectory(storageDir.path, { createAncestors: true, ignoreExisting: true });
		const filename = attachmentItem.attachmentFilename
			// @ts-ignore - PathUtils is a global in Firefox/Zotero
			|| PathUtils.filename(sourcePath);
		// @ts-ignore
		const destPath = PathUtils.join(storageDir.path, filename);
		// @ts-ignore
		await IOUtils.copy(sourcePath, destPath);
		if (!(await attachmentItem.fileExists())) {
			throw new Error('File was copied but attachment still reports missing');
		}
	}

	// ─────────────────────────────────────────────────────────────────────────

	/**
	 * Escape HTML special characters.
	 * @param {string} text - Text to escape
	 * @returns {string} Escaped text
	 */
	escapeHTML(text) {
		/** @type {Record<string, string>} */
		const map = {
			'&': '&amp;',
			'<': '&lt;',
			'>': '&gt;',
			'"': '&quot;',
			"'": '&#039;'
		};
		return text.replace(/[&<>"']/g, m => map[m]);
	}
}

// Assign singleton instance to the global variable declared in bootstrap.js.
// Consumers (bootstrap.js, dialog.js) continue to use ZoteroRAG unchanged.
ZoteroRAG = new ZoteroRAGPlugin();
