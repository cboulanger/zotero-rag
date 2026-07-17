// Tests for plugin/src/remote_indexer.js.
//
// remote_indexer.js is a plain script (not a CommonJS module — it's loaded by
// dialog.xhtml as a <script> tag inside Zotero's chrome environment, where
// `Zotero` is a global). To unit test it in plain Node without touching that
// loading contract, we read the source and evaluate it inside a vm context
// with a stubbed `Zotero` global, then pull the `RemoteIndexer` object (a
// top-level `var`) back out of that context.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'remote_indexer.js');

/**
 * Load RemoteIndexer into a fresh vm context with the given Zotero stub.
 * @param {any} zoteroStub
 * @returns {any} the RemoteIndexer object
 */
function loadRemoteIndexer(zoteroStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'remote_indexer.js' });
	return context.RemoteIndexer;
}

/**
 * Build a minimal Zotero stub whose Zotero.Search() returns a fake search
 * object recording every addCondition(...) call, and whose .search() /
 * Zotero.Items.getAsync() resolve to an empty item list (sufficient for
 * exercising the trash-exclusion call itself).
 * @returns {{ zotero: any, addedConditions: Array<any[]> }}
 */
function makeZoteroStub() {
	const addedConditions = [];
	const fakeSearch = {
		libraryID: null,
		addCondition(...args) { addedConditions.push(args); },
		async search() { return []; },
	};
	const zotero = {
		Groups: { get: () => ({ libraryID: 1 }) },
		Libraries: { userLibraryID: 1 },
		Search: function () { return fakeSearch; },
		Items: { getAsync: async () => [] },
	};
	return { zotero, addedConditions };
}

test('countIndexableAttachments excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer.countIndexableAttachments('123', 'group');

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

test('_collectAttachments excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer._collectAttachments('123', 'group', () => {});

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

test('_collectAbstractItems excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer._collectAbstractItems('123', 'group', () => {}, []);

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});
