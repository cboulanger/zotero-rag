// Tests for plugin/src/zotero-rag.js's download-failure storage and merge logic.
//
// zotero-rag.js defines `class ZoteroRAGPlugin` and instantiates a singleton at
// the bottom (`ZoteroRAG = new ZoteroRAGPlugin();`). The constructor does no
// Zotero-global work, so — same technique as plugin/test/remote_indexer.test.js —
// we evaluate the source inside a vm context with a stubbed `Zotero`/`IOUtils`/
// `PathUtils`, then construct a fresh instance per test from the context's
// `ZoteroRAGPlugin` class (not the singleton, so each test starts clean).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'zotero-rag.js');

/**
 * Build a minimal Zotero/IOUtils/PathUtils stub backed by an in-memory fake
 * filesystem (a plain object keyed by path) and a map of library attachments.
 * @param {Record<string, any>} attachmentsByKey - key -> fake attachment object
 * @returns {{ zotero: any, ioUtils: any, pathUtils: any, files: Record<string, string> }}
 */
function makeStubs(attachmentsByKey = {}) {
	const files = {};
	const ioUtils = {
		async readUTF8(filePath) {
			if (!(filePath in files)) throw new Error('ENOENT');
			return files[filePath];
		},
		async writeUTF8(filePath, text) { files[filePath] = text; },
		async makeDirectory() {},
	};
	const pathUtils = { join: (...parts) => parts.join('/') };
	const zotero = {
		DataDirectory: { dir: '/fake/zotero/data' },
		Libraries: { userLibraryID: 1 },
		Items: {
			async getByLibraryAndKeyAsync(_libraryID, key) {
				return attachmentsByKey[key] || null;
			},
			async getAsync(id) {
				return attachmentsByKey[`__parent_${id}`] || null;
			},
		},
		Sync: {
			Storage: {
				Local: {
					SYNC_STATE_TO_UPLOAD: 0,
					getModeForLibrary: () => 'zfs',
				},
			},
		},
	};
	return { zotero, ioUtils, pathUtils, files };
}

/**
 * Load a fresh ZoteroRAGPlugin instance into a vm context with the given stubs.
 * @param {any} zoteroStub
 * @param {any} ioUtilsStub
 * @param {any} pathUtilsStub
 * @param {Record<string, any>} [extra] - Extra globals to add to the vm context (e.g. TaskQueue, fetch)
 * @returns {any} a new ZoteroRAGPlugin instance
 */
function loadPlugin(zoteroStub, ioUtilsStub, pathUtilsStub, extra = {}) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, IOUtils: ioUtilsStub, PathUtils: pathUtilsStub, console, ...extra };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'zotero-rag.js' });
	// `class ZoteroRAGPlugin` is a top-level class declaration, not a `var` —
	// it lives in the context's global lexical environment, not as a property
	// on the context object itself. Pull it out with a second script eval in
	// the same context (lexical bindings persist across runInContext calls on
	// the same context object).
	const ZoteroRAGPluginClass = vm.runInContext('ZoteroRAGPlugin', context);
	return new ZoteroRAGPluginClass();
}

test('storeDownloadFailedItems merges keys and returns the count of new ones', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	const firstAdded = await plugin.storeDownloadFailedItems('u1', ['ATT1', 'ATT2']);
	assert.strictEqual(firstAdded, 2);

	const secondAdded = await plugin.storeDownloadFailedItems('u1', ['ATT2', 'ATT3']);
	assert.strictEqual(secondAdded, 1); // ATT2 already stored, only ATT3 is new
});

test('removeDownloadFailedItems prunes fixed keys so they stop reappearing', async () => {
	const fakeAttachment = (key) => ({
		deleted: false,
		parentItemID: null,
		key,
		isImportedAttachment: () => true,
		getCreators: () => [],
		getField: () => '',
	});
	const { zotero, ioUtils, pathUtils } = makeStubs({
		ATT1: fakeAttachment('ATT1'),
		ATT2: fakeAttachment('ATT2'),
		ATT3: fakeAttachment('ATT3'),
	});
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeDownloadFailedItems('u1', ['ATT1', 'ATT2', 'ATT3']);
	await plugin.removeDownloadFailedItems('u1', ['ATT1', 'ATT3']);

	const results = await plugin._getDownloadFailedAttachments(1);
	assert.strictEqual(results.length, 1);
	assert.strictEqual(results[0].attachmentItem.key, 'ATT2');
});

test('removeDownloadFailedItems is a no-op when the store does not exist yet', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	// Must not throw even though no file has ever been written
	await plugin.removeDownloadFailedItems('u1', ['ATT1']);
});

test('_getDownloadFailedAttachments resolves stored keys with serverDownloadFailed set, no skipReason/isParseError', async () => {
	const fakeAttachment = {
		deleted: false,
		parentItemID: null,
		key: 'ATT1',
		isImportedAttachment: () => true,
		getCreators: () => [{ lastName: 'Doe' }],
		getField: (f) => (f === 'title' ? 'A Paper' : f === 'date' ? '2020' : ''),
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ ATT1: fakeAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeDownloadFailedItems('u1', ['ATT1']);
	const results = await plugin._getDownloadFailedAttachments(1);

	assert.strictEqual(results.length, 1);
	assert.strictEqual(results[0].serverDownloadFailed, true);
	assert.strictEqual(results[0].isParseError, undefined);
	assert.strictEqual(results[0].skipReason, undefined);
	assert.strictEqual(results[0].authors, 'Doe');
	assert.strictEqual(results[0].year, '2020');
	assert.strictEqual(results[0].title, 'A Paper');
});

test('_getDownloadFailedAttachments sets isLinked from the attachment\'s actual import status, not hardcoded false', async () => {
	// Regression test: a server-reported download failure for a genuine link
	// attachment (LINK_MODE_LINKED_URL — a bare web link with no stored file,
	// the norm for these server-reported entries) was previously always marked
	// isLinked=false, routing it into fix-unavailable's "imported" retry bucket.
	// That bucket's copy-based repair strategies end by calling
	// attachmentItem.fileExists(), which Zotero itself throws on for
	// LINK_MODE_LINKED_URL items ("Zotero.Item.fileExists() cannot be called on
	// link attachments"), surfacing as a "Copy failed" error for every such item
	// instead of the correct "Linked file — fix path in Zotero" skip.
	//
	// An earlier version of this fix checked `attachmentLinkMode === 2`
	// (LINK_MODE_LINKED_FILE) — the wrong constant: fileExists() only throws for
	// LINK_MODE_LINKED_URL (3), not LINK_MODE_LINKED_FILE (2), so that check
	// never matched the attachments actually hitting this bug. Using Zotero's
	// own isImportedAttachment() avoids hardcoding either numeric constant.
	const linkedAttachment = {
		deleted: false,
		parentItemID: null,
		key: 'ATT1',
		isImportedAttachment: () => false, // LINK_MODE_LINKED_URL in real Zotero
		getCreators: () => [],
		getField: () => '',
	};
	const importedAttachment = {
		deleted: false,
		parentItemID: null,
		key: 'ATT2',
		isImportedAttachment: () => true, // LINK_MODE_IMPORTED_FILE in real Zotero
		getCreators: () => [],
		getField: () => '',
	};
	const { zotero, ioUtils, pathUtils } = makeStubs({ ATT1: linkedAttachment, ATT2: importedAttachment });
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeDownloadFailedItems('u1', ['ATT1', 'ATT2']);
	const results = await plugin._getDownloadFailedAttachments(1);

	const byKey = Object.fromEntries(results.map(r => [r.attachmentItem.key, r]));
	assert.strictEqual(byKey.ATT1.isLinked, true);
	assert.strictEqual(byKey.ATT2.isLinked, false);
});

test('_getDownloadFailedAttachments drops keys whose Zotero item no longer exists', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs({}); // ATT1 resolves to null
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeDownloadFailedItems('u1', ['ATT1']);
	const results = await plugin._getDownloadFailedAttachments(1);

	// Spread into a plain array first: `results` (even when empty) is an Array
	// from the vm context's separate realm, and assert.deepStrictEqual treats
	// same-shape-but-cross-realm objects as unequal ("not reference-equal").
	assert.deepStrictEqual([...results], []);
});

