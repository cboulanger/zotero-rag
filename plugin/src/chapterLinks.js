// plugin/src/chapterLinks.js
// Client-side implementation of the X-Contains/X-Contained-By Extra-field
// convention plus native "Related" item linking, used by the "Match
// Chapter" dialog (match-chapter-dialog.js) and chapterActions.js to link
// a bookSection item to its book without any backend call. Mirrors the
// format backend/services/chapter_link_store.py reads (write_links(),
// add_related_item()) closely enough to stay compatible with backend
// readers (chapter retrieval / citation suppression), implemented
// independently in JS -- see
// docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md.

var ChapterLinks = {
	/**
	 * @param {number} libraryID
	 * @returns {string} "users/<id>" or "groups/<id>"
	 */
	getZoteroSlug(libraryID) {
		if (libraryID === Zotero.Libraries.userLibraryID) {
			const userId = Zotero.Users.getCurrentUserID();
			return `users/${userId}`;
		}
		const group = Zotero.Groups.getByLibraryID(libraryID);
		return `groups/${group.id}`;
	},

	/**
	 * @param {string} slug
	 * @param {string} itemKey
	 * @returns {string}
	 */
	formatChapterId(slug, itemKey) {
		return `${slug}:${itemKey}`;
	},

	/**
	 * @param {*} item
	 * @returns {boolean}
	 */
	hasContainedByLink(item) {
		return !!item.getExtraField('X-Contained-By');
	},

	/**
	 * Write the X-Contains/X-Contained-By Extra-field link and the native
	 * "Related" connection between a book and one of its chapters. Both
	 * directions are written explicitly -- Zotero's local relations
	 * storage does not auto-mirror the reverse direction the way the
	 * server does (Zotero.Item.prototype._getRelatedItems only reads an
	 * item's own stored relations, confirmed against the Zotero source).
	 * @param {*} extraFieldTool - a zotero-plugin-toolkit ExtraFieldTool instance (plugin.toolkit.extraField)
	 * @param {*} bookItem
	 * @param {*} chapterItem
	 * @returns {Promise<void>}
	 */
	async writeLink(extraFieldTool, bookItem, chapterItem) {
		const slug = this.getZoteroSlug(bookItem.libraryID);
		const chapterId = this.formatChapterId(slug, chapterItem.key);
		const bookId = this.formatChapterId(slug, bookItem.key);

		const existing = extraFieldTool.getExtraField(bookItem, 'X-Contains', true) || [];
		const containsList = existing.length
			? existing[0].split(',').map(s => s.trim()).filter(Boolean)
			: [];
		if (!containsList.includes(chapterId)) {
			containsList.push(chapterId);
		}
		await extraFieldTool.setExtraField(bookItem, 'X-Contains', containsList.join(','), { save: false });
		bookItem.addRelatedItem(chapterItem);
		await bookItem.saveTx();

		await extraFieldTool.setExtraField(chapterItem, 'X-Contained-By', bookId, { save: false });
		chapterItem.addRelatedItem(bookItem);
		await chapterItem.saveTx();
	},
};
