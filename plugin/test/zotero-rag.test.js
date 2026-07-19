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

test('_getDownloadFailedAttachments resolves stored keys with serverDownloadFailed set, no skipReason/isParseError', async () => {
	const fakeAttachment = {
		deleted: false,
		parentItemID: null,
		key: 'ATT1',
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
	const plugin = loadPlugin(zotero, {}, {}, { fetch: fetchStub, Services: servicesStub });
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