test('_describeDownloadFailureReason explains a linked attachment as having no file to download', () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	const { downloadFailureReason } = plugin._describeDownloadFailureReason({}, true, 1);
	assert.match(downloadFailureReason, /no file to download/i);
});

test('_describeDownloadFailureReason flags WebDAV-mode libraries, which the backend can never reach', () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Sync.Storage.Local.getModeForLibrary = () => 'webdav';
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	const { downloadFailureReason } = plugin._describeDownloadFailureReason({ attachmentSyncState: 2 }, false, 1);
	assert.match(downloadFailureReason, /webdav/i);
});

test('_describeDownloadFailureReason flags a zfs-mode attachment never uploaded by any device', () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	const { downloadFailureReason } = plugin._describeDownloadFailureReason(
		{ attachmentSyncState: zotero.Sync.Storage.Local.SYNC_STATE_TO_UPLOAD }, false, 1
	);
	assert.match(downloadFailureReason, /not yet uploaded/i);
});

test('_getUnavailableAttachments includes download-failed entries only when includeDownloadFailed is true', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.DB = { columnQueryAsync: async () => [] };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);
	plugin._getParseErrorAttachments = async () => [];
	plugin._getSkippedServerAttachments = async () => [];
	plugin._getDownloadFailedAttachments = async () => [
		{ attachmentItem: { key: 'DL1' }, parentItem: {}, authors: '', year: '', title: '', zoteroID: 'P1', isLinked: false, serverDownloadFailed: true },
	];

	const withoutFlag = await plugin._getUnavailableAttachments(1);
	assert.strictEqual(withoutFlag.length, 0);

	const withFlag = await plugin._getUnavailableAttachments(1, { includeDownloadFailed: true });
	assert.strictEqual(withFlag.length, 1);
	assert.strictEqual(withFlag[0].attachmentItem.key, 'DL1');
});

test('retryDownloadFailedAttachment reports stillMissing, without attempting an upload, when the download itself fails', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Sync.Storage.Local.getEnabledForLibrary = () => true;
	zotero.Sync.Runner = { downloadFile: async () => { throw new Error('nope'); } };
	let uploadCalled = false;
	const RemoteIndexer = { _uploadAttachment: async () => { uploadCalled = true; return {}; } };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });

	const attachmentItem = { libraryID: 1, key: 'ATT1', fileExists: async () => false };
	const result = await plugin.retryDownloadFailedAttachment(attachmentItem, null, 1);

	assert.strictEqual(result.fixed, false);
	assert.strictEqual(result.stillMissing, true);
	assert.strictEqual(uploadCalled, false);
});

test('retryDownloadFailedAttachment downloads then uploads to the backend, returning fixed:true on success', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Sync.Storage.Local.getEnabledForLibrary = () => true;
	zotero.Sync.Runner = { downloadFile: async () => {} };
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	const uploadCalls = [];
	const RemoteIndexer = {
		_uploadAttachment: async (opts) => { uploadCalls.push(opts); return { rateLimitHeaders: null }; },
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.getCurrentZoteroUserId = () => 1;
	plugin.backendURL = 'http://backend';

	const attachmentItem = {
		libraryID: 1, key: 'ATT1', version: 5, attachmentContentType: 'application/pdf',
		fileExists: async () => true,
	};
	const result = await plugin.retryDownloadFailedAttachment(attachmentItem, null, 1);

	assert.strictEqual(result.fixed, true);
	assert.strictEqual(uploadCalls.length, 1);
	assert.strictEqual(uploadCalls[0].att.attachment_key, 'ATT1');
	assert.strictEqual(uploadCalls[0].libraryId, 'u1');
});

test('retryDownloadFailedAttachment treats a parse-error upload result as not fixed, with a descriptive error', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Sync.Storage.Local.getEnabledForLibrary = () => true;
	zotero.Sync.Runner = { downloadFile: async () => {} };
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	const RemoteIndexer = { _uploadAttachment: async () => ({ parseError: true, errorDetail: 'binary junk' }) };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.getCurrentZoteroUserId = () => 1;
	plugin.backendURL = 'http://backend';

	const attachmentItem = { libraryID: 1, key: 'ATT1', fileExists: async () => true };
	const result = await plugin.retryDownloadFailedAttachment(attachmentItem, null, 1);

	assert.strictEqual(result.fixed, false);
	assert.strictEqual(result.stillMissing, false);
	assert.strictEqual(result.error, 'binary junk');
});

