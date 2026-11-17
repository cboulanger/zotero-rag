// Indexed-status tags for Zotero RAG.
//
// Keeps a plain emoji tag ("✅ rag-indexed") on every attachment the backend has
// indexed; Zotero's item list renders any emoji tag as an icon before the title, so
// no custom column is needed. Two paths feed the same tag state:
//   - real time: poll /api/indexed-tags/events and apply each transition;
//   - Refresh:   run bin/sync_indexed_tags.py on the backend (POST /api/indexed-tags/refresh)
//                and apply the tag operations it emits as they stream in.
// The stored Zotero keys are read-only, so only the plugin can write tags (item.addTag +
// saveTx, type 1 = automatic). Tag removal is always confirmed against the backend first
// (POST /api/indexed-tags/check): a stale refresh operation or a transient re-index must
// not strip the tag from an attachment that is indexed again.
//
// The real-time path only sees events that happen while the plugin is polling, so tags
// can drift (Zotero closed while events rotated out, events never emitted, tags edited by
// hand or by another member's plugin). The plugin therefore also runs Refresh on its own:
// on first contact, after a `gap`, and whenever the last completed reconciliation is
// older than `autoRefreshHours` (default 24; 0 turns the automatic runs off). A refresh
// needs the key stored for automatic indexing; without it the attempt fails and is
// retried an hour later.
//
// A second tag, "⚠️ rag-failed", marks attachments the backend refuses to process (scan
// too big/costly, or quarantined after repeated failures). It is applied from `failed`
// events / `kind: "failed"` refresh ops, and removing it is how the user asks for a retry:
// a Zotero notifier observer reports the removal to POST /api/indexed-tags/failed/clear.
//
// Plugin-lifetime script, loaded eagerly by bootstrap.js.

// @ts-check

/**
 * @typedef {Object} IndexEvent
 * @property {number} seq
 * @property {'indexed'|'unindexed'|'library_unindexed'|'failed'|'unfailed'} type
 * @property {string} library_id - backend library id ("u123" personal, "456" group)
 * @property {string} [attachment_key]
 */

/**
 * @typedef {Object} TagOp
 * @property {'add'|'remove'} op
 * @property {'failed'} [kind] - set for operations on the rag-failed tag
 * @property {string} attachment_key
 * @property {string} [item_key]
 */

/**
 * @typedef {Object} RefreshStats
 * @property {number} librariesTotal
 * @property {number} librariesDone
 * @property {number} attachmentsChecked
 * @property {number} added
 * @property {number} removed
 * @property {number} skipped - ops not applied (unknown item/library, read-only library, re-confirmed indexed)
 * @property {string[]} errors
 * @property {string} [currentLibrary]
 */

