// Tests for plugin/src/fix-unavailable.js's row type-label logic.
//
// ZoteroFixUnavailableDialog auto-initializes at the bottom of the file
// (`ZoteroFixUnavailableDialog.init()`), but init() checks `window.arguments`
// first and returns immediately if it's missing — so a `window` stub with no
// `.arguments` property is enough to make loading the file side-effect-free.
// A `console` global must exist too, or the file's own console-shim IIFE
// would try to reference `Services`/`Cc`/`Ci`, which are therefore stubbed below.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'fix-unavailable.js');

/** @returns {any} a fresh ZoteroFixUnavailableDialog object */
function loadDialog() {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {
		window: {},
		document: {
			getElementById: () => ({ addEventListener: () => {} }),
		},
		console,
		Services: { console: { logStringMessage: () => {}, logMessage: () => {} } },
		Cc: {
			'@mozilla.org/scripterror;1': {
				createInstance: () => ({ init: () => {} }),
			},
		},
		Ci: { nsIScriptError: {} },
	};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'fix-unavailable.js' });
	return context.ZoteroFixUnavailableDialog;
}

test('_typeLabelFor prioritizes skipReason over everything else', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ skipReason: 'no text', isParseError: true, serverDownloadFailed: true }), 'empty');
	assert.strictEqual(dialog._typeLabelFor({ skipReason: 'timeout', isParseError: true }), 'timeout');
});

test('_typeLabelFor returns "parse err" for parse errors (when no skipReason)', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ isParseError: true, serverDownloadFailed: true }), 'parse err');
});

test('_typeLabelFor returns "srv fail" for server download failures (when no skipReason/parse error)', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ serverDownloadFailed: true, isLinked: true }), 'srv fail');
});

test('_typeLabelFor returns "linked" for linked files with no failure reason', () => {
	const dialog = loadDialog();
	assert.strictEqual(dialog._typeLabelFor({ isLinked: true }), 'linked');
});

test('_typeLabelFor falls back to the file type label', () => {
	const dialog = loadDialog();
	dialog.getFileTypeLabel = () => 'PDF';
	assert.strictEqual(dialog._typeLabelFor({ attachmentItem: {} }), 'PDF');
});

test('searchAndFix prunes successfully-fixed serverDownloadFailed entries from the persistent store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0, 1, 2]);
	dialog.items = [
		{ attachmentItem: { key: 'ATT1' }, serverDownloadFailed: true, isLinked: false },
		{ attachmentItem: { key: 'ATT2' }, serverDownloadFailed: true, isLinked: false },
		// Not a server-download-failure row (e.g. found locally missing) — must
		// never be passed to removeDownloadFailedItems even though it's fixed.
		{ attachmentItem: { key: 'ATT3' }, isLinked: false },
	];
	const removedCalls = [];
	dialog.plugin = {
		_tryDownloadAttachment: async () => ({ downloaded: true }),
		removeDownloadFailedItems: async (libId, keys) => { removedCalls.push({ libId, keys }); },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removedCalls.length, 1);
	assert.strictEqual(removedCalls[0].libId, 'u1');
	assert.deepStrictEqual([...removedCalls[0].keys].sort(), ['ATT1', 'ATT2']);
});

test('searchAndFix does not call removeDownloadFailedItems when nothing was fixed', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'ATT1' }, serverDownloadFailed: true, isLinked: false },
	];
	let called = false;
	dialog.plugin = {
		_tryDownloadAttachment: async () => ({ downloaded: false, reason: 'still-missing' }),
		_searchAndFixUnavailableAttachment: async () => ({ found: false }),
		removeDownloadFailedItems: async () => { called = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(called, false);
});
