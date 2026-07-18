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
