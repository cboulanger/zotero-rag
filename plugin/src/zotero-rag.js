ZoteroRAG = {
	id: null,
	version: null,
	rootURI: null,
	initialized: false,
	addedElementIDs: [],

	// Backend connection
	backendURL: null,

	// Active queries tracking
	activeQueries: new Set(),
	maxConcurrentQueries: 5,

	init({ id, version, rootURI }) {
		if (this.initialized) return;
		this.id = id;
		this.version = version;
		this.rootURI = rootURI;
		this.initialized = true;

		// Load backend URL from preferences (default: localhost:8119)
		this.backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || 'http://localhost:8119';
	},

	log(msg) {
		Zotero.debug("Zotero RAG: " + msg);
	},

	addToWindow(window) {
		let doc = window.document;

		// Add menu item under Tools menu
		// Note: Menu items still need to use XUL elements as they integrate with Zotero's existing XUL menus
		let menuitem = doc.createXULElement('menuitem');
		menuitem.id = 'zotero-rag-ask-question';
		menuitem.setAttribute('label', 'Zotero RAG: Ask Question...');
		menuitem.addEventListener('command', () => {
			this.openQueryDialog(window);
		});

		// Add to Tools menu
		let toolsMenu = doc.getElementById('menu_ToolsPopup');
		if (toolsMenu) {
			toolsMenu.appendChild(menuitem);
			this.storeAddedElement(menuitem);
		}
	},

	addToAllWindows() {
		var windows = Zotero.getMainWindows();
		for (let win of windows) {
			if (!win.ZoteroPane) continue;
			this.addToWindow(win);
		}
	},

	storeAddedElement(elem) {
		if (!elem.id) {
			throw new Error("Element must have an id");
		}
		this.addedElementIDs.push(elem.id);
	},

	removeFromWindow(window) {
		var doc = window.document;
		// Remove all elements added to DOM
		for (let id of this.addedElementIDs) {
			doc.getElementById(id)?.remove();
		}
		doc.querySelector('[href="zotero-rag.ftl"]')?.remove();
	},

	removeFromAllWindows() {
		var windows = Zotero.getMainWindows();
		for (let win of windows) {
			if (!win.ZoteroPane) continue;
			this.removeFromWindow(win);
		}
	},

	async main() {
		this.log(`Plugin initialized with backend URL: ${this.backendURL}`);

		// Check backend version compatibility
		try {
			await this.checkBackendVersion();
		} catch (e) {
			this.log(`Backend not available: ${e.message}`);
		}
	},

	/**
	 * Check backend version for compatibility
	 */
	async checkBackendVersion() {
		try {
			const response = await fetch(`${this.backendURL}/api/version`);
			if (!response.ok) {
				throw new Error(`HTTP ${response.status}`);
			}
			const data = await response.json();
			this.log(`Backend version: ${data.version}`);

			// TODO: Add version compatibility checking
			return data.version;
		} catch (e) {
			throw new Error(`Failed to check backend version: ${e.message}`);
		}
	},

	/**
	 * Open the query dialog
	 */
	openQueryDialog(window) {
		// Check concurrent query limit
		if (this.activeQueries.size >= this.maxConcurrentQueries) {
			this.showError(window, `Maximum concurrent queries (${this.maxConcurrentQueries}) reached. Please wait for existing queries to complete.`);
			return;
		}

		// Open dialog window using chrome:// URL
		const dialogURL = 'chrome://zotero-rag/content/dialog.xhtml';
		const dialogFeatures = 'chrome,centerscreen,modal,resizable=yes,width=600,height=500';

		window.openDialog(
			dialogURL,
			'zotero-rag-dialog',
			dialogFeatures,
			{ plugin: this }
		);
	},

	/**
	 * Show error message to user
	 */
	showError(window, message) {
		const prompts = Components.classes["@mozilla.org/embedcomp/prompt-service;1"]
			.getService(Components.interfaces.nsIPromptService);
		prompts.alert(window, "Zotero RAG Error", message);
	},

	/**
	 * Get all available libraries
	 */
	getLibraries() {
		const libraries = [];
		const userLibraryID = Zotero.Libraries.userLibraryID;
		const userLibrary = Zotero.Libraries.get(userLibraryID);

		libraries.push({
			id: userLibraryID.toString(),
			name: userLibrary.name || 'My Library',
			type: 'user'
		});

		// Get all group libraries
		const groups = Zotero.Groups.getAll();
		for (let group of groups) {
			libraries.push({
				id: group.libraryID.toString(),
				name: group.name,
				type: 'group'
			});
		}

		return libraries;
	},

	/**
	 * Get currently selected library/collection
	 */
	getCurrentLibrary() {
		const zoteroPane = Zotero.getActiveZoteroPane();
		if (!zoteroPane) return null;

		const libraryID = zoteroPane.getSelectedLibraryID();
		return libraryID ? libraryID.toString() : null;
	},

	/**
	 * Submit a query to the backend
	 */
	async submitQuery(question, libraryIDs, options = {}) {
		const queryId = Date.now().toString();
		this.activeQueries.add(queryId);

		try {
			// Submit query directly - the backend should be available if indexing succeeded
			const response = await fetch(`${this.backendURL}/api/query`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json'
				},
				body: JSON.stringify({
					question,
					library_ids: libraryIDs,
					top_k: options.topK || 5
				})
			});

			if (!response.ok) {
				const errorData = await response.json().catch(() => ({}));
				throw new Error(errorData.detail || `Query failed with HTTP ${response.status}`);
			}

			const result = await response.json();
			return result;
		} finally {
			this.activeQueries.delete(queryId);
		}
	},

	/**
	 * Create a note in the current collection with the query result
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
		note.libraryID = libraryID;

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
	 * Format the query result as HTML for the note
	 */
	formatNoteHTML(question, result, libraryIDs) {
		const timestamp = new Date().toLocaleString();
		const libraries = libraryIDs.map(id => {
			const lib = Zotero.Libraries.get(parseInt(id));
			return lib.name || 'My Library';
		}).join(', ');

		let html = `<div>`;
		html += `<h2>${this.escapeHTML(question)}</h2>`;
		html += `<p><strong>Answer:</strong></p>`;
		html += `<p>${this.escapeHTML(result.answer)}</p>`;

		// Add sources/citations
		if (result.sources && result.sources.length > 0) {
			html += `<p><strong>Sources:</strong></p>`;
			html += `<ul>`;
			for (let source of result.sources) {
				const link = `zotero://select/library/items/${source.item_id}`;
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
		html += `Libraries: ${this.escapeHTML(libraries)}</em>`;
		html += `</p>`;
		html += `</div>`;

		return html;
	},

	/**
	 * Escape HTML special characters
	 */
	escapeHTML(text) {
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
