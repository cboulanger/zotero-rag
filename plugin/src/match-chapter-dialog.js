// plugin/src/match-chapter-dialog.js
// Loaded into its own dialog window's scope by match-chapter-dialog.xhtml.
// See ChapterActions.handleMatchChapter (chapterActions.js) for how this
// dialog is opened and what window.arguments[0] contains. Runs in a
// separate JS global scope from the main Zotero window, so it reaches
// ChapterLinks and the ExtraFieldTool instance via the passed-in `plugin`
// object reference rather than via globals.

var MatchChapterDialog = {
	/** @type {*} */
	plugin: null,
	/** @type {*} */
	chapterItem: null,
	/** @type {string} */
	chapterTitle: '',
	/** @type {Array<*>} */
	candidates: [],
	/** @type {string|null} */
	selectedBookKey: null,

	init() {
		try {
			if (!window.arguments || !window.arguments[0]) {
				console.error('No arguments passed to Match Chapter dialog');
				return;
			}
			const args = window.arguments[0];
			this.plugin = args.plugin;
			this.chapterItem = args.chapterItem;
			this.chapterTitle = args.chapterTitle;
			this.candidates = args.candidates;

			document.getElementById('chapter-title').textContent = this.chapterItem.getField('title');
			document.getElementById('chapter-book-title').textContent = this.chapterTitle || '(none set on this chapter)';

			this._renderCandidates();

			const linkBtn = document.getElementById('link-btn');
			const rejectBtn = document.getElementById('reject-btn');
			if (this.candidates.length === 0) {
				linkBtn.disabled = true;
				rejectBtn.textContent = 'Close';
			}

			linkBtn.addEventListener('click', () => this._onLinkSelected());
			rejectBtn.addEventListener('click', () => window.close());
			document.getElementById('create-btn').addEventListener('click', () => this._onCreateNewBook());
		} catch (err) {
			console.error('Failed to initialize Match Chapter dialog:', err);
		}
	},

	/**
	 * @param {string} text
	 */
	_setStatus(text) {
		const statusBar = document.getElementById('status-bar');
		if (statusBar) statusBar.textContent = text || '';
	},

	_renderCandidates() {
		const tbody = document.getElementById('candidates-body');
		tbody.textContent = '';
		if (this.candidates.length === 0) {
			const row = document.createElementNS('http://www.w3.org/1999/xhtml', 'tr');
			const cell = document.createElementNS('http://www.w3.org/1999/xhtml', 'td');
			cell.setAttribute('colspan', '5');
			cell.textContent = 'No matching books found in this library.';
			row.appendChild(cell);
			tbody.appendChild(row);
			return;
		}
		this.candidates.forEach((candidate) => {
			const row = document.createElementNS('http://www.w3.org/1999/xhtml', 'tr');

			const radioCell = document.createElementNS('http://www.w3.org/1999/xhtml', 'td');
			const radio = document.createElementNS('http://www.w3.org/1999/xhtml', 'input');
			radio.type = 'radio';
			radio.name = 'candidate';
			radio.value = candidate.key;
			radio.addEventListener('change', () => { this.selectedBookKey = candidate.key; });
			radioCell.appendChild(radio);
			row.appendChild(radioCell);

			[candidate.title, candidate.creators, candidate.year != null ? String(candidate.year) : '', String(candidate.score)]
				.forEach(text => {
					const cell = document.createElementNS('http://www.w3.org/1999/xhtml', 'td');
					cell.textContent = text;
					row.appendChild(cell);
				});

			tbody.appendChild(row);
		});
	},

	async _onLinkSelected() {
		if (!this.selectedBookKey) {
			Services.prompt.alert(window, 'Zotero RAG', 'Select a candidate first.');
			return;
		}
		try {
			this._setStatus('Linking…');
			const candidate = this.candidates.find(c => c.key === this.selectedBookKey);
			await this.plugin.chapterLinks.writeLink(this.plugin.toolkit.extraField, candidate._item, this.chapterItem);
			window.close();
		} catch (err) {
			this._setStatus('');
			this.plugin.showError(`Failed to link chapter: ${err.message}`);
		}
	},

	async _onCreateNewBook() {
		try {
			this._setStatus('Creating book item…');
			const bookItem = new Zotero.Item('book');
			bookItem.libraryID = this.chapterItem.libraryID;
			bookItem.setField('title', this.chapterTitle || this.chapterItem.getField('title'));
			for (const field of ['publisher', 'place', 'date', 'ISBN', 'language']) {
				const value = this.chapterItem.getField(field);
				if (value) bookItem.setField(field, value);
			}
			const editorTypeID = Zotero.CreatorTypes.getID('editor');
			const editors = this.chapterItem.getCreators().filter(c => c.creatorTypeID === editorTypeID);
			if (editors.length) {
				bookItem.setCreators(editors);
			}
			await bookItem.saveTx();

			this._setStatus('Linking…');
			await this.plugin.chapterLinks.writeLink(this.plugin.toolkit.extraField, bookItem, this.chapterItem);
			window.close();
		} catch (err) {
			this._setStatus('');
			this.plugin.showError(`Failed to create and link book: ${err.message}`);
		}
	},
};
