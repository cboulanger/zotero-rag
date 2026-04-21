// Remote indexer for Zotero RAG plugin
//
// Used when the backend URL points to a remote server that cannot access local
// Zotero attachment files via file:// paths.  The plugin reads attachment bytes
// locally and uploads them to the backend one at a time.
//
// Loaded by dialog.xhtml before dialog.js so ZoteroRAGDialog can call it.

// @ts-check
/// <reference path="./zotero-types.d.ts" />

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
	 * @param {string} [opts.mode] - Indexing mode: "auto" | "incremental" | "full" (currently unused server-side)
	 * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
	 * @param {function(string): void} opts.log
	 * @param {function(UploadProgress): void} opts.onProgress
	 * @param {function(): boolean} opts.isCancelled - Return true to abort
	 * @param {AbortSignal} [opts.signal] - AbortSignal to cancel in-flight fetch requests immediately
	 * @param {Map<string, string>} [opts.downloadedFilePaths] - Cache of attachment key → local path for recently downloaded files
	 * @param {function(any): Promise<string|null>} [opts.downloadAttachment] - Download a single Zotero attachment item; returns local path or null on failure
	 * @returns {Promise<{uploaded: number, skipped: number, noFile: number, errors: number, firstError: string|null}>}
	 */
	async indexLibrary({ libraryId, libraryType, libraryName, backendURL, mode, getAuthHeaders, log, onProgress, isCancelled, signal, downloadedFilePaths, downloadAttachment }) {
		log(`[RemoteIndexer] Starting remote indexing for library ${libraryId}`);

		// 1. Collect all indexable attachments from the local Zotero database.
		//    Items without a local file are included (filePath: null) so check-indexed
		//    can decide whether they actually need indexing before we download them.
		onProgress({ percentage: 0, message: 'Scanning library', current: 0, total: 0 });
		const attachments = await this._collectAttachments(libraryId, libraryType, log, downloadedFilePaths);
		log(`[RemoteIndexer] Found ${attachments.length} indexable attachments`);

		if (attachments.length === 0) {
			onProgress({ percentage: 100, message: 'No indexable attachments found', current: 0, total: 0 });
			return { uploaded: 0, skipped: 0, noFile: 0, errors: 0, firstError: null };
		}

		// 2. Load the client-side version cache and pre-classify attachments.
		//    Attachments whose item_version matches the cache are up-to-date without
		//    asking the backend.  Only new/changed/unknown ones go to check-indexed.
		const versionCache = this._loadVersionCache(libraryId);
		/** @type {Array<AttachmentIndexStatus>} */
		const cachedStatuses = [];
		/** @type {typeof attachments} */
		const toCheck = [];
		for (const att of attachments) {
			const cachedVersion = versionCache[att.attachment_key];
			if (cachedVersion !== undefined && cachedVersion >= att.item_version) {
				cachedStatuses.push({ item_key: att.item_key, attachment_key: att.attachment_key, needs_indexing: false, reason: 'cached' });
			} else {
				toCheck.push(att);
			}
		}
		log(`[RemoteIndexer] Client cache: ${cachedStatuses.length} up-to-date, ${toCheck.length} to verify with backend`);

		// 2b. For cached items whose local file is missing, request a sync download so the
		//     file is available locally even if the backend already has the content indexed.
		if (downloadAttachment) {
			const cachedMissingFile = cachedStatuses
				.map(s => attachments.find(a => a.attachment_key === s.attachment_key))
				.filter(/** @type {(a: any) => a is NonNullable<typeof a>} */ (a) => a && !a.filePath);
			if (cachedMissingFile.length > 0) {
				log(`[RemoteIndexer] Requesting download for ${cachedMissingFile.length} cached attachment(s) with no local file`);
				for (const att of cachedMissingFile) {
					if (isCancelled()) break;
					try {
						const path = await downloadAttachment(att.zoteroItem);
						if (path) att.filePath = path;
					} catch (_) { /* non-fatal — file stays missing locally */ }
				}
			}
		}

		// 3. Ask the backend which of the remaining attachments actually need indexing.
		/** @type {Array<AttachmentIndexStatus>} */
		let checkedStatuses = [];
		if (toCheck.length > 0) {
			onProgress({ percentage: 0, message: `Checking ${toCheck.length} attachments`, current: 0, total: toCheck.length });
			checkedStatuses = await this._checkIndexed(libraryId, toCheck, backendURL, getAuthHeaders, log, signal, onProgress);
		}

		// Update cache for items the backend confirmed are up-to-date.
		for (let i = 0; i < checkedStatuses.length; i++) {
			const s = checkedStatuses[i];
			if (!s.needs_indexing) {
				const att = toCheck.find(a => a.attachment_key === s.attachment_key);
				if (att) versionCache[att.attachment_key] = att.item_version;
			}
		}

		const statuses = [...cachedStatuses, ...checkedStatuses];
		const toUpload = statuses.filter(s => s.needs_indexing);
		log(`[RemoteIndexer] ${toUpload.length} of ${attachments.length} attachments need indexing`);

		// 4. Download local files for toUpload items that have no cached path.
		//    Skips attachments already indexed — avoids downloading files we won't use.
		if (downloadAttachment) {
			const needsDownload = toUpload.filter(s => {
				const att = attachments.find(a => a.attachment_key === s.attachment_key);
				return att && !att.filePath;
			});
			if (needsDownload.length > 0) {
				log(`[RemoteIndexer] Downloading ${needsDownload.length} attachment(s) before indexing`);
				let dlCurrent = 0;
				for (const status of needsDownload) {
					if (isCancelled()) break;
					dlCurrent++;
					const att = attachments.find(a => a.attachment_key === status.attachment_key);
					if (!att) continue;
					onProgress({
						percentage: (dlCurrent / needsDownload.length) * 100,
						message: 'Downloading',
						current: dlCurrent,
						total: needsDownload.length,
					});
					const path = await downloadAttachment(att.zoteroItem);
					att.filePath = path;
				}
			}
		}

		// 5. Upload each attachment that needs indexing and has a local file
		let uploaded = 0;
		let skipped = attachments.length - toUpload.length;
		let errors = 0;
		/** @type {string|null} */
		let firstError = null;
		const uploadable = toUpload.filter(s => {
			const att = attachments.find(a => a.attachment_key === s.attachment_key);
			return att && att.filePath;
		});
		// Attachments with no local file are reported separately — not counted as errors
		const noFile = toUpload.length - uploadable.length;
		if (noFile > 0) {
			log(`[RemoteIndexer] ${noFile} attachment(s) have no local file — skipping`);
		}
		const total = uploadable.length;

		for (let i = 0; i < uploadable.length; i++) {
			if (isCancelled()) {
				log('[RemoteIndexer] Indexing cancelled');
				break;
			}

			const status = toUpload[i];
			const att = attachments.find(
				a => a.item_key === status.item_key && a.attachment_key === status.attachment_key
			);
			if (!att) continue;

			const label = att.parentItem
				? this._formatCitationLabel(att.parentItem)
				: this._formatCitationLabel(att.zoteroItem);
			onProgress({
				percentage: (i / total) * 100,
				message: `Indexing ${label}`,
				current: i + 1,
				total,
			});

			try {
				await this._uploadAttachment({ att, libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log, signal });
				uploaded++;
				versionCache[att.attachment_key] = att.item_version;
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				log(`[RemoteIndexer] Error uploading ${att.attachment_key}: ${msg}`);
				if (!firstError) firstError = msg;
				errors++;
				// Mark in cache so this item is not re-attempted on the next incremental run.
				// It will be retried when the item version changes or on a full re-index.
				versionCache[att.attachment_key] = att.item_version;
			}
		}

		// 6. Upload abstract-only items (no attachment file but substantial abstractNote)
		const abstractItems = await this._collectAbstractItems(libraryId, libraryType, log, attachments);
		log(`[RemoteIndexer] Found ${abstractItems.length} abstract-only item(s) to consider`);

		const abstractTotal = abstractItems.length;
		let abstractCurrent = 0;
		for (const abstractItem of abstractItems) {
			if (isCancelled()) break;

			// Skip if already up-to-date according to version cache
			const cachedVer = versionCache[abstractItem.attachment_key];
			if (cachedVer !== undefined && cachedVer >= abstractItem.item_version) {
				skipped++;
				continue;
			}

			abstractCurrent++;
			const label = this._formatCitationLabel(abstractItem.zoteroItem);
			onProgress({
				percentage: (abstractCurrent / abstractTotal) * 100,
				message: `Indexing ${label} (abstract)`,
				current: abstractCurrent,
				total: abstractTotal,
			});

			try {
				await this._uploadAbstract({ abstractItem, libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log, signal });
				uploaded++;
				versionCache[abstractItem.attachment_key] = abstractItem.item_version;
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				log(`[RemoteIndexer] Error uploading abstract for ${abstractItem.item_key}: ${msg}`);
				if (!firstError) firstError = msg;
				errors++;
				versionCache[abstractItem.attachment_key] = abstractItem.item_version;
			}
		}

		if (uploaded > 0 || errors > 0 || checkedStatuses.some(s => !s.needs_indexing) || abstractItems.length > 0) {
			this._saveVersionCache(libraryId, versionCache);
		}

		onProgress({ percentage: 100, message: `Done. Uploaded: ${uploaded}, Skipped: ${skipped + noFile}, Errors: ${errors}`, current: total, total });
		log(`[RemoteIndexer] Finished. uploaded=${uploaded}, skipped=${skipped}, noFile=${noFile}, errors=${errors}`);
		return { uploaded, skipped, noFile, errors, firstError };
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
	 * @param {Map<string, string>} [downloadedFilePaths] - Cache of attachment key → local path
	 * @returns {Promise<Array<AttachmentInfo & {zoteroItem: any, parentItem: any, filePath: string|null}>>}
	 */
	async _collectAttachments(libraryId, libraryType, log, downloadedFilePaths) {
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

		/** @type {Array<AttachmentInfo & {zoteroItem: any, parentItem: any, filePath: string|null}>} */
		const result = [];

		for (const item of items) {
			if (!item.isAttachment()) continue;

			const mimeType = item.attachmentContentType || '';
			if (!INDEXABLE_TYPES.has(mimeType)) continue;

			// Prefer live path; fall back to cached path from a prior download in this session.
			// Keep items with no local file (filePath: null) so check-indexed can decide
			// whether they need indexing before we attempt to download them.
			const filePath = await item.getFilePathAsync()
				|| (downloadedFilePaths && downloadedFilePaths.get(item.key))
				|| null;

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
				filePath,
			});
		}

		return result;
	},

	/**
	 * Count indexable attachments for a library without checking for local file existence.
	 * Fast (local Zotero DB only) — suitable for use at library-selection time.
	 *
	 * @param {string} libraryId
	 * @param {string} libraryType
	 * @returns {Promise<number>}
	 */
	async countIndexableAttachments(libraryId, libraryType) {
		const INDEXABLE_TYPES = new Set([
			'application/pdf',
			'text/html',
			'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
			'application/epub+zip',
		]);

		let zoteroLibraryID;
		if (libraryType === 'group') {
			const group = Zotero.Groups.get(parseInt(libraryId, 10));
			zoteroLibraryID = group ? group.libraryID : null;
		} else {
			zoteroLibraryID = parseInt(libraryId, 10);
		}
		if (!zoteroLibraryID) return 0;

		const search = new Zotero.Search();
		search.libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
		if (!itemIDs.length) return 0;

		const items = await Zotero.Items.getAsync(itemIDs);
		let count = 0;
		// Track parent item keys that have ANY indexable attachment (local or not).
		// Mirrors _collectAbstractItems which excludes these from abstract indexing.
		const keysWithAnyAttachment = new Set();
		for (const item of items) {
			if (!item.isAttachment()) continue;
			if (!INDEXABLE_TYPES.has(item.attachmentContentType || '')) continue;
			count++;
			if (item.parentItemID) {
				const parent = Zotero.Items.get(item.parentItemID);
				if (parent) keysWithAnyAttachment.add(parent.key);
			}
		}
		// Also count regular items with a substantial abstract and no indexable attachment at all.
		const MIN_ABSTRACT_WORDS = 100;
		for (const item of items) {
			if (item.isAttachment() || item.isNote()) continue;
			if (keysWithAnyAttachment.has(item.key)) continue;
			const abstract = item.getField ? (item.getField('abstractNote') || '') : '';
			if (!abstract) continue;
			const wordCount = abstract.trim().split(/\s+/).filter(/** @param {string} w */ w => w.length > 0).length;
			if (wordCount >= MIN_ABSTRACT_WORDS) count++;
		}
		return count;
	},

	/**
	 * Ask the backend which attachments need indexing.
	 *
	 * @param {string} libraryId
	 * @param {Array<AttachmentInfo>} attachments
	 * @param {string} backendURL
	 * @param {function(Record<string,string>=): Record<string,string>} getAuthHeaders
	 * @param {function(string): void} log
	 * @param {AbortSignal} [signal]
	 * @param {function(UploadProgress): void} [onProgress]
	 * @returns {Promise<Array<AttachmentIndexStatus>>}
	 */
	async _checkIndexed(libraryId, attachments, backendURL, getAuthHeaders, log, signal, onProgress) {
		const BATCH_SIZE = 100;
		/** @type {Array<AttachmentIndexStatus>} */
		const allStatuses = [];
		let checked = 0;

		for (let i = 0; i < attachments.length; i += BATCH_SIZE) {
			const batch = attachments.slice(i, i + BATCH_SIZE);
			try {
				const body = {
					library_id: libraryId,
					attachments: batch.map(a => ({
						item_key: a.item_key,
						attachment_key: a.attachment_key,
						mime_type: a.mime_type,
						item_version: a.item_version,
						attachment_version: a.attachment_version,
					})),
				};

				const response = await this._apiFetch(
					'POST',
					`${backendURL}/api/libraries/${libraryId}/check-indexed`,
					{ headers: getAuthHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body), signal },
				);

				const data = await response.json();
				allStatuses.push(...(data.statuses || []));
			} catch (err) {
				log(`[RemoteIndexer] check-indexed error: ${err} — marking batch as needs_indexing`);
				allStatuses.push(...batch.map(a => ({
					item_key: a.item_key,
					attachment_key: a.attachment_key,
					needs_indexing: true,
					reason: 'check_failed',
				})));
			}

			checked += batch.length;
			if (onProgress) {
				onProgress({
					percentage: (checked / attachments.length) * 100,
					message: `Checking attachments`,
					current: checked,
					total: attachments.length,
				});
			}
		}

		return allStatuses;
	},

	/**
	 * Upload a single attachment to the backend.
	 *
	 * @param {Object} opts
	 * @param {AttachmentInfo & {zoteroItem: any, parentItem: any, filePath: string|null}} opts.att
	 * @param {string} opts.libraryId
	 * @param {string} opts.libraryType
	 * @param {string} opts.libraryName
	 * @param {string} opts.backendURL
	 * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
	 * @param {function(string): void} opts.log
	 * @param {AbortSignal} [opts.signal]
	 * @returns {Promise<void>}
	 */
	async _uploadAttachment({ att, libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log, signal }) {
		// Prefer the path already resolved in _collectAttachments (may come from the
		// downloaded-paths cache); fall back to a fresh getFilePathAsync() call.
		const filePath = att.filePath || await att.zoteroItem.getFilePathAsync();
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

		const response = await this._apiFetch('POST', `${backendURL}/api/index/document`, {
			headers: getAuthHeaders(), // no Content-Type — let browser set multipart boundary
			body: formData,
			signal,
		});

		const result = await response.json();
		const rateLimitNote = result.rate_limit_retries > 0
			? ` [rate-limited, ${result.rate_limit_retries} retr${result.rate_limit_retries === 1 ? 'y' : 'ies'}]`
			: '';
		log(`[RemoteIndexer] ${att.attachment_key}: ${result.status} (${result.chunks_added} chunks)${rateLimitNote}`);
		if (result.status === 'error') {
			throw new Error(result.message || `Indexing failed for ${att.attachment_key}`);
		}
	},

	/**
	 * Fetch a URL and throw a descriptive error on non-2xx responses.
	 * The error message includes the method, path, HTTP status, and response body
	 * so callers can tell exactly which endpoint failed and why.
	 *
	/**
	 * Load the per-library version cache from Zotero prefs.
	 * @param {string} libraryId
	 * @returns {Record<string, number>} map of attachment_key → last confirmed item_version
	 */
	_loadVersionCache(libraryId) {
		try {
			const raw = Zotero.Prefs.get(`extensions.zotero-rag.indexCache.${libraryId}`, true) || '{}';
			return JSON.parse(raw);
		} catch (_) {
			return {};
		}
	},

	/**
	 * Persist the per-library version cache to Zotero prefs.
	 * @param {string} libraryId
	 * @param {Record<string, number>} cache
	 */
	_saveVersionCache(libraryId, cache) {
		try {
			Zotero.Prefs.set(`extensions.zotero-rag.indexCache.${libraryId}`, JSON.stringify(cache), true);
		} catch (e) {
			// Non-fatal — cache miss on next session is acceptable
		}
	},

	/**
	 * @param {string} method
	 * @param {string} url
	 * @param {RequestInit} [init]
	 * @returns {Promise<Response>}
	 */
	async _apiFetch(method, url, init) {
		const response = await fetch(url, { method, ...init });
		if (!response.ok) {
			let detail = '';
			const ct = response.headers.get('content-type') || '';
			if (ct.includes('application/json')) {
				const body = await response.json().catch(() => ({}));
				detail = body.detail || JSON.stringify(body);
			} else {
				const text = await response.text().catch(() => '');
				// Strip HTML tags (e.g. nginx error pages) and normalise whitespace
				detail = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
			}
			const path = (() => { try { return new URL(url).pathname; } catch { return url; } })();
			throw new Error(`${method} ${path}: HTTP ${response.status}${detail ? ` — ${detail}` : ''}`);
		}
		return response;
	},

	/**
	 * Collect regular items (non-attachments) that should be indexed via their abstractNote.
	 * Includes items where no local attachment file is available but the abstract is substantial.
	 *
	 * @param {string} libraryId
	 * @param {string} libraryType
	 * @param {function(string): void} log
	 * @param {Array<{item_key: string, filePath: string|null}>} existingAttachments - From _collectAttachments()
	 * @param {number} [minWords=100] - Minimum abstract word count
	 * @returns {Promise<Array<{item_key: string, attachment_key: string, item_version: number, zoteroItem: any, abstractNote: string}>>}
	 */
	async _collectAbstractItems(libraryId, libraryType, log, existingAttachments, minWords = 100) {
		let zoteroLibraryID;
		if (libraryType === 'group') {
			const group = Zotero.Groups.get(parseInt(libraryId, 10));
			zoteroLibraryID = group ? group.libraryID : null;
		} else {
			zoteroLibraryID = parseInt(libraryId, 10);
		}
		if (!zoteroLibraryID) return [];

		// Parent item keys that have ANY indexable attachment in Zotero (local file or not).
		// Abstract indexing is only a last resort for items with no attachment at all.
		const keysWithAnyAttachment = new Set(existingAttachments.map(a => a.item_key));

		const search = new Zotero.Search();
		search.libraryID = zoteroLibraryID;
		const itemIDs = await search.search();
		if (!itemIDs.length) return [];

		const items = await Zotero.Items.getAsync(itemIDs);
		/** @type {Array<{item_key: string, attachment_key: string, item_version: number, zoteroItem: any, abstractNote: string}>} */
		const result = [];

		for (const item of items) {
			if (item.isAttachment() || item.isNote()) continue;

			const itemKey = item.key;
			if (keysWithAnyAttachment.has(itemKey)) continue;

			const abstract = item.getField ? (item.getField('abstractNote') || '') : '';
			if (!abstract) continue;

			const wordCount = abstract.trim().split(/\s+/).filter(/** @param {string} w */ w => w.length > 0).length;
			if (wordCount < minWords) continue;

			result.push({
				item_key: itemKey,
				attachment_key: itemKey + ':abstract',
				item_version: item.version || 0,
				zoteroItem: item,
				abstractNote: abstract,
			});
		}

		log(`[RemoteIndexer] _collectAbstractItems: ${result.length} item(s) with usable abstract`);
		return result;
	},

	/**
	 * Upload an item's abstractNote to the backend for indexing.
	 *
	 * @param {Object} opts
	 * @param {{item_key: string, attachment_key: string, item_version: number, zoteroItem: any, abstractNote: string}} opts.abstractItem
	 * @param {string} opts.libraryId
	 * @param {string} opts.libraryType
	 * @param {string} opts.libraryName
	 * @param {string} opts.backendURL
	 * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
	 * @param {function(string): void} opts.log
	 * @param {AbortSignal} [opts.signal]
	 * @returns {Promise<void>}
	 */
	async _uploadAbstract({ abstractItem, libraryId, libraryType, libraryName, backendURL, getAuthHeaders, log, signal }) {
		const item = abstractItem.zoteroItem;
		const body = {
			library_id: libraryId,
			library_type: libraryType,
			library_name: libraryName,
			item_key: abstractItem.item_key,
			item_version: abstractItem.item_version,
			title: item.getField ? (item.getField('title') || 'Untitled') : 'Untitled',
			authors: this._extractAuthors(item),
			year: this._extractYear(item),
			item_type: item.itemType || null,
			zotero_modified: item.dateModified || new Date().toISOString(),
			abstract_text: abstractItem.abstractNote,
		};

		const response = await this._apiFetch('POST', `${backendURL}/api/index/abstract`, {
			headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
			signal,
		});

		const result = await response.json();
		log(`[RemoteIndexer] ${abstractItem.item_key} (abstract): ${result.status} (${result.chunks_added} chunks)`);
		if (result.status === 'error') {
			throw new Error(result.message || `Abstract indexing failed for ${abstractItem.item_key}`);
		}
	},

	/**
	 * Format a short citation label: "Lastname et al. (Year) \"Title...\""
	 * @param {any} item - Zotero item
	 * @param {number} [maxTitleLen=50]
	 * @returns {string}
	 */
	_formatCitationLabel(item, maxTitleLen = 50) {
		const authors = this._extractAuthors(item);
		let authorPart = '';
		if (authors.length > 0) {
			const lastName = authors[0].split(' ').pop() || authors[0];
			authorPart = authors.length > 1 ? `${lastName} et al.` : lastName;
		}
		const year = this._extractYear(item);
		const yearPart = year ? ` (${year})` : '';
		let title = (item.getField ? item.getField('title') : '') || '';
		if (title.length > maxTitleLen) title = title.slice(0, maxTitleLen) + '\u2026';
		const titlePart = title ? ` "${title}"` : '';
		return `${authorPart}${yearPart}${titlePart}`.trim();
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