test('_uploadDownloadFailedAttachment returns queued:true when the upload is deferred', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	let capturedDefer;
	const RemoteIndexer = {
		_uploadAttachment: async (opts) => {
			capturedDefer = opts.defer;
			return { queued: true, eta: '2026-10-06T15:30:00Z', queueBlockReason: 'key_invalid' };
		},
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.getCurrentZoteroUserId = () => 1;
	plugin.backendURL = 'http://backend';

	const result = await plugin._uploadDownloadFailedAttachment(
		{ key: 'A', attachmentContentType: 'application/pdf', version: 1 },
		{ key: 'I', version: 1 },
		1,
		{ defer: true },
	);

	assert.strictEqual(capturedDefer, true);
	assert.strictEqual(result.fixed, false);
	assert.strictEqual(result.queued, true);
	assert.strictEqual(result.eta, '2026-10-06T15:30:00Z');
	assert.strictEqual(result.queueBlockReason, 'key_invalid');
});

test('retryTimeoutSkippedAttachment returns queued:true with the server-reported queueBlockReason when deferred', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	const RemoteIndexer = {
		_uploadAttachment: async () => ({ queued: true, eta: '2026-10-06T16:00:00Z', queueBlockReason: 'paused' }),
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.getCurrentZoteroUserId = () => 1;
	plugin.backendURL = 'http://backend';

	const result = await plugin.retryTimeoutSkippedAttachment(
		{ key: 'A', attachmentContentType: 'application/pdf', version: 1 },
		{ key: 'I', version: 1 },
		1,
		{ defer: true },
	);

	assert.strictEqual(result.fixed, false);
	assert.strictEqual(result.stillTimedOut, false);
	assert.strictEqual(result.queued, true);
	assert.strictEqual(result.eta, '2026-10-06T16:00:00Z');
	assert.strictEqual(result.queueBlockReason, 'paused');
});

test('retryEmptyTextSkippedAttachment returns queued:true with the server-reported queueBlockReason when deferred', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	const RemoteIndexer = {
		_uploadAttachment: async () => ({ queued: true, eta: '2026-10-06T16:00:00Z', queueBlockReason: 'key_invalid' }),
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.getCurrentZoteroUserId = () => 1;
	plugin.backendURL = 'http://backend';

	const result = await plugin.retryEmptyTextSkippedAttachment(
		{ key: 'A', attachmentContentType: 'application/pdf', version: 1 },
		{ key: 'I', version: 1 },
		1,
		{ defer: true },
	);

	assert.strictEqual(result.fixed, false);
	assert.strictEqual(result.stillEmpty, false);
	assert.strictEqual(result.queued, true);
	assert.strictEqual(result.eta, '2026-10-06T16:00:00Z');
	assert.strictEqual(result.queueBlockReason, 'key_invalid');
});

test('processQueuedAttachmentNow calls RemoteIndexer._processQueuedNow and maps success to fixed:true', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	let capturedOpts;
	const RemoteIndexer = { _processQueuedNow: async (opts) => { capturedOpts = opts; return {}; } };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';

	const result = await plugin.processQueuedAttachmentNow(
		{ key: 'A', attachmentContentType: 'application/pdf', version: 1 },
		{ key: 'I', version: 1 },
		1,
	);

	assert.strictEqual(result.fixed, true);
	assert.strictEqual(capturedOpts.attachmentKey, 'A');
	assert.strictEqual(capturedOpts.libraryId, 'u1');
	assert.strictEqual(capturedOpts.backendURL, 'http://backend');
});

test('processQueuedAttachmentNow maps a skippedTimeout result to fixed:false with a descriptive error', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	zotero.Libraries.get = () => ({ libraryType: 'user' });
	const RemoteIndexer = { _processQueuedNow: async () => ({ skippedTimeout: true, errorDetail: null }) };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';

	const result = await plugin.processQueuedAttachmentNow(
		{ key: 'A', attachmentContentType: 'application/pdf', version: 1 },
		{ key: 'I', version: 1 },
		1,
	);

	assert.strictEqual(result.fixed, false);
	assert.strictEqual(result.error, 'Text extraction timed out');
});

test('getQueuedStatusMap returns only attachments the backend reports as queued', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const RemoteIndexer = {
		_checkIndexed: async () => ([
			{ item_key: 'I1', attachment_key: 'A1', needs_indexing: false, reason: 'queued', eta: '2026-10-06T15:30:00Z', queue_block_reason: null },
			{ item_key: 'I2', attachment_key: 'A2', needs_indexing: true, reason: 'not_indexed' },
		]),
	};
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';

	const items = [
		{ parentItem: { key: 'I1', version: 1 }, attachmentItem: { key: 'A1', version: 1, attachmentContentType: 'application/pdf' } },
		{ parentItem: { key: 'I2', version: 1 }, attachmentItem: { key: 'A2', version: 1, attachmentContentType: 'application/pdf' } },
	];
	const map = await plugin.getQueuedStatusMap(1, items);
	assert.strictEqual(map.size, 1);
	// Spread into a plain object first: the value is an object literal from the
	// vm context's separate realm, and assert.deepStrictEqual treats
	// same-shape-but-cross-realm objects as unequal ("not reference-equal").
	assert.deepStrictEqual({ ...map.get('A1') }, { eta: '2026-10-06T15:30:00Z', queueBlockReason: null });
	assert.strictEqual(map.has('A2'), false);
});

test('getQueuedStatusMap returns an empty map for an empty item list without calling the backend', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	let called = false;
	const RemoteIndexer = { _checkIndexed: async () => { called = true; return []; } };
	const plugin = loadPlugin(zotero, ioUtils, pathUtils, { RemoteIndexer });
	plugin.getBackendLibraryId = () => 'u1';
	plugin.getAuthHeaders = () => ({});
	plugin.backendURL = 'http://backend';

	const map = await plugin.getQueuedStatusMap(1, []);
	assert.strictEqual(map.size, 0);
	assert.strictEqual(called, false);
});

test('getBackendLibraryId returns "u{userId}" for the personal library', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {});

	assert.strictEqual(plugin.getBackendLibraryId(1), 'u12345');
});

test('getBackendLibraryId returns the numeric group id for a group library', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: (/** @type {number} */ id) => (id === 7 ? { id: 999 } : null) },
	};
	const plugin = loadPlugin(zotero, {}, {});

	assert.strictEqual(plugin.getBackendLibraryId(7), '999');
});

test('getBackendLibraryId falls back to the raw libraryID when unsynced and not a group', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => null },
		Groups: { getByLibraryID: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {});

	assert.strictEqual(plugin.getBackendLibraryId(1), '1');
});

