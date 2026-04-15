// Remote indexer for Zotero RAG plugin
//
// Used when the backend URL points to a remote server that cannot access local
// Zotero attachment files via file:// paths.  The plugin reads attachment bytes
// locally and uploads them to the backend one at a time.
//
// Loaded by dialog.xhtml before dialog.js so ZoteroRAGDialog can call it.

// @ts-check

/**
 * @typedef {Object} AttachmentInfo
 * @property {string} item_key
 * @property {string} attachment_key
 * @property {string} mime_type
 * @property {number} item_version
 * @property {number} attachment_version
 */

/**
 * @typedef {Object} AttachmentIndexStatus
 * @property {string} item_key
 * @property {string} attachment_key
 * @property {boolean} needs_indexing
 * @property {string} reason
 */

/**
 * @typedef {Object} UploadProgress
 * @property {number} percentage - 0-100
 * @property {string} message
 * @property {number} current
 * @property {number} total
 */

/**
 * Remote-mode document upload coordinator.
 *
 * When the backend is not on localhost, this module replaces the
 * server-side Zotero API access with plugin-side file reading + HTTP upload.
 */
var RemoteIndexer = {

	/**
	 * Index a library by uploading attachments to the remote backend.
	 *
	 * @param {Object} opts
	 * @param {string} opts.libraryId - Zotero library ID (used by backend)
	 * @param {string} opts.libraryType - "user" or "group"
	 * @param {string} opts.libraryName - Human-readable library name
	 * @param {string} opts.backendURL - Backend base URL
	 * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
	 * @param {function(string): void} opts.log
	 * @param {function(UploadProgress): void} opts.onProgress
	 * @param {function(): boolean} opts.isCancelled - Return true to abort
	 * @returns {Promise<{uploaded: number, skipped: number, errors: number}>}
	 */
	async indexLibrary({ libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log, onProgress, isCancelled }) {
		log(`[RemoteIndexer] Starting remote indexing for library ${libraryId}`);

		// 1. Collect all indexable attachments from the local Zotero database
		const attachments = await this._collectAttachments(libraryId, libraryType, log);
		log(`[RemoteIndexer] Found ${attachments.length} indexable attachments`);

		if (attachments.length === 0) {
			onProgress({ percentage: 100, message: 'No indexable attachments found', current: 0, total: 0 });
			return { uploaded: 0, skipped: 0, errors: 0 };
		}

		// 2. Ask the backend which attachments actually need indexing
		const statuses = await this._checkIndexed(libraryId, attachments, backendURL, getAuthHeaders, log);
		const toUpload = statuses.filter(s => s.needs_indexing);
		log(`[RemoteIndexer] ${toUpload.length} of ${attachments.length} attachments need indexing`);

		// 3. Upload each attachment that needs indexing
		let uploaded = 0;
		let skipped = attachments.length - toUpload.length;
		let errors = 0;
		const total = toUpload.length;

		for (let i = 0; i < toUpload.length; i++) {
			if (isCancelled()) {
				log('[RemoteIndexer] Indexing cancelled');
				break;
			}

			const status = toUpload[i];
			const att = attachments.find(
				a => a.item_key === status.item_key && a.attachment_key === status.attachment_key
			);
			if (!att) continue;

			onProgress({
				percentage: (i / total) * 100,
				message: `Uploading attachment ${i + 1} of ${total}`,
				current: i + 1,
				total,
			});

			try {
				await this._uploadAttachment({ att, libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log });
				uploaded++;
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				log(`[RemoteIndexer] Error uploading ${att.attachment_key}: ${msg}`);
				errors++;
			}
		}

		onProgress({ percentage: 100, message: `Done. Uploaded: ${uploaded}, Skipped: ${skipped}, Errors: ${errors}`, current: total, total });
		log(`[RemoteIndexer] Finished. uploaded=${uploaded}, skipped=${skipped}, errors=${errors}`);
		return { uploaded, skipped, errors };
	},

	// ---------------------------------------------------------------------------
	// Private helpers
	// ---------------------------------------------------------------------------

	/**
	 * Collect all indexable attachments from the local Zotero library.
	 *
	 * @param {string} libraryId
	 * @param {string} libraryType
	 * @param {function(string): void} log
	 * @returns {Promise<Array<AttachmentInfo & {zoteroItem: any, parentItem: any}>>}
	 */
	async _collectAttachments(libraryId, libraryType, log) {
		const INDEXABLE_TYPES = new Set([
			'application/pdf',
			'text/html',
			'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
			'application/epub+zip',
		]);

		// Resolve Zotero internal libraryID
		let zoteroLibraryID;
		if (libraryType === 'group') {
			const group = Zotero.Groups.get(parseInt(libraryId, 10));
			zoteroLibraryID = group ? group.libraryID : null;
		} else {
			zoteroLibraryID = parseInt(libraryId, 10);
		}

		if (!zoteroLibraryID) {
			log(`[RemoteIndexer] Could not resolve libraryID for ${libraryId}`);
			return [];
		}

		const search = new Zotero.Search();
		search.libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
		if (!itemIDs.length) return [];

		const items = await Zotero.Items.getAsync(itemIDs);

		/** @type {Array<AttachmentInfo & {zoteroItem: any, parentItem: any}>} */
		const result = [];

		for (const item of items) {
			if (!item.isAttachment()) continue;

			const mimeType = item.attachmentContentType || '';
			if (!INDEXABLE_TYPES.has(mimeType)) continue;

			// Only include locally-stored attachments (file must exist)
			const filePath = await item.getFilePathAsync();
			if (!filePath) continue;

			// Get parent item for metadata
			const parentItem = item.parentItemID
				? await Zotero.Items.getAsync(item.parentItemID)
				: null;

			result.push({
				item_key: parentItem ? parentItem.key : item.key,
				attachment_key: item.key,
				mime_type: mimeType,
				item_version: parentItem ? (parentItem.version || 0) : (item.version || 0),
				attachment_version: item.version || 0,
				zoteroItem: item,
				parentItem,
			});
		}

		return result;
	},

	/**
	 * Ask the backend which attachments need indexing.
	 *
	 * @param {string} libraryId
	 * @param {Array<AttachmentInfo>} attachments
	 * @param {string} backendURL
	 * @param {function(Record<string,string>=): Record<string,string>} getAuthHeaders
	 * @param {function(string): void} log
	 * @returns {Promise<Array<AttachmentIndexStatus>>}
	 */
	async _checkIndexed(libraryId, attachments, backendURL, getAuthHeaders, log) {
		try {
			const body = {
				library_id: libraryId,
				attachments: attachments.map(a => ({
					item_key: a.item_key,
					attachment_key: a.attachment_key,
					mime_type: a.mime_type,
					item_version: a.item_version,
					attachment_version: a.attachment_version,
				})),
			};

			const response = await fetch(`${backendURL}/api/libraries/${libraryId}/check-indexed`, {
				method: 'POST',
				headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify(body),
			});

			if (!response.ok) {
				log(`[RemoteIndexer] check-indexed returned HTTP ${response.status}, uploading all`);
				// Fallback: assume all need indexing
				return attachments.map(a => ({
					item_key: a.item_key,
					attachment_key: a.attachment_key,
					needs_indexing: true,
					reason: 'check_failed',
				}));
			}

			const data = await response.json();
			return data.statuses || [];
		} catch (err) {
			log(`[RemoteIndexer] check-indexed error: ${err}, uploading all`);
			return attachments.map(a => ({
				item_key: a.item_key,
				attachment_key: a.attachment_key,
				needs_indexing: true,
				reason: 'check_failed',
			}));
		}
	},

	/**
	 * Upload a single attachment to the backend.
	 *
	 * @param {Object} opts
	 * @param {AttachmentInfo & {zoteroItem: any, parentItem: any}} opts.att
	 * @param {string} opts.libraryId
	 * @param {string} opts.libraryType
	 * @param {string} opts.libraryName
	 * @param {string} opts.backendURL
	 * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
	 * @param {function(string): void} opts.log
	 * @returns {Promise<void>}
	 */
	async _uploadAttachment({ att, libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log }) {
		const filePath = await att.zoteroItem.getFilePathAsync();
		if (!filePath) {
			throw new Error(`No local file path for attachment ${att.attachment_key}`);
		}

		// Read raw bytes from local disk
		const bytes = await IOUtils.read(filePath);

		// Collect item metadata from the parent Zotero item (or attachment itself)
		const parent = att.parentItem || att.zoteroItem;
		const metadata = {
			library_id: libraryId,
			library_type: libraryType,
			item_key: att.item_key,
			attachment_key: att.attachment_key,
			mime_type: att.mime_type,
			item_version: att.item_version,
			attachment_version: att.attachment_version,
			title: parent.getField ? (parent.getField('title') || 'Untitled') : 'Untitled',
			authors: this._extractAuthors(parent),
			year: this._extractYear(parent),
			item_type: parent.itemType || null,
			zotero_modified: parent.dateModified || new Date().toISOString(),
		};

		const formData = new FormData();
		formData.append('file', new Blob([bytes], { type: att.mime_type }), att.attachment_key);
		formData.append('metadata', JSON.stringify(metadata));

		const response = await fetch(`${backendURL}/api/index/document`, {
			method: 'POST',
			headers: getAuthHeaders(), // no Content-Type — let browser set multipart boundary
			body: formData,
		});

		if (!response.ok) {
			const errBody = await response.json().catch(() => ({}));
			throw new Error(errBody.detail || `HTTP ${response.status}`);
		}

		const result = await response.json();
		log(`[RemoteIndexer] ${att.attachment_key}: ${result.status} (${result.chunks_added} chunks)`);
	},

	/**
	 * Extract author names from a Zotero item.
	 * @param {any} item
	 * @returns {Array<string>}
	 */
	_extractAuthors(item) {
		if (!item || !item.getCreators) return [];
		try {
			return item.getCreators()
				.filter(c => c.creatorTypeID === Zotero.CreatorTypes.getID('author') ||
				             c.creatorTypeID === Zotero.CreatorTypes.getID('editor'))
				.map(c => `${c.firstName || ''} ${c.lastName || ''}`.trim())
				.filter(Boolean);
		} catch (_) {
			return [];
		}
	},

	/**
	 * Extract the publication year from a Zotero item.
	 * @param {any} item
	 * @returns {number|null}
	 */
	_extractYear(item) {
		if (!item || !item.getField) return null;
		try {
			const dateStr = item.getField('date') || '';
			const m = dateStr.match(/\b(19|20)\d{2}\b/);
			return m ? parseInt(m[0], 10) : null;
		} catch (_) {
			return null;
		}
	},
};
