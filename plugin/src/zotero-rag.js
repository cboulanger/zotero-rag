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
		this.backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || 'http://localhost:8119';

		// Load optional API key (required when backend is on a remote host)
		this.apiKey = Zotero.Prefs.get('extensions.zotero-rag.apiKey', true) || '';
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
		Zotero.debug("Zotero RAG: " + msg);
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
		// test
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
		const dialogFeatures = 'chrome,centerscreen,resizable=yes,width=600,height=600';

		// @ts-ignore - openDialog is available in XUL/Firefox extension context
		window.openDialog(
			dialogURL,
			'zotero-rag-dialog',
			dialogFeatures,
			{ plugin: this }
		);
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

		// For user library, return the library ID as-is
		return String(libraryID);
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
				const errorData = /** @type {any} */ (await response.json().catch(() => ({})));
				throw new Error(errorData.detail || `Query failed with HTTP ${response.status}`);
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

		// Save note
		await note.saveTx();

		// Add to collection if one is selected
		if (collectionID) {
			const collection = await Zotero.Collections.getAsync(collectionID);
			await collection.addItem(note.id);
		}

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
			// For user library, it's already the library ID
			return parseInt(backendLibraryId, 10);
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
	 * @param {number|null} [page] - Optional page number
	 * @param {string|null} [textAnchor] - Optional quote context (used only when page is absent)
	 * @returns {string} HTML anchor element
	 */
	formatCitationHTML(uri, displayText, page = null, textAnchor = null) {
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
		// Fallback: normalise "Source N", "*Source N*", "**Source N**" → [N]
		// so that LLM responses that ignored the bracket-notation instruction are still handled.
		const sourceWordPattern = /\*{0,2}Source\s+(\d+)\*{0,2}/g;
		text = text.replace(sourceWordPattern, (_m, n) => `[${n}]`);

		// Pattern: [1], [1:10], [1,2,3], [1:10,2:20,3]
		const citationPattern = /\[(\d+(?::\d+)?(?:,\s*\d+(?::\d+)?)*)\]/g;

		return text.replace(citationPattern, (_match, citationList) => {
			// Parse comma-separated citations
			const citations = citationList.split(/,\s*/);
			const citationSpans = [];

			for (let citation of citations) {
				// Parse source number and optional page
				const parts = citation.split(':');
				const sourceNum = parseInt(parts[0], 10);
				const page = parts.length > 1 ? parseInt(parts[1], 10) : null;

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

		let html = `<hr/><p><strong>Sources</strong></p><ul>`;
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