test('the item-delete notifier maps the internal libraryID to the backend library_id in the DELETE URL', () => {
	/** @type {string[]} */
	const deletedUrls = [];
	/** @type {any} */
	let capturedObserver;
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: (/** @type {number} */ id) => (id === 7 ? { id: 999 } : null) },
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const fetchStub = (/** @type {string} */ url) => { deletedUrls.push(url); return Promise.resolve({ ok: true }); };
	// plugin.init() logs via this.log() -> console.log(), and the file's own
	// console-shim IIFE (top of zotero-rag.js) rewires console.log to route
	// through Services.console.logStringMessage — stub Services so that
	// doesn't throw (same pattern as plugin/test/fix-unavailable.test.js).
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	// init() now also registers the metadata dispatcher and starts the queue's
	// heartbeat — stub those no-ops since this test only exercises the delete path.
	const taskQueueStub = { start: () => {}, registerDispatcher: () => {} };
	const plugin = loadPlugin(zotero, {}, {}, { fetch: fetchStub, Services: servicesStub, TaskQueue: taskQueueStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	capturedObserver.notify('delete', 'item', [1], { 1: { libraryID: 1, key: 'ITEM1' } });
	capturedObserver.notify('delete', 'item', [2], { 2: { libraryID: 7, key: 'ITEM2' } });

	assert.strictEqual(deletedUrls[0], 'http://localhost:8119/api/libraries/u12345/items/ITEM1/chunks');
	assert.strictEqual(deletedUrls[1], 'http://localhost:8119/api/libraries/999/items/ITEM2/chunks');
});

test('_extractAuthors returns "First Last" for authors and editors, skipping other creator types', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		CreatorTypes: { getID: (/** @type {string} */ name) => (/** @type {Record<string, number>} */ ({ author: 1, editor: 2, contributor: 3 }))[name] },
	};
	const plugin = loadPlugin(zotero, {}, {});
	const item = {
		getCreators: () => [
			{ creatorTypeID: 1, firstName: 'Jane', lastName: 'Doe' },
			{ creatorTypeID: 3, firstName: 'Ignored', lastName: 'Contributor' },
			{ creatorTypeID: 2, firstName: '', lastName: 'Smith' },
		],
	};

	assert.deepStrictEqual(plugin._extractAuthors(item), ['Jane Doe', 'Smith']);
});

test('_extractYear extracts a 4-digit year from the date field', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const item = { getField: (/** @type {string} */ f) => (f === 'date' ? 'March 3, 2021' : '') };

	assert.strictEqual(plugin._extractYear(item), 2021);
});

test('_extractYear returns null when there is no parseable year', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const item = { getField: () => '' };

	assert.strictEqual(plugin._extractYear(item), null);
});

test('_extractTags maps Zotero tag objects to a plain string array, dropping empty tags', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const item = { getTags: () => [{ tag: 'Law', type: 0 }, { tag: 'Automatic', type: 1 }, { tag: '' }] };

	assert.deepStrictEqual(plugin._extractTags(item), ['Law', 'Automatic']);
});

test('the item-modify notifier enqueues a metadata task for top-level regular items', () => {
	/** @type {any[]} */
	const enqueued = [];
	/** @type {any} */
	let capturedObserver;
	const fakeItem = {
		key: 'ITEM1', libraryID: 1, version: 7, dateModified: '2026-01-01T00:00:00Z',
		itemType: 'journalArticle',
		isRegularItem: () => true,
		getField: (/** @type {string} */ f) => (f === 'title' ? 'A Title' : ''),
		getCreators: () => [],
		getTags: () => [{ tag: 'Law' }],
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: () => null },
		Items: { get: (/** @type {number} */ id) => (id === 42 ? fakeItem : null) },
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const taskQueueStub = {
		enqueue: (/** @type {any[]} */ ...args) => enqueued.push(args),
		start: () => {},
		registerDispatcher: () => {},
	};
	// plugin.init() logs via this.log() -> console.log(), routed through
	// Services.console.logStringMessage — stub Services so that doesn't throw
	// (same pattern as the item-delete notifier test above).
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, Services: servicesStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	capturedObserver.notify('modify', 'item', [42], {});

	assert.strictEqual(enqueued.length, 1);
	const [type, key, payload, debounceMs] = enqueued[0];
	assert.strictEqual(type, 'metadata');
	assert.strictEqual(key, 'u12345:ITEM1');
	assert.strictEqual(payload.title, 'A Title');
	assert.deepStrictEqual(payload.tags, ['Law']);
	assert.strictEqual(payload.item_version, 7);
	assert.strictEqual(debounceMs, 4000);
});

test('the item-modify notifier ignores non-regular items (attachments, notes)', () => {
	/** @type {any[]} */
	const enqueued = [];
	/** @type {any} */
	let capturedObserver;
	const fakeItem = { key: 'ATT1', libraryID: 1, isRegularItem: () => false };
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: { get: () => fakeItem },
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const taskQueueStub = { enqueue: (/** @type {any[]} */ ...args) => enqueued.push(args), start: () => {}, registerDispatcher: () => {} };
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, Services: servicesStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	capturedObserver.notify('modify', 'item', [1], {});

	assert.strictEqual(enqueued.length, 0);
});

test('the item-modify notifier isolates per-item failures: one throwing item does not block the rest of the batch', () => {
	/** @type {any[]} */
	const enqueued = [];
	/** @type {any} */
	let capturedObserver;
	const goodItem = {
		key: 'ITEM2', libraryID: 1, version: 3, dateModified: '2026-01-02T00:00:00Z',
		itemType: 'book',
		isRegularItem: () => true,
		getField: (/** @type {string} */ f) => (f === 'title' ? 'Good Title' : ''),
		getCreators: () => [],
		getTags: () => [],
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: () => null },
		Items: {
			get: (/** @type {number} */ id) => {
				if (id === 1) throw new Error('simulated failure reading item 1');
				if (id === 2) return goodItem;
				return null;
			},
		},
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const taskQueueStub = {
		enqueue: (/** @type {any[]} */ ...args) => enqueued.push(args),
		start: () => {},
		registerDispatcher: () => {},
	};
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	// The per-item catch block logs via console.warn(), which the file's own
	// console-shim IIFE routes through Cc/Ci (nsIScriptError) rather than
	// Services.console.logStringMessage — stub those too (same pattern as
	// plugin/test/fix-unavailable.test.js).
	const ccStub = { '@mozilla.org/scripterror;1': { createInstance: () => ({ init: () => {} }) } };
	const ciStub = { nsIScriptError: {} };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, Services: servicesStub, Cc: ccStub, Ci: ciStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	// id 1 throws when Zotero.Items.get() is called; id 2 is a normal valid item.
	// The failure on id 1 must not prevent id 2 from being enqueued.
	capturedObserver.notify('modify', 'item', [1, 2], {});

	assert.strictEqual(enqueued.length, 1);
	const [type, key, payload] = enqueued[0];
	assert.strictEqual(type, 'metadata');
	assert.strictEqual(key, 'u12345:ITEM2');
	assert.strictEqual(payload.title, 'Good Title');
});

