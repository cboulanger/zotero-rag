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
		retryDownloadFailedAttachment: async () => ({ fixed: true, stillMissing: false }),
		removeDownloadFailedItems: async (libId, keys) => { removedCalls.push({ libId, keys }); },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removedCalls.length, 1);
	assert.strictEqual(removedCalls[0].libId, 'u1');
	assert.deepStrictEqual([...removedCalls[0].keys].sort(), ['ATT1', 'ATT2']);
});

test('searchAndFix removes fixed rows immediately and keeps unresolved rows with their status intact', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0, 1, 2]);
	dialog.items = [
		{ attachmentItem: { key: 'FIXED' }, serverDownloadFailed: true, isLinked: false },
		{ attachmentItem: { key: 'NOTFOUND' }, serverDownloadFailed: true, isLinked: false },
		{ attachmentItem: { key: 'ERRORED' }, serverDownloadFailed: true, isLinked: false },
	];
	dialog.plugin = {
		retryDownloadFailedAttachment: async (att) => (
			att.key === 'FIXED' ? { fixed: true, stillMissing: false } : { fixed: false, stillMissing: true }
		),
		_searchAndFixUnavailableAttachment: async (att) => {
			if (att.key === 'ERRORED') throw new Error('boom');
			return { found: false };
		},
		removeDownloadFailedItems: async () => {},
	};

	await dialog.searchAndFix();

	// The fixed row is gone immediately — no manual Refresh needed.
	assert.strictEqual(dialog.items.length, 2);
	assert.deepStrictEqual([...dialog.items.map(i => i.attachmentItem.key)], ['NOTFOUND', 'ERRORED']);
	// Unresolved rows survive with their just-set status intact — nothing wipes
	// rowStatus wholesale the way a full populateTable() re-fetch would.
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'not-found');
	assert.strictEqual(dialog.rowStatus.get(1)?.cssClass, 'error');
});

test('searchAndFix uploads to the backend after Phase 2 recovers a serverDownloadFailed file some other way', async () => {
	// retryDownloadFailedAttachment's own Zotero-sync download fails (e.g. the
	// server and the client both lack access to wherever this copy actually
	// lives), but Phase 2's other-library search finds a copy some other way.
	// Phase 2 only recovers the file — serverDownloadFailed rows still need an
	// explicit upload afterward, since nothing else will ever index them.
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'ATT1' }, serverDownloadFailed: true, isLinked: false },
	];
	const removedCalls = [];
	let uploadCalled = false;
	dialog.plugin = {
		retryDownloadFailedAttachment: async () => ({ fixed: false, stillMissing: true }),
		_searchAndFixUnavailableAttachment: async () => ({ found: true, via: 'md5' }),
		_uploadDownloadFailedAttachment: async () => { uploadCalled = true; return { fixed: true }; },
		removeDownloadFailedItems: async (libId, keys) => { removedCalls.push({ libId, keys }); },
	};

	await dialog.searchAndFix();

	assert.strictEqual(uploadCalled, true);
	assert.strictEqual(dialog.items.length, 0);
	assert.strictEqual(removedCalls.length, 1);
	assert.deepStrictEqual([...removedCalls[0].keys], ['ATT1']);
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
		retryDownloadFailedAttachment: async () => ({ fixed: false, stillMissing: true }),
		_searchAndFixUnavailableAttachment: async () => ({ found: false }),
		removeDownloadFailedItems: async () => { called = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(called, false);
});

test('searchAndFix retries skipReason timeout rows via retryTimeoutSkippedAttachment instead of marking them not-found immediately', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'TIMEOUT1' }, parentItem: { key: 'PARENT1' }, skipReason: 'timeout', isLinked: false },
	];
	const retryCalls = [];
	const removedCalls = [];
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async (att, parent, libId) => {
			retryCalls.push({ key: att.key, libId });
			return { fixed: true, stillTimedOut: false };
		},
		removeSkippedServerItems: async (libId, keys) => { removedCalls.push({ libId, keys }); },
	};

	await dialog.searchAndFix();

	assert.strictEqual(retryCalls.length, 1);
	assert.strictEqual(retryCalls[0].key, 'TIMEOUT1');
	assert.strictEqual(removedCalls.length, 1);
	// Spread into a host-realm array before comparing — removedCalls[0].keys was
	// built by .map() inside the vm-executed searchAndFix(), so it's a vm-realm
	// Array; deepStrictEqual treats same-content arrays from different vm
	// realms as unequal otherwise (see the established pattern a few tests up).
	assert.deepStrictEqual([...removedCalls[0].keys], ['TIMEOUT1']);
	// Fixed row is removed from the table immediately, same as other fix paths.
	assert.strictEqual(dialog.items.length, 0);
});

