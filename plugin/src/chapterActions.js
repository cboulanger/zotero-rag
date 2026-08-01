// plugin/src/chapterActions.js
// "Zotero RAG: Tools" per-item menu actions: Segment Book (client-side
// eligibility check, then the existing analyze/segment-upload backend
// pipeline scoped to one book, then opens /admin/review) and Match Chapter
// (fully client-side book-candidate search + link writing, no backend
// call). See
// docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md.
//
// Loaded by bootstrap.js after chapterLinks.js and fuzzyMatch.js, into the
// same window-scoped global environment as zotero-rag.js, so ChapterLinks
// and FuzzyMatch are available here as plain globals.

var ChapterActions = {
	/** @type {WeakMap<Window, Array<{target: EventTarget, type: string, handler: Function}>>} */
	_cleanupHandlers: new WeakMap(),

	// ---- Menu registration -------------------------------------------------

	/**
	 * @param {Document} doc
	 * @param {*} ui - plugin.toolkit.uiTool
	 * @param {string} idPrefix
	 * @param {*} plugin
	 * @param {Window} window
	 * @returns {{menu: Element, segmentItem: Element, matchItem: Element}}
	 */
	buildSubmenu(doc, ui, idPrefix, plugin, window) {
		const segmentItem = ui.createElement(doc, 'menuitem', {
			namespace: 'xul',
			id: `${idPrefix}-segment-book`,
			attributes: { label: 'Segment Book…' },
			listeners: [{ type: 'command', listener: () => this.handleSegmentBook(window, plugin) }],
		});
		const matchItem = ui.createElement(doc, 'menuitem', {
			namespace: 'xul',
			id: `${idPrefix}-match-chapter`,
			attributes: { label: 'Match Chapter…' },
			listeners: [{ type: 'command', listener: () => this.handleMatchChapter(window, plugin) }],
		});
		const menupopup = ui.createElement(doc, 'menupopup', {
			namespace: 'xul',
			id: `${idPrefix}-popup`,
		});
		menupopup.appendChild(segmentItem);
		menupopup.appendChild(matchItem);

		const menu = ui.createElement(doc, 'menu', {
			namespace: 'xul',
			id: `${idPrefix}-menu`,
			attributes: { label: 'Zotero RAG: Tools' },
		});
		menu.appendChild(menupopup);

		return { menu, segmentItem, matchItem };
	},

	/**
	 * @param {Array<*>} selectedItems
	 * @returns {{segmentEnabled: boolean, matchEnabled: boolean}}
	 */
	computeEnablement(selectedItems) {
		if (!selectedItems || selectedItems.length !== 1) {
			return { segmentEnabled: false, matchEnabled: false };
		}
		const item = selectedItems[0];
		const segmentEnabled = item.itemType === 'book';
		const matchEnabled = item.itemType === 'bookSection' && !ChapterLinks.hasContainedByLink(item);
		return { segmentEnabled, matchEnabled };
	},

	/**
	 * @param {Window} window
	 * @param {{segmentItem: Element, matchItem: Element}} refs
	 * @returns {void}
	 */
	updateMenuState(window, refs) {
		const selected = window.ZoteroPane.getSelectedItems();
		const { segmentEnabled, matchEnabled } = this.computeEnablement(selected);
		refs.segmentItem.disabled = !segmentEnabled;
		refs.matchItem.disabled = !matchEnabled;
	},

	/**
	 * @param {Window} window
	 * @param {EventTarget} target
	 * @param {string} type
	 * @param {Function} handler
	 * @returns {void}
	 */
	_registerCleanup(window, target, type, handler) {
		if (!this._cleanupHandlers.has(window)) {
			this._cleanupHandlers.set(window, []);
		}
		this._cleanupHandlers.get(window).push({ target, type, handler });
	},

	/**
	 * @param {Window} window
	 * @param {*} plugin
	 * @returns {void}
	 */
	addToWindow(window, plugin) {
		const doc = window.document;
		const ui = plugin.toolkit.uiTool;

		const itemMenuPopup = doc.getElementById('zotero-itemmenu');
		if (itemMenuPopup) {
			const { menu, segmentItem, matchItem } = this.buildSubmenu(doc, ui, 'zotero-rag-itemmenu', plugin, window);
			itemMenuPopup.appendChild(menu);
			plugin.storeAddedElement(menu);
			const handler = () => this.updateMenuState(window, { segmentItem, matchItem });
			itemMenuPopup.addEventListener('popupshowing', handler);
			this._registerCleanup(window, itemMenuPopup, 'popupshowing', handler);
		}

		const toolsMenuPopup = doc.getElementById('menu_ToolsPopup');
		if (toolsMenuPopup) {
			const { menu, segmentItem, matchItem } = this.buildSubmenu(doc, ui, 'zotero-rag-toolsmenu', plugin, window);
			toolsMenuPopup.appendChild(menu);
			plugin.storeAddedElement(menu);
			const handler = () => this.updateMenuState(window, { segmentItem, matchItem });
			toolsMenuPopup.addEventListener('popupshowing', handler);
			this._registerCleanup(window, toolsMenuPopup, 'popupshowing', handler);
		}
	},

	/**
	 * @param {Window} window
	 * @returns {void}
	 */
	removeFromWindow(window) {
		const handlers = this._cleanupHandlers.get(window) || [];
		for (const { target, type, handler } of handlers) {
			target.removeEventListener(type, handler);
		}
		this._cleanupHandlers.delete(window);
	},

	// ---- Segment Book -------------------------------------------------------

	/**
	 * @param {*} item - Zotero book item
	 * @returns {number|null} attachment item ID of the first PDF attachment, or null
	 */
	findBookPdfAttachmentID(item) {
		const attachmentIDs = item.getAttachments();
		for (const id of attachmentIDs) {
			const attachment = Zotero.Items.get(id);
			if (attachment && attachment.attachmentContentType === 'application/pdf') {
				return id;
			}
		}
		return null;
	},

	/**
	 * @param {number} attachmentID
	 * @returns {Promise<number|null>}
	 */
	async getPageCount(attachmentID) {
		const pages = await Zotero.FullText.getPages(attachmentID);
		if (pages && pages.total != null) {
			return pages.total;
		}
		const fullText = await Zotero.PDFWorker.getFullText(attachmentID, 1);
		return fullText ? fullText.totalPages : null;
	},

	/**
	 * @param {string} backendURL
	 * @param {string} path - e.g. "analyze", "segment-upload"
	 * @param {object} body
	 * @returns {Promise<string>} job_id
	 */
	async postChapterLinkingJob(backendURL, path, body) {
		const response = await fetch(`${backendURL}/api/chapter-linking/${path}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});
		if (!response.ok) {
			const errBody = await response.json().catch(() => ({}));
			const detail = typeof errBody.detail === 'string' ? errBody.detail : (errBody.detail ? JSON.stringify(errBody.detail) : null);
			throw new Error(detail || `POST /api/chapter-linking/${path}: HTTP ${response.status}`);
		}
		const data = await response.json();
		return data.job_id;
	},

	/**
	 * @param {string} backendURL
	 * @param {string} jobId
	 * @param {function(string): void} [onProgress]
	 * @returns {Promise<any>} the job's `result` field
	 */
	async pollChapterLinkingJob(backendURL, jobId, onProgress) {
		const POLL_INTERVAL_MS = 3000;
		const POLL_TIMEOUT_MS = 30 * 1000;
		while (true) {
			await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
			const response = await fetch(`${backendURL}/api/chapter-linking/jobs/${jobId}`, {
				signal: AbortSignal.timeout(POLL_TIMEOUT_MS),
			});
			if (!response.ok) {
				throw new Error(`GET /api/chapter-linking/jobs/${jobId}: HTTP ${response.status}`);
			}
			const data = await response.json();
			if (onProgress) onProgress(data.message || '');
			if (data.status === 'done') return data.result;
			if (data.status === 'error') throw new Error(data.error || 'Job failed');
			// status === 'processing' -- keep polling
		}
	},

	/**
	 * @param {Window} window
	 * @param {*} plugin
	 * @returns {Promise<void>}
	 */
	async handleSegmentBook(window, plugin) {
		const selected = window.ZoteroPane.getSelectedItems();
		if (selected.length !== 1 || selected[0].itemType !== 'book') {
			plugin.showError('Select a single book item.');
			return;
		}
		const item = selected[0];
		const attachmentID = this.findBookPdfAttachmentID(item);
		if (attachmentID == null) {
			plugin.showError('No book-type attachment found');
			return;
		}
		const pageCount = await this.getPageCount(attachmentID);
		const minPages = Zotero.Prefs.get('extensions.zotero-rag.minBookPages', true) || 50;
		if (pageCount == null || pageCount < minPages) {
			plugin.showError(`PDF has ${pageCount ?? 'an unknown number of'} pages; Segment Book requires at least ${minPages}.`);
			return;
		}

		const slug = ChapterLinks.getZoteroSlug(item.libraryID);
		const backendURL = plugin.backendURL;

		try {
			plugin.toolkit.showNotification(`Analyzing "${item.getField('title')}"…`, 'default');
			const analyzeJobId = await this.postChapterLinkingJob(backendURL, 'analyze', {
				library_slug: slug,
				api_key: plugin.zoteroApiKey,
				item_keys: [item.key],
				enable_llm_fallback: true,
			});
			const analyzeResult = await this.pollChapterLinkingJob(backendURL, analyzeJobId,
				(msg) => plugin.toolkit.showNotification(msg, 'default'));

			const attachments = (analyzeResult && analyzeResult.attachments) || [];
			if (attachments.length === 0) {
				plugin.showError('No chapters detected — this book may already be linked, or the backend could not process its attachment.');
				return;
			}

			const needsOcrAny = attachments.some(a => a.needs_ocr);
			const readyAttachments = attachments.filter(a => !a.needs_ocr);
			if (needsOcrAny) {
				plugin.toolkit.showNotification('Some attachments need OCR before chapters can be detected — queued for review.', 'default');
			}
			if (readyAttachments.length > 0) {
				const uploadJobId = await this.postChapterLinkingJob(backendURL, 'segment-upload', {
					library_slug: slug,
					api_key: plugin.zoteroApiKey,
					analyses: readyAttachments,
					committed: false,
				});
				await this.pollChapterLinkingJob(backendURL, uploadJobId,
					(msg) => plugin.toolkit.showNotification(msg, 'default'));
			}
			Zotero.launchURL(`${backendURL}/admin/review?library_slug=${encodeURIComponent(slug)}`);
		} catch (err) {
			plugin.showError(`Segment Book failed: ${err.message}`);
		}
	},

	// ---- Match Chapter --------------------------------------------------

	/**
	 * @param {string} dateStr
	 * @returns {number|null}
	 */
	extractYear(dateStr) {
		const match = /(\d{4})/.exec(dateStr || '');
		return match ? parseInt(match[1], 10) : null;
	},

	/**
	 * @param {Window} window
	 * @param {*} plugin
	 * @returns {Promise<void>}
	 */
	async handleMatchChapter(window, plugin) {
		const selected = window.ZoteroPane.getSelectedItems();
		if (selected.length !== 1 || selected[0].itemType !== 'bookSection') {
			plugin.showError('Select a single book-section item.');
			return;
		}
		const chapterItem = selected[0];
		if (ChapterLinks.hasContainedByLink(chapterItem)) {
			plugin.showError('This chapter is already linked to a book.');
			return;
		}

		try {
			const s = new Zotero.Search();
			s.libraryID = chapterItem.libraryID;
			s.addCondition('itemType', 'is', 'book');
			const ids = await s.search();
			const books = Zotero.Items.get(ids).map(b => ({
				key: b.key,
				title: b.getField('title') || '',
				year: this.extractYear(b.getField('date')),
				creators: b.getCreators().map(c => [c.firstName, c.lastName].filter(Boolean).join(' ')).join('; '),
				_item: b,
			}));

			const chapterTitle = chapterItem.getField('bookTitle') || '';
			const chapterYear = this.extractYear(chapterItem.getField('date'));
			const candidates = FuzzyMatch.rankCandidates(chapterTitle, chapterYear, books, 5);

			window.openDialog(
				'chrome://zotero-rag/content/match-chapter-dialog.xhtml',
				'zotero-rag-match-chapter',
				'chrome,centerscreen,resizable=yes,width=640,height=480',
				{ plugin, chapterItem, chapterTitle, candidates }
			);
		} catch (err) {
			plugin.showError(`Match Chapter failed: ${err.message}`);
		}
	},
};