test('the metadata dispatcher POSTs one request per library_id and reports succeeded keys', async () => {
	/** @type {Array<{url: string, init: any}>} */
	const fetchCalls = [];
	const fetchStub = async (/** @type {string} */ url, /** @type {any} */ init) => {
		fetchCalls.push({ url, init });
		return { ok: true, status: 200, json: async () => ({ updated_items: 1, updated_chunks: 1 }) };
	};
	/** @type {any} */
	let registeredDispatcher;
	const taskQueueStub = {
		start: () => {},
		registerDispatcher: (/** @type {string} */ type, /** @type {any} */ fn) => { if (type === 'metadata') registeredDispatcher = fn; },
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
	};
	// init() logs a "toolkit not loaded" warning via console.log(), which the
	// file's own console-shim IIFE routes through Services.console.logStringMessage
	// — stub Services so that doesn't throw (same pattern as other tests above).
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, fetch: fetchStub, Services: servicesStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	assert.strictEqual(typeof registeredDispatcher, 'function');

	const result = await registeredDispatcher([
		{ type: 'metadata', key: 'u123:ITEM1', payload: { item_key: 'ITEM1', title: 'A' } },
		{ type: 'metadata', key: 'u123:ITEM2', payload: { item_key: 'ITEM2', title: 'B' } },
		{ type: 'metadata', key: 'u456:ITEM3', payload: { item_key: 'ITEM3', title: 'C' } },
	]);

	assert.strictEqual(fetchCalls.length, 2); // one request per distinct library_id
	const bodies = fetchCalls.map(c => JSON.parse(c.init.body));
	const u123Body = bodies.find(b => b.library_id === 'u123');
	assert.strictEqual(u123Body.items.length, 2);
	assert.deepStrictEqual([...result.succeededKeys].sort(), ['u123:ITEM1', 'u123:ITEM2', 'u456:ITEM3'].sort());
});

test('the metadata dispatcher reports failed:true (without throwing) when the backend returns a non-2xx response', async () => {
	const fetchStub = async () => ({ ok: false, status: 500 });
	/** @type {any} */
	let registeredDispatcher;
	const taskQueueStub = { start: () => {}, registerDispatcher: (/** @type {string} */ _type, /** @type {any} */ fn) => { registeredDispatcher = fn; } };
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
	};
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	// The per-library catch block logs via console.warn(), which the file's own
	// console-shim IIFE routes through Cc/Ci (nsIScriptError) rather than
	// Services.console.logStringMessage — stub those too (same pattern as
	// the "isolates per-item failures" test above).
	const ccStub = { '@mozilla.org/scripterror;1': { createInstance: () => ({ init: () => {} }) } };
	const ciStub = { nsIScriptError: {} };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, fetch: fetchStub, Services: servicesStub, Cc: ccStub, Ci: ciStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	// A per-library HTTP failure must NOT reject the dispatcher promise — doing
	// so would prevent TaskQueue from ever seeing succeededKeys for OTHER
	// libraries dispatched in the same batch (see the mixed-library test below).
	// Instead it resolves with an empty succeededKeys and failed: true, which
	// TaskQueue's _dispatchNext() treats as "apply backoff, but still trust
	// succeededKeys for what to keep vs. re-queue."
	const result = await registeredDispatcher([{ type: 'metadata', key: 'u1:ITEM1', payload: { item_key: 'ITEM1' } }]);
	assert.strictEqual(result.failed, true);
	assert.strictEqual(result.succeededKeys.size, 0);
});

test('the metadata dispatcher preserves partial success: a failing library does not discard another library\'s succeeded keys', async () => {
	const fetchStub = async (/** @type {string} */ _url, /** @type {any} */ init) => {
		const body = JSON.parse(init.body);
		if (body.library_id === 'u1') return { ok: true, status: 200, json: async () => ({ updated_items: body.items.length }) };
		return { ok: false, status: 500 };
	};
	/** @type {any} */
	let registeredDispatcher;
	const taskQueueStub = { start: () => {}, registerDispatcher: (/** @type {string} */ _type, /** @type {any} */ fn) => { registeredDispatcher = fn; } };
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
	};
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const ccStub = { '@mozilla.org/scripterror;1': { createInstance: () => ({ init: () => {} }) } };
	const ciStub = { nsIScriptError: {} };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, fetch: fetchStub, Services: servicesStub, Cc: ccStub, Ci: ciStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	const result = await registeredDispatcher([
		{ type: 'metadata', key: 'u1:ITEM1', payload: { item_key: 'ITEM1' } },
		{ type: 'metadata', key: 'u2:ITEM2', payload: { item_key: 'ITEM2' } },
	]);

	assert.strictEqual(result.failed, true);
	assert.deepStrictEqual([...result.succeededKeys], ['u1:ITEM1']);
});

test('submitQuery includes conversation_history in the payload when provided', async () => {
	/** @type {any} */
	let capturedBody = null;
	const fetchStub = async (/** @type {string} */ _url, /** @type {any} */ opts) => {
		capturedBody = JSON.parse(opts.body);
		return { ok: true, json: async () => ({ answer: 'ok' }) };
	};
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 }, Prefs: { get: () => undefined } }, {}, {}, { fetch: fetchStub });
	plugin.backendURL = 'http://localhost:8119';

	const history = [{ question: 'Q0', answer: 'A0', agents_used: ['rag'], source_refs: ['c1'], query_plan: null }];
	await plugin.submitQuery('Follow-up', ['1'], { conversationHistory: history });

	assert.deepStrictEqual(capturedBody.conversation_history, history);
});

test('submitQuery includes force_fresh_retrieval only when true', async () => {
	/** @type {any} */
	let capturedBody = null;
	const fetchStub = async (/** @type {string} */ _url, /** @type {any} */ opts) => {
		capturedBody = JSON.parse(opts.body);
		return { ok: true, json: async () => ({ answer: 'ok' }) };
	};
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 }, Prefs: { get: () => undefined } }, {}, {}, { fetch: fetchStub });
	plugin.backendURL = 'http://localhost:8119';

	await plugin.submitQuery('Q', ['1'], {});
	assert.strictEqual(capturedBody.force_fresh_retrieval, undefined);

	await plugin.submitQuery('Q', ['1'], { forceFreshRetrieval: true });
	assert.strictEqual(capturedBody.force_fresh_retrieval, true);
});