test('searchAndFix keeps a still-timed-out row visible with an updated status, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'STILLSLOW' }, parentItem: { key: 'PARENT1' }, skipReason: 'timeout', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async () => ({ fixed: false, stillTimedOut: true }),
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'not-found');
});

test('searchAndFix retries skipReason "no text" rows via retryEmptyTextSkippedAttachment instead of marking them not-found immediately', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'EMPTY1' }, parentItem: { key: 'PARENT1' }, skipReason: 'no text', isLinked: false },
	];
	const retryCalls = [];
	const removedCalls = [];
	dialog.plugin = {
		retryEmptyTextSkippedAttachment: async (att, parent, libId) => {
			retryCalls.push({ key: att.key, libId });
			return { fixed: true, stillEmpty: false };
		},
		removeSkippedServerItems: async (libId, keys) => { removedCalls.push({ libId, keys }); },
	};

	await dialog.searchAndFix();

	assert.strictEqual(retryCalls.length, 1);
	assert.strictEqual(retryCalls[0].key, 'EMPTY1');
	assert.strictEqual(removedCalls.length, 1);
	assert.deepStrictEqual([...removedCalls[0].keys], ['EMPTY1']);
	// Fixed row is removed from the table immediately, same as other fix paths.
	assert.strictEqual(dialog.items.length, 0);
});

test('searchAndFix keeps a still-empty row visible with an updated status, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'STILLEMPTY' }, parentItem: { key: 'PARENT1' }, skipReason: 'no text', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryEmptyTextSkippedAttachment: async () => ({ fixed: false, stillEmpty: true }),
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'not-found');
});

test('searchAndFix marks a row with error status when retryEmptyTextSkippedAttachment returns a non-empty failure, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'EMPTYPARSEFAIL' }, parentItem: { key: 'PARENT1' }, skipReason: 'no text', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryEmptyTextSkippedAttachment: async () => ({ fixed: false, stillEmpty: false, error: 'Binary data — unsupported format' }),
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'error');
});

test('searchAndFix marks a row with error status when retryEmptyTextSkippedAttachment throws, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'EMPTYNETERR' }, parentItem: { key: 'PARENT1' }, skipReason: 'no text', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryEmptyTextSkippedAttachment: async () => { throw new Error('network error'); },
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'error');
});

test('searchAndFix marks a row with error status when retryTimeoutSkippedAttachment returns a non-timeout failure, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'PARSEFAIL' }, parentItem: { key: 'PARENT1' }, skipReason: 'timeout', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async () => ({ fixed: false, stillTimedOut: false, error: 'Binary data — unsupported format' }),
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'error');
});

test('searchAndFix marks a row with error status when retryTimeoutSkippedAttachment throws, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'NETERR' }, parentItem: { key: 'PARENT1' }, skipReason: 'timeout', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async () => { throw new Error('network error'); },
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'error');
});

// ---------------------------------------------------------------------------
// "Download debugging information" option
// ---------------------------------------------------------------------------

const DEBUG_SOURCE_PATH = path.join(__dirname, '..', 'src', 'fix-unavailable-debug.js');

/**
 * Load the dialog + debug helper with a DOM stub exposing the footer checkbox.
 * @param {{checked?: boolean, saveImpl?: Function}} [opts]
 */
function loadDialogWithDebug({ checked = false, saveImpl } = {}) {
	const elements = {
		'debug-download-label': { style: { display: 'none' } },
		'debug-download-cb': { checked, disabled: false },
	};
	const context = {
		window: {},
		document: { getElementById: (id) => elements[id] || { addEventListener: () => {}, style: {}, disabled: false } },
		console,
		Services: { console: { logStringMessage: () => {}, logMessage: () => {} } },
		Cc: { '@mozilla.org/scripterror;1': { createInstance: () => ({ init: () => {} }) } },
		Ci: { nsIScriptError: {} },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(DEBUG_SOURCE_PATH, 'utf8'), context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'fix-unavailable.js' });
	const saves = [];
	context.ZoteroFixDebug.save = saveImpl || (async (_w, data, name) => { saves.push({ data, name }); return name; });
	const dialog = context.ZoteroFixUnavailableDialog;
	dialog._describeFile = async () => ({ exists_locally: false, size_bytes: null, basename: null, is_linked: false });
	dialog._debugEnvironment = () => ({ plugin: {}, backend: {}, library: {}, pathPrefixes: [] });
	return { dialog, elements, saves, context };
}

test('debug checkbox is visible only while 1-10 rows are selected', () => {
	const { dialog, elements } = loadDialogWithDebug();
	const label = elements['debug-download-label'];
	for (const [n, visible] of [[0, false], [1, true], [10, true], [11, false]]) {
		dialog.selected = new Set(Array.from({ length: n }, (_, i) => i));
		dialog._updateDebugCheckboxVisibility();
		assert.strictEqual(label.style.display === '', visible, `n=${n}`);
	}
});

