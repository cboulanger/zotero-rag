// Tests for plugin/src/dialog.js's mergeDownloadFailures.
//
// dialog.js calls `ZoteroRAGDialog.init()` at the bottom, either immediately or
// on DOMContentLoaded depending on `document.readyState`. Providing a
// `document.addEventListener` that just records the callback (never invokes
// it) means init() never actually runs during load, regardless of
// `readyState` — so no further DOM stubbing is needed for this file.
//
// mergeDownloadFailures is called with an explicit `this` (via `.call()`)
// bound to a plain fake object, rather than through a real ZoteroRAGDialog
// instance — this file has no constructor/class to instantiate a fresh copy
// from (it's a single `var ZoteroRAGDialog = {...}` object), so binding a
// fake `this` is how each test gets an isolated instance's worth of state.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'dialog.js');

/** @returns {any} the ZoteroRAGDialog object (methods only — no live state) */
function loadDialogMethods() {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {},
		console,
	};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'dialog.js' });
	return context.ZoteroRAGDialog;
}

/**
 * Build a fake `this` for mergeDownloadFailures: a plugin stub recording
 * storeDownloadFailedItems calls, plus the count/callback state the real
 * ZoteroRAGDialog object carries.
 * @param {number} addedCount - What storeDownloadFailedItems should report as newly added
 * @param {number} currentCount - Pre-existing libraryMissingFilesCount for the library
 */
function makeFakeThis(addedCount, currentCount) {
	const storeCalls = [];
	const countUpdates = [];
	return {
		fakeThis: {
			plugin: {
				async storeDownloadFailedItems(libraryId, keys) {
					storeCalls.push({ libraryId, keys });
					return addedCount;
				},
			},
			libraryMissingFilesCount: new Map([['lib1', currentCount]]),
			onUnavailableCountUpdated(libraryId, count) { countUpdates.push({ libraryId, count }); },
		},
		storeCalls,
		countUpdates,
	};
}

test('mergeDownloadFailures does nothing when metadata has no failed downloads', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(0, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', { last_full_scan_failed_downloads: [] });
	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', null);

	assert.deepStrictEqual(storeCalls, []);
	assert.deepStrictEqual(countUpdates, []);
});

test('mergeDownloadFailures stores keys and bumps the count when new ones were added', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(2, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', {
		last_full_scan_failed_downloads: [
			{ item_key: 'A', attachment_key: 'ATT1' },
			{ item_key: 'B', attachment_key: 'ATT2' },
		],
	});

	assert.deepStrictEqual(storeCalls, [{ libraryId: 'lib1', keys: ['ATT1', 'ATT2'] }]);
	assert.deepStrictEqual(countUpdates, [{ libraryId: 'lib1', count: 7 }]); // 5 existing + 2 new
});

test('mergeDownloadFailures does not bump the count when nothing new was added', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(0, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', {
		last_full_scan_failed_downloads: [{ item_key: 'A', attachment_key: 'ATT1' }],
	});

	assert.deepStrictEqual(storeCalls, [{ libraryId: 'lib1', keys: ['ATT1'] }]);
	assert.deepStrictEqual(countUpdates, []);
});

test('resolveZoteroLibraryID delegates to the plugin\'s own resolver', () => {
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ZoteroRAGDialog = context.ZoteroRAGDialog;

	const calls = [];
	const fakeThis = {
		plugin: {
			_resolveZoteroLibraryID(id) {
				calls.push(id);
				return id === 'u12345' ? 1 : (id === '42' ? 99 : null);
			},
		},
	};

	assert.strictEqual(ZoteroRAGDialog.resolveZoteroLibraryID.call(fakeThis, 'u12345'), 1);
	assert.strictEqual(ZoteroRAGDialog.resolveZoteroLibraryID.call(fakeThis, '42'), 99);
	assert.deepStrictEqual(calls, ['u12345', '42']);
});

test('resolveZoteroLibraryID returns null when the plugin is not available', () => {
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ZoteroRAGDialog = context.ZoteroRAGDialog;

	const result = ZoteroRAGDialog.resolveZoteroLibraryID.call({ plugin: null }, 'u12345');

	assert.strictEqual(result, null);
});

test('buildConversationHistory maps turns to the backend ChatTurn shape', () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const fakeThis = {
		turns: [
			{ question: 'Q1', result: { answer: 'A1', agents_used: ['rag'], source_refs: ['c1'], query_plan: { agents_to_use: ['rag'] } } },
			{ question: 'Q2', result: { status: 'needs_clarification', answer: '', clarification_message: 'Narrow it down.', agents_used: [], source_refs: [], query_plan: null } },
		],
	};
	const history = ZoteroRAGDialog.buildConversationHistory.call(fakeThis);
	assert.strictEqual(history.length, 2);
	assert.strictEqual(history[0].answer, 'A1');
	assert.strictEqual(history[1].answer, 'Narrow it down.'); // clarification_message, not the empty answer
	assert.deepStrictEqual(history[0].source_refs, ['c1']);
});

test('buildConversationHistory returns an empty array with no turns yet', () => {
	const ZoteroRAGDialog = loadDialogMethods();
	assert.deepStrictEqual(ZoteroRAGDialog.buildConversationHistory.call({ turns: [] }), []);
});