test('getDiversityTuningPayload returns hardcoded defaults when no prefs are set', () => {
	const zotero = { Libraries: { userLibraryID: 1 }, Prefs: { get: () => undefined } };
	const plugin = loadPlugin(zotero, {}, {});

	const payload = plugin.getDiversityTuningPayload();
	assert.strictEqual(payload.diversity_floor, 3);
	assert.strictEqual(payload.diversity_escalation_factor, 3);
	assert.strictEqual(payload.diversity_escalation_max_top_k, 30);
	assert.strictEqual(payload.max_chunks_per_document, 4);
	assert.strictEqual(payload.low_diversity_available_floor, 3);
});

test('getDiversityTuningPayload reflects stored pref overrides', () => {
	const stored = {
		'extensions.zotero-rag.diversityFloor': '5',
		'extensions.zotero-rag.maxChunksPerDocument': '2',
	};
	const zotero = { Libraries: { userLibraryID: 1 }, Prefs: { get: (/** @type {string} */ key) => stored[key] } };
	const plugin = loadPlugin(zotero, {}, {});

	const payload = plugin.getDiversityTuningPayload();
	assert.strictEqual(payload.diversity_floor, 5);
	assert.strictEqual(payload.max_chunks_per_document, 2);
	// Untouched fields still fall back to their defaults
	assert.strictEqual(payload.diversity_escalation_factor, 3);
});

test('submitQuery includes the diversity tuning payload read from Prefs on every request', async () => {
	/** @type {any} */
	let capturedBody = null;
	const fetchStub = async (/** @type {string} */ _url, /** @type {any} */ opts) => {
		capturedBody = JSON.parse(opts.body);
		return { ok: true, json: async () => ({ answer: 'ok' }) };
	};
	const stored = { 'extensions.zotero-rag.lowDiversityAvailableFloor': '7' };
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Prefs: { get: (/** @type {string} */ key) => stored[key] },
	};
	const plugin = loadPlugin(zotero, {}, {}, { fetch: fetchStub });
	plugin.backendURL = 'http://localhost:8119';

	await plugin.submitQuery('Q', ['1'], {});

	assert.strictEqual(capturedBody.low_diversity_available_floor, 7);
	assert.strictEqual(capturedBody.diversity_floor, 3);
});

test('formatTurnHTML renders question heading, answer, and bibliography without the outer wrapper', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const result = {
		answer: 'The answer.',
		answer_format: 'text',
		sources: [],
	};
	const html = plugin.formatTurnHTML('A follow-up question?', result, new Map());
	assert.ok(html.includes('A follow-up question?'));
	assert.ok(html.includes('The answer.'));
	assert.ok(!html.includes('Generated:')); // metadata footer belongs to formatNoteHTML only
});

test('formatTurnHTML resolves an inline [S1] citation to a Zotero item and lists it in the bibliography', () => {
	const fakeItem = {
		key: 'ITEM1',
		getCreators: () => [{ lastName: 'Smith', firstName: 'Jane' }],
		getField: (/** @type {string} */ f) => (f === 'title' ? 'A Great Paper' : f === 'date' ? '2020' : ''),
		getAttachments: () => [], // no PDF attachment -> falls back to zotero://select/
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: {
			getByLibraryAndKey: (/** @type {number} */ libraryID, /** @type {string} */ key) =>
				(libraryID === 1 && key === 'ITEM1') ? fakeItem : null,
		},
	};
	const plugin = loadPlugin(zotero, {}, {});

	/** @type {SourceCitation} */
	const source = {
		item_id: 'ITEM1',
		library_id: 'u12345',
		title: 'A Great Paper (fallback title)',
		page_number: null,
		text_anchor: null,
		relevance_score: 0.9,
	};
	const result = {
		answer: 'This claim is supported by prior work [S1].',
		answer_format: 'text',
		sources: [source],
	};
	// buildLibraryMap() would produce exactly this shape for a 'u12345' user library.
	const libraryMap = new Map([['u12345', { name: 'My Library', type: 'user' }]]);

	const html = plugin.formatTurnHTML('Does prior work support this?', result, libraryMap);

	// The [S1] marker must be gone, replaced by a resolved citation link using the
	// real Zotero item's author/year (not the raw source.title fallback) — proves
	// replaceCitationsInText() actually looked up the item via getZoteroItem().
	assert.ok(!html.includes('[S1]'), 'raw [S1] marker should have been replaced');
	assert.ok(html.includes('>(Smith, 2020)</a>'), `expected a resolved "Smith, 2020" citation link, got: ${html}`);
	assert.ok(html.includes('zotero://select/library/items/ITEM1'), 'citation link should point at the resolved Zotero item');

	// The bibliography section (formatBibliographyHTML) must list the same item,
	// formatted as "Author (Year) \"Title\"" using the real item's metadata.
	assert.ok(html.includes('<strong>References</strong>'), 'bibliography header missing');
	assert.ok(html.includes('Smith (2020) &quot;A Great Paper&quot;'), `expected bibliography entry for the resolved item, got: ${html}`);
	assert.ok(!html.includes('A Great Paper (fallback title)'), 'bibliography should use the real item title, not the source fallback title');
});

test('replaceCitationsInText resolves a page-range citation like [S1:305-306] instead of leaving it unreplaced', () => {
	// Observed live: a weaker model cited a page range ("305-306") despite the
	// prompt asking for a single integer page. The citation regex only accepted
	// digits/dots after the colon, so the whole bracket failed to match and
	// "[S1:305-306]" leaked into the rendered answer verbatim.
	const fakeItem = {
		key: 'ITEM1',
		getCreators: () => [{ lastName: 'Watkins', firstName: 'Alexander' }],
		getField: (/** @type {string} */ f) => (f === 'title' ? 'Zotero for Personal Image Management' : f === 'date' ? '2013' : ''),
		getAttachments: () => [],
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: {
			getByLibraryAndKey: (/** @type {number} */ libraryID, /** @type {string} */ key) =>
				(libraryID === 1 && key === 'ITEM1') ? fakeItem : null,
		},
	};
	const plugin = loadPlugin(zotero, {}, {});

	/** @type {SourceCitation} */
	const source = { item_id: 'ITEM1', library_id: 'u12345', title: 'Zotero for Personal Image Management', page_number: null, text_anchor: null, relevance_score: 0.9 };
	const libraryMap = new Map([['u12345', { name: 'My Library', type: 'user' }]]);

	const html = plugin.replaceCitationsInText('Image citations are tricky [S1:305-306].', [source], libraryMap);

	assert.ok(!html.includes('[S1:305-306]'), `raw page-range citation should have been replaced, got: ${html}`);
	assert.ok(html.includes('Watkins, 2013'), `expected the resolved item's author/year in the citation, got: ${html}`);
	assert.ok(html.includes('305-306'), `expected the page range preserved in the citation display text, got: ${html}`);
	assert.ok(html.includes('zotero://select/library/items/ITEM1'), `citation link should point at the resolved Zotero item, got: ${html}`);
});