test('debug checkbox is disabled (not hidden) while a run is in progress', () => {
	const { dialog, elements } = loadDialogWithDebug();
	dialog.selected = new Set([0]);
	dialog.isRunning = true;
	dialog._updateDebugCheckboxVisibility();
	assert.strictEqual(elements['debug-download-cb'].disabled, true);
	assert.strictEqual(elements['debug-download-label'].style.display, '');
});

test('_shouldCollectDebug ignores a checked box when the selection is out of range', () => {
	const { dialog } = loadDialogWithDebug({ checked: true });
	assert.strictEqual(dialog._shouldCollectDebug([0]), true);
	assert.strictEqual(dialog._shouldCollectDebug([]), false);
	assert.strictEqual(dialog._shouldCollectDebug(Array.from({ length: 11 }, (_, i) => i)), false);
});

/** Run searchAndFix on a mixed selection and return the call log + statuses. */
async function runMixed(checked) {
	const { dialog, saves } = loadDialogWithDebug({ checked });
	dialog.backendLibraryId = 'u1';
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0, 1, 2, 3]);
	dialog.items = [
		{ attachmentItem: { key: 'T' }, skipReason: 'timeout' },
		{ attachmentItem: { key: 'E' }, skipReason: 'no text' },
		{ attachmentItem: { key: 'D' }, isLinked: false },
		{ attachmentItem: { key: 'N' }, isLinked: false },
	];
	const calls = [];
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async (...a) => { calls.push(['timeout', a[0].key]); return { fixed: false, stillTimedOut: true, backendDiag: { request_id: 'r1' }, pluginDiag: { upload_attempts: 1 } }; },
		retryEmptyTextSkippedAttachment: async (...a) => { calls.push(['empty', a[0].key]); return { fixed: true, stillEmpty: false }; },
		_tryDownloadAttachment: async (att) => { calls.push(['dl', att.key]); return { downloaded: att.key === 'D', reason: 'x' }; },
		_searchAndFixUnavailableAttachment: async (att, trace) => { calls.push(['search', att.key]); if (trace) trace('md5', { attempted: false }); return { found: false }; },
		removeSkippedServerItems: async () => {},
		removeDownloadFailedItems: async () => {},
	};
	await dialog.searchAndFix();
	return { calls, statuses: [...dialog.rowStatus.entries()], remaining: dialog.items.map(i => i.attachmentItem.key), saves, dialog };
}

test('debug collection is behaviour-neutral: same calls, statuses and surviving rows as without it', async () => {
	const off = await runMixed(false);
	const on = await runMixed(true);
	const plain = (v) => JSON.parse(JSON.stringify(v));
	assert.deepStrictEqual(plain(on.calls), plain(off.calls));
	assert.deepStrictEqual(plain(on.statuses), plain(off.statuses));
	assert.deepStrictEqual(plain(on.remaining), plain(off.remaining));
	assert.strictEqual(off.saves.length, 0);
});

test('with the box checked, one report is saved containing every selected row and backend payloads', async () => {
	const { saves } = await runMixed(true);
	assert.strictEqual(saves.length, 1);
	assert.ok(/^zotero-rag-fix-debug-u1-\d{8}-\d{6}\.json$/.test(saves[0].name));
	const data = saves[0].data;
	assert.strictEqual(data.items.length, 4);
	const byKey = Object.fromEntries(data.items.map(i => [i.attachment_key, i]));
	assert.strictEqual(byKey.T.steps[0].phase, 'timeout_retry');
	assert.strictEqual(byKey.T.steps[0].backend.request_id, 'r1');
	assert.strictEqual(byKey.T.steps[0].outcome, 'still_timed_out');
	assert.strictEqual(byKey.E.steps[0].outcome, 'fixed');
	assert.strictEqual(byKey.E.steps[0].backend, null);
	assert.ok(byKey.E.steps[0].backend_note.includes('did not return diagnostics'));
	assert.strictEqual(byKey.D.steps[0].phase, 'sync_download');
	assert.strictEqual(byKey.D.final_row_status.css_class, 'fixed');
	assert.strictEqual(byKey.N.steps[1].phase, 'other_library_search');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(byKey.N.steps[1].plugin.trail)), [{ name: 'md5', data: { attempted: false } }]);
});

test('a cancelled save dialog does not throw or change row statuses', async () => {
	const off = await runMixed(false);
	const { dialog } = loadDialogWithDebug({ checked: true, saveImpl: async () => null });
	dialog.backendLibraryId = 'u1'; dialog.isRunning = false; dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [{ attachmentItem: { key: 'N' }, isLinked: false }];
	dialog.plugin = {
		_tryDownloadAttachment: async () => ({ downloaded: false }),
		_searchAndFixUnavailableAttachment: async () => ({ found: false }),
	};
	await dialog.searchAndFix();
	assert.strictEqual(dialog.rowStatus.get(0).cssClass, 'not-found');
	assert.strictEqual(dialog.isRunning, false);
	assert.ok(off.statuses.length > 0);
});

