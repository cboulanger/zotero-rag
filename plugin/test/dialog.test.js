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

test('runQuery returns the result directly when no client evidence is needed', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const fakeThis = {
		plugin: { submitQuery: async () => ({ status: 'complete', answer: 'A', sources: [] }) },
	};
	const result = await ZoteroRAGDialog.runQuery.call(fakeThis, 'Q', ['1'], { minScore: 0.3 });
	assert.strictEqual(result.answer, 'A');
});

test('runQuery gathers mention evidence and resubmits once when the backend requests it', async () => {
	/** @type {any[]} */
	const submittedOptions = [];
	const fakeThis = {
		plugin: {
			submitQuery: async (/** @type {string} */ _q, /** @type {string[]} */ _ids, /** @type {any} */ opts) => {
				submittedOptions.push(opts);
				if (submittedOptions.length === 1) {
					return { status: 'needs_client_evidence', citation_targets: [{ author: 'X', year: null, title_keywords: [] }], query_plan: { agents_to_use: ['mentions'] } };
				}
				return { status: 'complete', answer: 'Resolved.', sources: [] };
			},
		},
		resolveZoteroLibraryID: (/** @type {string} */ id) => (id === 'u1' ? 1 : null),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		MentionSearch: { findMentionEvidence: async () => ({ items: [], truncated: false, total_candidates: 0 }) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const result = await ContextDialog.runQuery.call(fakeThis, 'Q', ['u1'], {});

	assert.strictEqual(result.answer, 'Resolved.');
	assert.strictEqual(submittedOptions.length, 2);
	assert.ok(submittedOptions[1].clientEvidence);
	assert.deepStrictEqual(submittedOptions[1].queryPlan, { agents_to_use: ['mentions'] });
});

test('runQuery calls the optional progress callback around the mentions round trip', async () => {
	/** @type {any[]} */
	const progressCalls = [];
	let calls = 0;
	const fakeThis = {
		plugin: {
			submitQuery: async () => {
				calls++;
				return calls === 1
					? { status: 'needs_client_evidence', citation_targets: [], query_plan: null }
					: { status: 'complete', answer: 'A', sources: [] };
			},
		},
		resolveZoteroLibraryID: () => null,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		MentionSearch: { findMentionEvidence: async () => ({ items: [], truncated: false, total_candidates: 0 }) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await ContextDialog.runQuery.call(fakeThis, 'Q', [], {}, (/** @type {number} */ pct, /** @type {string} */ label) => progressCalls.push({ pct, label }));

	assert.strictEqual(progressCalls.length, 2);
	assert.strictEqual(progressCalls[0].label, 'Searching local library');
	assert.strictEqual(progressCalls[1].label, 'Resubmitting query');
});

test('runQuery throws if the backend requests client evidence a second time', async () => {
	const fakeThis = {
		plugin: { submitQuery: async () => ({ status: 'needs_client_evidence', citation_targets: [], query_plan: null }) },
		resolveZoteroLibraryID: () => null,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		MentionSearch: { findMentionEvidence: async () => ({ items: [], truncated: false, total_candidates: 0 }) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await assert.rejects(
		() => ContextDialog.runQuery.call(fakeThis, 'Q', [], {}),
		/requested citation evidence a second time/
	);
});

test('renderResultContent joins every turn\'s formatted HTML with a divider', () => {
	/** @type {any[][]} */
	const libraryMapCalls = [];
	const fakeElement = { innerHTML: '' };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => (id === 'result-content' ? fakeElement : null) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		plugin: {
			buildLibraryMap: (/** @type {string[]} */ ids) => { libraryMapCalls.push(ids); return new Map(); },
			formatTurnHTML: (/** @type {string} */ q, /** @type {any} */ r) => `<p>${q}:${r.answer}</p>`,
		},
		libraryIds: ['u1'],
		turns: [
			{ question: 'Q1', result: { answer: 'A1' } },
			{ question: 'Q2', result: { answer: 'A2' } },
		],
	};

	ContextDialog.renderResultContent.call(fakeThis);

	assert.strictEqual(fakeElement.innerHTML, '<p>Q1:A1</p><hr/><p>Q2:A2</p>');
	assert.deepStrictEqual(libraryMapCalls, [['u1']]);
});

test('updateExportButtonVisibility shows the button only when the first turn has a trace', () => {
	const fakeButton = { style: {} };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => (id === 'export-debug-button' ? fakeButton : null) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.updateExportButtonVisibility.call({ turns: [{ question: 'Q', result: { trace: { a: 1 } } }] });
	assert.strictEqual(fakeButton.style.display, '');

	ContextDialog.updateExportButtonVisibility.call({ turns: [{ question: 'Q', result: {} }] });
	assert.strictEqual(fakeButton.style.display, 'none');
});

test('switchToResultState hides the input state and reveals the result state', () => {
	const inputContent = { style: {} };
	const inputButtons = { style: {} };
	const resultSection = { classList: { added: /** @type {string[]} */ ([]), add(/** @type {string} */ c) { this.added.push(c); } } };
	const resultContent = { addEventListener: () => {} };
	const elementsById = {
		'input-content': inputContent,
		'input-buttons': inputButtons,
		'result-section': resultSection,
		'result-content': resultContent,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.switchToResultState.call({});

	assert.strictEqual(inputContent.style.display, 'none');
	assert.strictEqual(inputButtons.style.display, 'none');
	assert.deepStrictEqual(resultSection.classList.added, ['visible']);
});

test('switchToResultState is idempotent — a second call does not re-attach the listener', () => {
	let listenerCount = 0;
	const inputContent = { style: {} };
	const inputButtons = { style: {} };
	const resultSection = { classList: { add() {} } };
	const resultContent = { addEventListener: () => { listenerCount++; } };
	const elementsById = {
		'input-content': inputContent,
		'input-buttons': inputButtons,
		'result-section': resultSection,
		'result-content': resultContent,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = { _resultStateActive: false };
	ContextDialog.switchToResultState.call(fakeThis);
	ContextDialog.switchToResultState.call(fakeThis);

	assert.strictEqual(listenerCount, 1);
});