test('replaceCitationsInText resolves a citation with a literal "P" page placeholder like [S2:P] instead of leaving it unreplaced', () => {
	// Observed live: the synthesis prompt shows "[SN:P]" as its own notation
	// example ("P is a single integer"), and a weaker model occasionally
	// echoed the placeholder letter "P" itself instead of substituting a real
	// page number. The old regex required digits after the colon, so the
	// whole bracket failed to match and "[S2:P]" leaked into the rendered
	// answer verbatim, unlinked.
	const fakeItem = {
		key: 'ITEM2',
		getCreators: () => [{ lastName: 'Ivey', firstName: 'Jane' }],
		getField: (/** @type {string} */ f) => (f === 'title' ? 'Manuscript Matcher' : f === 'date' ? '2023' : ''),
		getAttachments: () => [],
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: {
			getByLibraryAndKey: (/** @type {number} */ libraryID, /** @type {string} */ key) =>
				(libraryID === 1 && key === 'ITEM2') ? fakeItem : null,
		},
	};
	const plugin = loadPlugin(zotero, {}, {});

	/** @type {SourceCitation} */
	const source = { item_id: 'ITEM2', library_id: 'u12345', title: 'Manuscript Matcher', page_number: null, text_anchor: null, relevance_score: 0.9 };
	const libraryMap = new Map([['u12345', { name: 'My Library', type: 'user' }]]);

	const html = plugin.replaceCitationsInText('EndNote has a matcher feature [S2:P].', [null, source], libraryMap);

	assert.ok(!html.includes('[S2:P]'), `raw placeholder-page citation should have been replaced, got: ${html}`);
	assert.ok(html.includes('Ivey, 2023'), `expected the resolved item's author/year in the citation, got: ${html}`);
	assert.ok(!html.includes('p. P'), `the literal "P" placeholder should not be rendered as a page label, got: ${html}`);
	assert.ok(html.includes('zotero://select/library/items/ITEM2'), `citation link should point at the resolved Zotero item, got: ${html}`);
});

test('formatTurnHTML lists only the sources actually cited inline, not every retrieved source', () => {
	const items = {
		ITEM1: {
			key: 'ITEM1',
			getCreators: () => [{ lastName: 'Cited', firstName: 'Anne' }],
			getField: (/** @type {string} */ f) => (f === 'title' ? 'The Cited Paper' : f === 'date' ? '2020' : ''),
			getAttachments: () => [],
		},
		ITEM2: {
			key: 'ITEM2',
			getCreators: () => [{ lastName: 'Uncited', firstName: 'Bob' }],
			getField: (/** @type {string} */ f) => (f === 'title' ? 'The Uncited Paper' : f === 'date' ? '2021' : ''),
			getAttachments: () => [],
		},
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: {
			getByLibraryAndKey: (/** @type {number} */ libraryID, /** @type {string} */ key) =>
				libraryID === 1 ? (items[key] || null) : null,
		},
	};
	const plugin = loadPlugin(zotero, {}, {});

	/** @type {SourceCitation} */
	const source1 = { item_id: 'ITEM1', library_id: 'u12345', title: 'The Cited Paper', page_number: null, text_anchor: null, relevance_score: 0.9 };
	/** @type {SourceCitation} */
	const source2 = { item_id: 'ITEM2', library_id: 'u12345', title: 'The Uncited Paper', page_number: null, text_anchor: null, relevance_score: 0.8 };
	const result = {
		// Retrieval returned two documents (source1, source2), but the model
		// only found source1 relevant enough to cite.
		answer: 'This claim is supported by prior work [S1].',
		answer_format: 'text',
		sources: [source1, source2],
	};
	const libraryMap = new Map([['u12345', { name: 'My Library', type: 'user' }]]);

	const html = plugin.formatTurnHTML('Does prior work support this?', result, libraryMap);

	assert.ok(html.includes('>(Cited, 2020)</a>'), `expected the cited source's inline link, got: ${html}`);
	assert.ok(html.includes('Cited (2020)'), `expected the cited source in the bibliography, got: ${html}`);
	assert.ok(!html.includes('Uncited'), `bibliography must not list a source that was never cited, got: ${html}`);
});

test('formatTurnHTML falls back to listing all retrieved sources when the answer has no [SN] citations at all', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: { getByLibraryAndKey: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {});
	/** @type {SourceCitation} */
	const source = { item_id: 'ITEM1', library_id: 'u12345', title: 'A Great Paper', page_number: null, text_anchor: null, relevance_score: 0.9 };
	const result = {
		answer: 'This answer has no inline citation markers at all.',
		answer_format: 'text',
		sources: [source],
	};
	const html = plugin.formatTurnHTML('A question?', result, new Map());
	assert.ok(html.includes('A Great Paper'), `expected the uncited-but-retrieved source to still appear when nothing was cited, got: ${html}`);
});

test('formatTurnHTML renders the clarification message (not the empty answer) when status is needs_clarification', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const result = {
		status: 'needs_clarification',
		answer: '',
		clarification_message: 'Please narrow by year.',
		sources: [],
	};
	const html = plugin.formatTurnHTML('What has Luhmann written about?', result, new Map());
	assert.ok(html.includes('Please narrow by year.'), `expected clarification message in rendered HTML, got: ${html}`);
	// Must not silently render an empty answer paragraph with nothing in it.
	assert.ok(!/<p>\s*<\/p>/.test(html), `expected no empty answer paragraph, got: ${html}`);
});

test('formatNoteHTML joins multiple turns with a divider and appends a metadata footer built from the first turn', () => {
	const zotero = {
		Libraries: { userLibraryID: 1, get: () => ({ name: 'My Library' }) },
		Users: { getCurrentUserID: () => 12345, getCurrentUsername: () => 'tester' },
		Groups: { getAll: () => [] },
	};
	const plugin = loadPlugin(zotero, {}, {});
	plugin.version = '1.0.0';
	const turns = [
		{ question: 'Q1', result: { answer: 'A1', answer_format: 'text', sources: [], model_name: 'gpt', agents_used: ['rag'] } },
		{ question: 'Q2', result: { answer: 'A2', answer_format: 'text', sources: [], agents_used: ['continuation'] } },
	];
	const html = plugin.formatNoteHTML(turns, ['u12345']);

	assert.ok(html.includes('Q1') && html.includes('A1'));
	assert.ok(html.includes('Q2') && html.includes('A2'));
	assert.ok(html.includes('Model: gpt'), 'model comes from the first turn');
	assert.ok(html.includes('Agents: rag, continuation'), 'agents are the union across all turns');
	assert.ok(html.includes('Generated:'));
});