// ---------------------------------------------------------------------------
// Split button: defer threading and queued-row handling
// ---------------------------------------------------------------------------

test('searchAndFix threads defer:true into an upload call site when deferCapable and not forcing', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.deferCapable = true;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'T1' }, parentItem: { key: 'P1' }, skipReason: 'timeout', isLinked: false },
	];
	let capturedOpts = null;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async (att, parent, libId, opts) => {
			capturedOpts = opts;
			return { fixed: true, stillTimedOut: false };
		},
		removeSkippedServerItems: async () => {},
	};

	await dialog.searchAndFix({ forceIndexNow: false });

	assert.strictEqual(capturedOpts.defer, true);
});

test('searchAndFix({forceIndexNow: true}) threads defer:false into an upload call site', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.deferCapable = true;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'T1' }, parentItem: { key: 'P1' }, skipReason: 'timeout', isLinked: false },
	];
	let capturedOpts = null;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async (att, parent, libId, opts) => {
			capturedOpts = opts;
			return { fixed: true, stillTimedOut: false };
		},
		removeSkippedServerItems: async () => {},
	};

	await dialog.searchAndFix({ forceIndexNow: true });

	assert.strictEqual(capturedOpts.defer, false);
});

test('a row already in status-queued is left untouched by the default (non-forcing) action', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.deferCapable = true;
	dialog.rowStatus = new Map([[0, { cssClass: 'queued', text: 'Waiting to be indexed' }]]);
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'Q1' }, parentItem: { key: 'P1' }, isLinked: false },
	];
	let processNowCalled = false;
	dialog.plugin = {
		processQueuedAttachmentNow: async () => { processNowCalled = true; return { fixed: true }; },
	};

	await dialog.searchAndFix({ forceIndexNow: false });

	assert.strictEqual(processNowCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'queued');
});

test('a row already in status-queued is processed via processQueuedAttachmentNow when forceIndexNow is true, and becomes fixed on success', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.deferCapable = true;
	dialog.rowStatus = new Map([[0, { cssClass: 'queued', text: 'Waiting to be indexed' }]]);
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'Q1' }, parentItem: { key: 'P1' }, isLinked: false },
	];
	const calls = [];
	dialog.plugin = {
		processQueuedAttachmentNow: async (att, parent, libId, opts) => {
			calls.push({ key: att.key, libId, opts });
			return { fixed: true };
		},
	};

	await dialog.searchAndFix({ forceIndexNow: true });

	assert.strictEqual(calls.length, 1);
	assert.strictEqual(calls[0].key, 'Q1');
	assert.strictEqual(calls[0].libId, 1);
	// Fixed row is dropped from the table immediately, same as every other fix path.
	assert.strictEqual(dialog.items.length, 0);
});

test('a result.queued response sets the row to status-queued and the row survives the "drop fixed rows" step', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.deferCapable = true;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'TQ1' }, parentItem: { key: 'P1' }, skipReason: 'timeout', isLinked: false },
	];
	let removeCalled = null;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async () => ({ fixed: false, queued: true, eta: '2026-10-06T12:00:00Z', queueBlockReason: null }),
		removeSkippedServerItems: async (libId, keys) => { removeCalled = { libId, keys }; },
	};

	await dialog.searchAndFix({ forceIndexNow: false });

	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'queued');
	// The row survives the "drop fixed rows" step — it's queued, not fixed.
	assert.strictEqual(dialog.items.length, 1);
	// Still pruned from the skipped-server store: the row is no longer stuck
	// skipping, it's now tracked by the backend's deferred-upload cache instead.
	assert.ok(removeCalled);
	assert.deepStrictEqual([...removeCalled.keys], ['TQ1']);
});

test('a failing save is reported in the status bar and still restores the dialog state', async () => {
	const { dialog } = loadDialogWithDebug({ checked: true, saveImpl: async () => { throw new Error('disk full'); } });
	dialog.backendLibraryId = 'u1'; dialog.isRunning = false; dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [{ attachmentItem: { key: 'N' }, isLinked: false }];
	let status = '';
	dialog.setStatus = (t) => { status = t; };
	dialog.plugin = {
		_tryDownloadAttachment: async () => ({ downloaded: false }),
		_searchAndFixUnavailableAttachment: async () => ({ found: false }),
	};
	await dialog.searchAndFix();
	assert.ok(status.includes('Failed to save debug info: disk full'), status);
	assert.strictEqual(dialog.isRunning, false);
});