var IndexedTags = {
	DEFAULT_TAG: '\u2705 rag-indexed',
	PREF_ENABLED: 'extensions.zotero-rag.indexedTags.enabled',
	PREF_CURSOR: 'extensions.zotero-rag.indexedTags.cursor',
	// Epoch ms of the last completed reconciliation (manual or automatic).
	PREF_LAST_REFRESH: 'extensions.zotero-rag.indexedTags.lastRefresh',
	// Hours between automatic reconciliations; 0 disables them.
	PREF_AUTO_REFRESH_HOURS: 'extensions.zotero-rag.indexedTags.autoRefreshHours',
	DEFAULT_AUTO_REFRESH_HOURS: 24,
	// After a failed automatic refresh (backend down, no auto-index key stored), wait this long.
	AUTO_REFRESH_RETRY_MS: 3600000,
	POLL_INTERVAL_MS: 15000,
	// An "unindexed" event is held this long (and then re-checked against the backend)
	// before the tag is removed: re-indexing deletes an attachment's chunks before
	// re-adding them, and removing + re-adding the tag in between would needlessly
	// churn item versions.
	REMOVE_SETTLE_MS: 120000,
	REFRESH_POLL_MS: 1000,

	/** @type {any} */ plugin: null,
	tag: '\u2705 rag-indexed',
	failedTag: '\u26a0\ufe0f rag-failed',
	/** @type {string|null} */ notifierID: null,
	/** @type {number|null} */ cursor: null,
	/** @type {ReturnType<typeof setTimeout>|null} */ timer: null,
	running: false,
	/** Last real-time event seq applied per "libraryID/attachmentKey" (guards stale refresh ops). @type {Map<string, number>} */
	appliedSeq: new Map(),
	/** Removals waiting out REMOVE_SETTLE_MS, keyed like appliedSeq. @type {Map<string, {libraryID: number, key: string, due: number}>} */
	pendingRemovals: new Map(),
	/** Automatic refresh in progress, if any. @type {Promise<void>|null} */
	autoRefreshInFlight: null,
	/** No automatic attempt before this time (epoch ms), set after a failure. */
	autoRefreshNotBefore: 0,
	/** A reconciliation is needed regardless of the interval (first contact, event gap). */
	autoRefreshWanted: false,

	/**
	 * @param {any} plugin - the ZoteroRAGPlugin instance (backendURL, getAuthHeaders, log)
	 * @returns {void}
	 */
	init(plugin) {
		this.plugin = plugin;
		this.running = true;
		this.cursor = this._loadCursor();
		this.autoRefreshNotBefore = 0;
		this.autoRefreshWanted = false;
		this._schedule(2000);
		this._registerFailedTagObserver();
	},

	/** @returns {void} */
	shutdown() {
		this.running = false;
		if (this.timer) clearTimeout(this.timer);
		this.timer = null;
		this.pendingRemovals.clear();
		if (this.notifierID) Zotero.Notifier.unregisterObserver(this.notifierID);
		this.notifierID = null;
	},

	/** @returns {boolean} */
	isEnabled() {
		return !!Zotero.Prefs.get(this.PREF_ENABLED, true);
	},

	/** @param {string} msg @returns {void} */
	_log(msg) {
		try { this.plugin?.log?.(`[IndexedTags] ${msg}`); } catch (_) { /* logging must never throw */ }
	},

	/** @returns {number|null} */
	_loadCursor() {
		const raw = Zotero.Prefs.get(this.PREF_CURSOR, true);
		const n = parseInt(String(raw ?? ''), 10);
		return Number.isFinite(n) ? n : null;
	},

	/** @param {number} seq @returns {void} */
	_saveCursor(seq) {
		this.cursor = seq;
		Zotero.Prefs.set(this.PREF_CURSOR, String(seq), true);
	},

	/** @param {number} ms @returns {void} */
	_schedule(ms) {
		if (!this.running) return;
		this.timer = setTimeout(async () => {
			try {
				await this.pollOnce();
			} catch (e) {
				this._log(`poll failed: ${e}`);
			}
			this._schedule(this.POLL_INTERVAL_MS);
		}, ms);
	},

	/**
	 * GET/POST helper against the backend with the plugin's auth headers.
	 * @param {string} path
	 * @param {{method?: string, body?: any}} [opts]
	 * @returns {Promise<any>}
	 */
	async _api(path, opts = {}) {
		const headers = this.plugin.getAuthHeaders(opts.body ? { 'Content-Type': 'application/json' } : {});
		const response = await fetch(`${this.plugin.backendURL}${path}`, {
			method: opts.method || 'GET',
			headers,
			body: opts.body ? JSON.stringify(opts.body) : undefined,
		});
		if (!response.ok) {
			const err = await response.json().catch(() => ({}));
			throw new Error(err.detail || `HTTP ${response.status}`);
		}
		return response.json();
	},

	/**
	 * Map a backend library id to Zotero's internal libraryID; null if this Zotero
	 * profile doesn't have that library (or the id is another user's personal library).
	 * @param {string} backendId
	 * @returns {number|null}
	 */
	backendIdToLibraryID(backendId) {
		if (backendId.startsWith('u')) {
			const uid = this.plugin.getCurrentZoteroUserId();
			return uid && backendId === `u${uid}` ? Zotero.Libraries.userLibraryID : null;
		}
		const group = Zotero.Groups.get(parseInt(backendId, 10));
		return group ? group.libraryID : null;
	},

	/**
	 * @param {string} slug - "users/<id>" or "groups/<id>"
	 * @returns {number|null}
	 */
	slugToLibraryID(slug) {
		if (slug.startsWith('users/')) return this.backendIdToLibraryID(`u${slug.slice(6)}`);
		if (slug.startsWith('groups/')) return this.backendIdToLibraryID(slug.slice(7));
		return null;
	},

	/**
	 * Add or remove the tag on an attachment item. Idempotent: no write (and no
	 * saveTx) when the item already has the desired state.
	 * @param {any} item
	 * @param {boolean} tagged
	 * @param {string} [tag] - defaults to the indexed tag
	 * @returns {Promise<'changed'|'unchanged'|'skipped'>}
	 */
	async setTagged(item, tagged, tag = this.tag) {
		if (!item || !item.isAttachment || !item.isAttachment()) return 'skipped';
		if (!Zotero.Libraries.get(item.libraryID)?.editable) return 'skipped';
		const has = item.hasTag(tag);
		if (has === tagged) return 'unchanged';
		if (tagged) item.addTag(tag, 1);
		else item.removeTag(tag);
		// The tag is bookkeeping, not an edit by the user: don't bump dateModified.
		await item.saveTx({ skipDateModifiedUpdate: true });
		return 'changed';
	},

	/**
	 * Of these attachment keys, return those the backend currently reports as indexed.
	 * @param {number} libraryID
	 * @param {string[]} keys
	 * @returns {Promise<Set<string>>}
	 */
	async confirmIndexed(libraryID, keys) {
		/** @type {Set<string>} */
		const indexed = new Set();
		const backendId = this.plugin.getBackendLibraryId(libraryID);
		for (let i = 0; i < keys.length; i += 500) {
			const data = await this._api('/api/indexed-tags/check', {
				method: 'POST',
				body: { library_id: backendId, attachment_keys: keys.slice(i, i + 500) },
			});
			for (const k of data.indexed || []) indexed.add(k);
		}
		return indexed;
	},

	/**
	 * Apply tag removals after re-confirming with the backend that they're still
	 * un-indexed. Keys the backend now reports as indexed are re-tagged instead.
	 * @param {number} libraryID
	 * @param {string[]} keys
	 * @returns {Promise<{removed: number, skipped: number}>}
	 */
	async removeConfirmed(libraryID, keys) {
		const stillIndexed = await this.confirmIndexed(libraryID, keys);
		let removed = 0;
		let skipped = 0;
		for (const key of keys) {
			const item = Zotero.Items.getByLibraryAndKey(libraryID, key);
			const result = await this.setTagged(/** @type {any} */ (item), stillIndexed.has(key));
			if (stillIndexed.has(key)) skipped++;
			else if (result === 'changed') removed++;
			else if (result === 'skipped') skipped++;
		}
		return { removed, skipped };
	},

	/**
	 * Apply a batch of real-time events: per attachment the newest event wins.
	 * Adds happen immediately; removals are deferred (see REMOVE_SETTLE_MS).
	 * @param {IndexEvent[]} events
	 * @param {number} [now]
	 * @returns {Promise<void>}
	 */
	async applyEvents(events, now = Date.now()) {
		/** @type {Map<string, IndexEvent>} */
		const latest = new Map();
		for (const ev of events) {
			if (ev.type === 'library_unindexed') {
				await this._queueLibraryRemovals(ev, now);
				continue;
			}
			const libraryID = this.backendIdToLibraryID(ev.library_id);
			if (libraryID === null || !ev.attachment_key) continue;
			const group = ev.type === 'failed' || ev.type === 'unfailed' ? 'failed:' : '';
			latest.set(`${group}${libraryID}/${ev.attachment_key}`, ev);
		}
		for (const [id, ev] of latest) {
			const libraryID = /** @type {number} */ (this.backendIdToLibraryID(ev.library_id));
			const key = /** @type {string} */ (ev.attachment_key);
			if (ev.type === 'failed' || ev.type === 'unfailed') {
				this.appliedSeq.set(id, ev.seq);
				const item = /** @type {any} */ (Zotero.Items.getByLibraryAndKey(libraryID, key));
				await this.setTagged(item, ev.type === 'failed', this.failedTag);
				// A quarantined attachment usually has no chunks; a leftover indexed tag
				// would make it look fine. Queue the usual settled + confirmed removal
				// (a later `indexed` event cancels it; the check keeps a tag that is right).
				const indexedId = `${libraryID}/${key}`;
				if (ev.type === 'failed' && item && item.hasTag?.(this.tag) && latest.get(indexedId)?.type !== 'indexed') {
					this.pendingRemovals.set(indexedId, { libraryID, key, due: now + this.REMOVE_SETTLE_MS });
				}
				continue;
			}
			this.appliedSeq.set(id, ev.seq);
			if (ev.type === 'indexed') {
				this.pendingRemovals.delete(id);
				await this.setTagged(/** @type {any} */ (Zotero.Items.getByLibraryAndKey(libraryID, key)), true);
			} else {
				this.pendingRemovals.set(id, { libraryID, key, due: now + this.REMOVE_SETTLE_MS });
			}
		}
	},

	/**
	 * A whole library was cleared: schedule removal for every attachment carrying the tag.
	 * @param {IndexEvent} ev
	 * @param {number} now
	 * @returns {Promise<void>}
	 */
	async _queueLibraryRemovals(ev, now) {
		const libraryID = this.backendIdToLibraryID(ev.library_id);
		if (libraryID === null) return;
		const tagID = Zotero.Tags.getID(this.tag);
		if (!tagID) return;
		const ids = await Zotero.Tags.getTagItems(libraryID, tagID);
		for (const item of Zotero.Items.get(ids)) {
			this.appliedSeq.set(`${libraryID}/${item.key}`, ev.seq);
			this.pendingRemovals.set(`${libraryID}/${item.key}`, { libraryID, key: item.key, due: now + this.REMOVE_SETTLE_MS });
		}
	},

	/**
	 * Execute pending removals whose settle time has passed.
	 * @param {number} [now]
	 * @returns {Promise<void>}
	 */
	async flushDueRemovals(now = Date.now()) {
		/** @type {Map<number, string[]>} */
		const byLibrary = new Map();
		for (const [id, p] of this.pendingRemovals) {
			if (p.due > now) continue;
			this.pendingRemovals.delete(id);
			byLibrary.set(p.libraryID, [...(byLibrary.get(p.libraryID) || []), p.key]);
		}
		for (const [libraryID, keys] of byLibrary) {
			await this.removeConfirmed(libraryID, keys);
		}
	},

	/**
	 * One poll cycle of the real-time path.
	 * @returns {Promise<void>}
	 */
	async pollOnce() {
		if (!this.isEnabled() || !this.plugin?.backendURL) return;
		if (this.cursor === null) {
			// First contact: follow from "now"; Refresh covers existing history.
			const head = await this._api('/api/indexed-tags/events');
			this.tag = head.tag || this.tag;
			this.failedTag = head.failed_tag || this.failedTag;
			this._saveCursor(head.last_seq);
			// Following from "now" skips history: reconcile it once.
			this.autoRefreshWanted = true;
			this.maybeAutoRefresh();
			return;
		}
		const data = await this._api(`/api/indexed-tags/events?since=${this.cursor}`);
		this.tag = data.tag || this.tag;
		this.failedTag = data.failed_tag || this.failedTag;
		if (data.gap) {
			this._log('missed some index events while offline; reconciling all tags');
			this.autoRefreshWanted = true;
		}
		if (data.events.length) await this.applyEvents(data.events);
		await this.flushDueRemovals();
		if (data.last_seq > this.cursor) this._saveCursor(data.last_seq);
		this.maybeAutoRefresh();
	},

	/**
	 * Hours between automatic reconciliations (0 = off).
	 * @returns {number}
	 */
	_autoRefreshHours() {
		const raw = Zotero.Prefs.get(this.PREF_AUTO_REFRESH_HOURS, true);
		const n = Number(raw);
		return raw === undefined || raw === null || raw === '' || !Number.isFinite(n) ? this.DEFAULT_AUTO_REFRESH_HOURS : n;
	},

	/**
	 * @param {number} [now]
	 * @returns {boolean}
	 */
	isAutoRefreshDue(now = Date.now()) {
		if (this.autoRefreshInFlight || now < this.autoRefreshNotBefore) return false;
		const hours = this._autoRefreshHours();
		if (hours <= 0) return false;
		if (this.autoRefreshWanted) return true;
		const last = Number(Zotero.Prefs.get(this.PREF_LAST_REFRESH, true));
		if (!Number.isFinite(last) || last <= 0) return true;
		return now - last >= hours * 3600000;
	},

	/**
	 * Start a background reconciliation if one is due. Does not wait for it: a full
	 * refresh can take minutes, and the real-time poll keeps running alongside it
	 * (stale refresh ops are dropped via appliedSeq).
	 * @param {number} [now]
	 * @returns {Promise<void>|null} the run, when one was started
	 */
	maybeAutoRefresh(now = Date.now()) {
		if (!this.running || !this.isEnabled() || !this.isAutoRefreshDue(now)) return null;
		this._log('starting automatic tag reconciliation');
		this.autoRefreshInFlight = (async () => {
			try {
				const stats = await this.refresh();
				this.autoRefreshWanted = false;
				this._log(`automatic reconciliation done: ${stats.attachmentsChecked} checked, ${stats.added} tagged, ${stats.removed} untagged`
					+ (stats.errors.length ? `, errors: ${stats.errors.join('; ')}` : ''));
			} catch (e) {
				this.autoRefreshNotBefore = Date.now() + this.AUTO_REFRESH_RETRY_MS;
				this._log(`automatic reconciliation failed (retrying in an hour): ${e}`);
			} finally {
				this.autoRefreshInFlight = null;
			}
		})();
		return this.autoRefreshInFlight;
	},

	/**
	 * Watch for the user removing the rag-failed tag from an attachment; that is the
	 * request to retry it. Our own removals (on an `unfailed` event) also arrive here,
	 * which is harmless: the backend ignores attachments without a failure record.
	 * @returns {void}
	 */
	_registerFailedTagObserver() {
		const observer = {
			/**
			 * @param {string} event
			 * @param {string} type
			 * @param {string[]} ids - "itemID-tagID"
			 * @param {Object<string, any>} extraData
			 */
			notify: async (event, type, ids, extraData) => {
				if (event !== 'remove' || type !== 'item-tag' || !this.isEnabled()) return;
				try {
					await this._reportRemovedFailedTags(ids, extraData);
				} catch (e) {
					this._log(`failed-tag removal report failed: ${e}`);
				}
			},
		};
		this.notifierID = Zotero.Notifier.registerObserver(observer, ['item-tag'], 'zotero-rag-failed-tag');
	},

	/**
	 * @param {string[]} ids
	 * @param {Object<string, any>} extraData
	 * @returns {Promise<void>}
	 */
	async _reportRemovedFailedTags(ids, extraData) {
		/** @type {Map<number, string[]>} */
		const byLibrary = new Map();
		for (const id of ids) {
			const [itemID, tagID] = id.split('-').map(Number);
			const name = extraData?.[id]?.tag ?? Zotero.Tags.getName(tagID);
			if (name !== this.failedTag) continue;
			const item = Zotero.Items.get(itemID);
			if (!item) continue;
			byLibrary.set(item.libraryID, [...(byLibrary.get(item.libraryID) || []), item.key]);
		}
		for (const [libraryID, keys] of byLibrary) {
			await this._api('/api/indexed-tags/failed/clear', {
				method: 'POST',
				body: { library_id: this.plugin.getBackendLibraryId(libraryID), attachment_keys: keys },
			});
		}
	},

	/**
	 * Apply one `ops` record emitted by the sync script. Operations older than a
	 * real-time event already applied for the same attachment are dropped.
	 * @param {string} slug
	 * @param {TagOp[]} ops
	 * @param {number} asOfSeq
	 * @returns {Promise<{added: number, removed: number, skipped: number}>}
	 */
	async applyOps(slug, ops, asOfSeq) {
		const result = { added: 0, removed: 0, skipped: 0 };
		const libraryID = this.slugToLibraryID(slug);
		if (libraryID === null) {
			result.skipped = ops.length;
			return result;
		}
		/** @type {string[]} */
		const removals = [];
		for (const op of ops) {
			const isFailed = op.kind === 'failed';
			const id = `${isFailed ? 'failed:' : ''}${libraryID}/${op.attachment_key}`;
			if ((this.appliedSeq.get(id) ?? -1) > asOfSeq) {
				result.skipped++;
				continue;
			}
			if (isFailed) {
				const item = Zotero.Items.getByLibraryAndKey(libraryID, op.attachment_key);
				const outcome = await this.setTagged(/** @type {any} */ (item), op.op === 'add', this.failedTag);
				if (outcome === 'changed') result[op.op === 'add' ? 'added' : 'removed']++;
				else if (outcome === 'skipped') result.skipped++;
				continue;
			}
			if (op.op === 'remove') {
				removals.push(op.attachment_key);
				continue;
			}
			const item = Zotero.Items.getByLibraryAndKey(libraryID, op.attachment_key);
			const outcome = await this.setTagged(/** @type {any} */ (item), true);
			if (outcome === 'changed') result.added++;
			else if (outcome === 'skipped') result.skipped++;
		}
		if (removals.length) {
			const r = await this.removeConfirmed(libraryID, removals);
			result.removed += r.removed;
			result.skipped += r.skipped;
		}
		return result;
	},

	/**
	 * Run a full reconciliation on the backend and apply its output as it streams in.
	 * @param {{onProgress?: function(RefreshStats): void, signal?: AbortSignal}} [opts]
	 * @returns {Promise<RefreshStats>}
	 */
	async refresh({ onProgress, signal } = {}) {
		/** @type {RefreshStats} */
		const stats = { librariesTotal: 0, librariesDone: 0, attachmentsChecked: 0, added: 0, removed: 0, skipped: 0, errors: [] };
		const started = await this._api('/api/indexed-tags/refresh', { method: 'POST' });
		this.tag = started.tag || this.tag;
		this.failedTag = started.failed_tag || this.failedTag;
		let offset = 0;
		// Attachments checked in libraries that already finished, so the running
		// total doesn't reset when the next library's own counter starts at 0.
		let checkedBefore = 0;
		for (;;) {
			if (signal?.aborted) throw new Error('Cancelled');
			const page = await this._api(`/api/indexed-tags/refresh/${started.run_id}?offset=${offset}`);
			offset = page.offset;
			for (const rec of page.records) {
				switch (rec.type) {
					case 'libraries':
						stats.librariesTotal = rec.libraries.length;
						break;
					case 'library_start':
						stats.currentLibrary = rec.library;
						break;
					case 'ops': {
						const r = await this.applyOps(rec.library, rec.ops, rec.as_of_seq);
						stats.added += r.added;
						stats.removed += r.removed;
						stats.skipped += r.skipped;
						break;
					}
					case 'progress':
						stats.attachmentsChecked = checkedBefore + rec.attachments_checked;
						break;
					case 'library_done':
						checkedBefore += rec.attachments_checked;
						stats.attachmentsChecked = checkedBefore;
						stats.librariesDone++;
						break;
					case 'library_error':
						stats.librariesDone++;
						stats.errors.push(`${rec.library}: ${rec.error}`);
						break;
					case 'error':
						throw new Error(rec.message || 'Tag sync failed');
					default:
						break;
				}
			}
			if (onProgress) onProgress({ ...stats });
			if (page.done) {
				Zotero.Prefs.set(this.PREF_LAST_REFRESH, String(Date.now()), true);
				return stats;
			}
			await new Promise(resolve => setTimeout(resolve, this.REFRESH_POLL_MS));
		}
	},
};