test('formatNoteHTML never embeds a debug trace — export is on-demand only', () => {
	const zotero = {
		Libraries: { userLibraryID: 1, get: () => ({ name: 'My Library' }) },
		Users: { getCurrentUserID: () => 12345, getCurrentUsername: () => 'tester' },
		Groups: { getAll: () => [] },
	};
	const plugin = loadPlugin(zotero, {}, {});
	plugin.version = '1.0.0';
	const turns = [{ question: 'Q', result: { answer: 'A', answer_format: 'text', sources: [], trace: { some: 'trace data' } } }];
	const html = plugin.formatNoteHTML(turns, ['u12345']);
	assert.ok(!html.includes('Debugging Trace'));
	assert.ok(!html.includes('trace data'));
});

test('createResultNote creates a tagged note from every turn and does not reference ChatPane', async () => {
	/** @type {string[]} */
	const noteHtmls = [];
	const noteStub = {
		id: 'note1',
		libraryID: 1,
		setNote(/** @type {string} */ html) { noteHtmls.push(html); },
		addToCollection() {},
		addTag() {},
		async saveTx() {},
	};

	const zoteroPaneStub = {
		getSelectedLibraryID: () => 1,
		getSelectedCollection: () => undefined,
		selectItem: async () => {},
	};

	const zotero = {
		Libraries: { userLibraryID: 1, get: () => ({ name: 'My Library' }) },
		Users: { getCurrentUserID: () => 12345, getCurrentUsername: () => 'tester' },
		Groups: { getAll: () => [] },
		Item: function (/** @type {string} */ _type) { return noteStub; },
		getActiveZoteroPane: () => zoteroPaneStub,
	};

	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const plugin = loadPlugin(zotero, {}, {}, { Services: servicesStub });
	plugin.version = '1.0.0';

	const turns = [
		{ question: 'Q1', result: { answer: 'A1', answer_format: 'text', sources: [] } },
		{ question: 'Q2', result: { answer: 'A2', answer_format: 'text', sources: [] } },
	];

	const note = await plugin.createResultNote(turns, ['u12345']);

	assert.strictEqual(note, noteStub);
	assert.strictEqual(noteHtmls.length, 1);
	assert.ok(noteHtmls[0].includes('Q1') && noteHtmls[0].includes('Q2'));
});

test('init() starts the TaskQueue and removeFromAllWindows() stops it', () => {
	/** @type {string[]} */
	const calls = [];
	const taskQueueStub = {
		start: () => calls.push('start'),
		stop: () => calls.push('stop'),
		registerDispatcher: () => {},
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
		getMainWindows: () => [],
	};
	const servicesStub = { console: { logStringMessage: () => {}, logMessage: () => {} } };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, Services: servicesStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });
	plugin.removeFromAllWindows();

	assert.deepStrictEqual(calls, ['start', 'stop']);
});

test('_getSelectedLibraryIDCompat prefers the Zotero 10+ plural getter when present', () => {
	const plugin = loadPlugin({}, {}, {});
	const pane = {
		getSelectedLibraryIDs: () => [7],
		getSelectedLibraryID: () => { throw new Error('should not be called'); },
	};
	assert.strictEqual(plugin._getSelectedLibraryIDCompat(pane), 7);
});

test('_getSelectedLibraryIDCompat falls back to the singular getter on pre-Zotero-10 panes', () => {
	const plugin = loadPlugin({}, {}, {});
	const pane = { getSelectedLibraryID: () => 3 };
	assert.strictEqual(plugin._getSelectedLibraryIDCompat(pane), 3);
});

test('_getSelectedLibraryIDCompat returns null when nothing is selected, on both pane shapes', () => {
	const plugin = loadPlugin({}, {}, {});
	assert.strictEqual(plugin._getSelectedLibraryIDCompat({ getSelectedLibraryIDs: () => [] }), null);
	assert.strictEqual(plugin._getSelectedLibraryIDCompat({ getSelectedLibraryID: () => false }), null);
});

test('_getSelectedCollectionIDCompat prefers the Zotero 10+ plural getter when present', () => {
	const plugin = loadPlugin({}, {}, {});
	const pane = {
		getSelectedCollections: () => [{ id: 42 }],
		getSelectedCollection: () => { throw new Error('should not be called'); },
	};
	assert.strictEqual(plugin._getSelectedCollectionIDCompat(pane), 42);
});

test('_getSelectedCollectionIDCompat falls back to the singular getter on pre-Zotero-10 panes', () => {
	const plugin = loadPlugin({}, {}, {});
	const pane = { getSelectedCollection: () => ({ id: 9 }) };
	assert.strictEqual(plugin._getSelectedCollectionIDCompat(pane), 9);
});

test('_getSelectedCollectionIDCompat returns null when no collection is selected, on both pane shapes', () => {
	const plugin = loadPlugin({}, {}, {});
	assert.strictEqual(plugin._getSelectedCollectionIDCompat({ getSelectedCollections: () => [] }), null);
	assert.strictEqual(plugin._getSelectedCollectionIDCompat({ getSelectedCollection: () => undefined }), null);
});

test('removeSkippedServerItems prunes only the given keys, preserving the rest', async () => {
	const fakeAttachment = (key) => ({
		deleted: false,
		parentItemID: null,
		key,
		getCreators: () => [],
		getField: () => '',
	});
	const { zotero, ioUtils, pathUtils } = makeStubs({
		KEEP: fakeAttachment('KEEP'),
		REMOVE: fakeAttachment('REMOVE'),
	});
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeSkippedServerItems('u1', [
		{ key: 'KEEP', reason: 'skipped_timeout' },
		{ key: 'REMOVE', reason: 'skipped_timeout' },
	]);
	await plugin.removeSkippedServerItems('u1', ['REMOVE']);

	const results = await plugin._getSkippedServerAttachments(1);
	assert.strictEqual(results.length, 1);
	assert.strictEqual(results[0].attachmentItem.key, 'KEEP');
});

test('removeSkippedServerItems is a no-op when the store does not exist yet', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	// Must not throw even though no file has ever been written
	await plugin.removeSkippedServerItems('u1', ['ANY']);
});
