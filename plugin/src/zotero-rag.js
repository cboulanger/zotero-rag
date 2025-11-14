// @ts-check
/// <reference path="./zotero-types.d.ts" />
/// <reference path="./toolkit.d.ts" />

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
 * Main plugin object for Zotero RAG integration.
 */
ZoteroRAG = {
	/** @type {string|null} */
	id: null,

	/** @type {string|null} */
	version: null,

	/** @type {string|null} */
	rootURI: null,

	/** @type {boolean} */
	initialized: false,

	/** @type {Array<string>} */
	addedElementIDs: [],

	/** @type {string|null} */
	backendURL: null,

	/** @type {Set<string>} */
	activeQueries: new Set(),

	/** @type {number} */
	maxConcurrentQueries: 5,

	/** @type {import('./toolkit.d.ts').Toolkit|null} */
	toolkit: null,

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
		if (typeof ZoteroPluginToolkit !== 'undefined') {
			this.toolkit = ZoteroPluginToolkit.createToolkit({ id, version, rootURI });
			this.log('Toolkit initialized successfully');
		} else {
			this.log('WARNING: Toolkit bundle not loaded');
		}

		// Load backend URL from preferences (default: localhost:8119)
		this.backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || 'http://localhost:8119';
	},

	/**
	 * Log a debug message.
	 * @param {string} msg - Message to log
	 * @returns {void}
	 */
	log(msg) {
		Zotero.debug("Zotero RAG: " + msg);
	},

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
	},

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
	},

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
	},

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
	},

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
	},

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
		}
	},

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
			const response = await fetch(`${this.backendURL}/api/version`);
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			const data = /** @type {BackendVersion} */ (await response.json());
			this.log(`Backend version: ${data.api_version}`);

			// TODO: Add version compatibility checking
			return data.api_version;
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			throw new Error(`Failed to check backend version: ${errorMessage}`);
		}
	},

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

		// Check backend connectivity before opening dialog
		try {
			await this.checkBackendVersion();
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			this.showError(
				`Cannot connect to backend server!\n\n${errorMessage}\n\nPlease start the server:\n  npm run server:start\n\nDefault URL: ${this.backendURL || 'http://localhost:8119'}`
			);
			return;
		}

		// Open dialog window using chrome:// URL
		const dialogURL = 'chrome://zotero-rag/content/dialog.xhtml';
		const dialogFeatures = 'chrome,centerscreen,modal,resizable=yes,width=600,height=500';

		// @ts-ignore - openDialog is available in XUL/Firefox extension context
		window.openDialog(
			dialogURL,
			'zotero-rag-dialog',
			dialogFeatures,
			{ plugin: this }
		);
	},

	/**
	 * Show error message to user.
	 * @param {string} message - Error message
	 * @returns {void}
	 */
	showError(message) {
		this.toolkit.showError(message);
	},

	/**
	 * Get all available libraries.
	 * @returns {Array<Library>} List of libraries
	 */
	getLibraries() {
		/** @type {Array<Library>} */
		const libraries = [];
		const userLibraryID = Zotero.Libraries.userLibraryID;
		const userLibrary = Zotero.Libraries.get(userLibraryID);

		libraries.push({
			id: String(userLibraryID),
			name: userLibrary.name || 'My Library',
			type: /** @type {'user'} */ ('user')
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
	},

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
		if (library && library.libraryType === 'group') {
			// Get the group associated with this library
			const group = Zotero.Groups.getByLibraryID(libraryID);
			if (group) {
				return String(group.id);  // Return group ID for backend
			}
		}

		// For user library, return the library ID as-is
		return String(libraryID);
	},

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
				headers: {
					'Content-Type': 'application/json'
				},
				body: JSON.stringify(payload)
			});

			if (!response.ok) {
				const errorData = await response.json().catch(() => ({}));
				throw new Error(errorData.detail || `Query failed with HTTP ${response.status}`);
			}

			const result = /** @type {QueryResult} */ (await response.json());
			return result;
		} finally {
			this.activeQueries.delete(queryId);
		}
	},

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

		// Save note
		await note.saveTx();

		// Add to collection if one is selected
		if (collectionID) {
			const collection = await Zotero.Collections.getAsync(collectionID);
			await collection.addItem(note.id);
		}

		return note;
	},

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
			const lib = libraries.find(l => l.id === id);
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
		// Use answer directly if it's HTML, otherwise escape it
		if (result.answer_format === 'html') {
			html += result.answer;
		} else {
			html += `<p>${this.escapeHTML(result.answer)}</p>`;
		}

		// Add sources/citations
		if (result.sources && result.sources.length > 0) {
			html += `<p><strong>Sources:</strong></p>`;
			html += `<ul>`;
			for (let source of result.sources) {
				// Build Zotero URI based on library type
				const sourceLibrary = libraryMap.get(source.library_id);
				let link;

				if (sourceLibrary && sourceLibrary.type === 'group') {
					// For group items, use: zotero://select/groups/{GROUP_ID}/items/{ITEM_ID}
					link = `zotero://select/groups/${source.library_id}/items/${source.item_id}`;
				} else {
					// For user library items, use: zotero://select/library/items/{ITEM_ID}
					link = `zotero://select/library/items/${source.item_id}`;
				}

				let citation = `<a href="${link}">Source</a>`;

				// Add page number if available
				if (source.page_number) {
					citation += `, p. ${source.page_number}`;
				}
				// Add text anchor if available
				else if (source.text_anchor) {
					citation += ` (${this.escapeHTML(source.text_anchor)})`;
				}

				html += `<li>${citation}</li>`;
			}
			html += `</ul>`;
		}

		// Add metadata
		html += `<hr/>`;
		html += `<p style="font-size: 0.9em; color: #666;">`;
		html += `<em>Generated: ${timestamp}<br/>`;
		html += `Libraries: ${this.escapeHTML(libraryNames)}</em>`;
		html += `</p>`;
		html += `</div>`;

		return html;
	},

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
};
