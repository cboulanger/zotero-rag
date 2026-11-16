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

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', { last_scan_failed_downloads: [] });
	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', null);

	assert.deepStrictEqual(storeCalls, []);
	assert.deepStrictEqual(countUpdates, []);
});

test('mergeDownloadFailures stores keys and bumps the count when new ones were added', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const { fakeThis, storeCalls, countUpdates } = makeFakeThis(2, 5);

	await ZoteroRAGDialog.mergeDownloadFailures.call(fakeThis, 'lib1', {
		last_scan_failed_downloads: [
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
		last_scan_failed_downloads: [{ item_key: 'A', attachment_key: 'ATT1' }],
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
	const fakeElement = { innerHTML: '', querySelectorAll: () => [] };
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

test('renderResultContent rewrites zotero:// hrefs to survive innerHTML sanitization, then restores them', () => {
	/** @type {string} */
	let capturedInnerHTML = '';
	/** @type {any[]} */
	const restoredAnchors = [];
	const fakeAnchor = {
		attrs: /** @type {Record<string, string>} */ ({}),
		getAttribute(/** @type {string} */ name) { return this.attrs[name]; },
		setAttribute(/** @type {string} */ name, /** @type {string} */ value) { this.attrs[name] = value; restoredAnchors.push({ name, value }); },
		removeAttribute(/** @type {string} */ name) { delete this.attrs[name]; },
	};
	fakeAnchor.attrs['data-zotero-href'] = 'zotero://select/groups/1/items/ABC';
	const fakeElement = {
		set innerHTML(/** @type {string} */ html) { capturedInnerHTML = html; },
		get innerHTML() { return capturedInnerHTML; },
		querySelectorAll: (/** @type {string} */ selector) => (selector === 'a[data-zotero-href]' ? [fakeAnchor] : []),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => (id === 'result-content' ? fakeElement : null) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		plugin: {
			buildLibraryMap: () => new Map(),
			formatTurnHTML: () => '<a href="zotero://select/groups/1/items/ABC">(cite)</a>',
		},
		libraryIds: ['u1'],
		turns: [{ question: 'Q', result: { answer: 'A' } }],
	};

	ContextDialog.renderResultContent.call(fakeThis);

	assert.ok(capturedInnerHTML.includes('data-zotero-href="zotero://select/groups/1/items/ABC"'), 'href should be rewritten to data-zotero-href before assignment');
	// Note: a plain `.includes('href="zotero://')` would be a false negative here, since
	// 'data-zotero-href="zotero://' itself contains that substring — match only a real
	// (non-data-*) href attribute instead.
	assert.ok(!/(?<!data-zotero-)href="zotero:\/\//.test(capturedInnerHTML), 'raw href should not survive into the assigned HTML string (would be stripped by the sanitizer)');
	assert.deepStrictEqual(restoredAnchors, [{ name: 'href', value: 'zotero://select/groups/1/items/ABC' }], 'href should be restored via setAttribute after assignment');
	assert.strictEqual(fakeAnchor.attrs['data-zotero-href'], undefined, 'the temporary data attribute should be removed after restoring href');
});

test('renderResultContent scrolls the container to the bottom after rendering, so a follow-up answer is visible', () => {
	const fakeElement = { innerHTML: '', querySelectorAll: () => [], scrollTop: 0, scrollHeight: 500 };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => (id === 'result-content' ? fakeElement : null) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		plugin: { buildLibraryMap: () => new Map(), formatTurnHTML: () => '<p>A</p>' },
		libraryIds: ['u1'],
		turns: [{ question: 'Q', result: { answer: 'A' } }],
	};

	ContextDialog.renderResultContent.call(fakeThis);

	assert.strictEqual(fakeElement.scrollTop, fakeElement.scrollHeight);
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

test('switchToResultState wires citation clicks to openZoteroLink, not Zotero.launchURL', () => {
	// Regression test: clicking a citation used to call Zotero.launchURL(),
	// which routes any non-http(s) scheme back out through the OS's
	// external-protocol-handler service — circular when Zotero is already
	// the running process, and it surfaced as a native "Allow this site to
	// open the zotero link with Zotero?" dialog whose "Open Link" did
	// nothing. The click handler must instead delegate to this.openZoteroLink().
	const inputContent = { style: {} };
	const inputButtons = { style: {} };
	const resultSection = { classList: { add() {} } };
	/** @type {((event: any) => void)|null} */
	let clickListener = null;
	const resultContent = { addEventListener: (/** @type {string} */ _type, /** @type {any} */ fn) => { clickListener = fn; } };
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

	/** @type {string[]} */
	const openedLinks = [];
	const fakeThis = { openZoteroLink: (/** @type {string} */ href) => openedLinks.push(href) };
	ContextDialog.switchToResultState.call(fakeThis);

	/** @type {any} */
	const fakeAnchorEl = { href: 'zotero://select/library/items/ABC' };
	fakeAnchorEl.closest = () => fakeAnchorEl;
	let prevented = false;
	/** @type {any} */
	const fakeEvent = { target: fakeAnchorEl, preventDefault: () => { prevented = true; } };

	assert.ok(clickListener, 'a click listener should have been attached to #result-content');
	/** @type {any} */ (clickListener)(fakeEvent);

	assert.strictEqual(prevented, true);
	assert.deepStrictEqual(openedLinks, ['zotero://select/library/items/ABC']);
});

test('openZoteroLink dispatches in-process via the registered "zotero" protocol handler, without calling Zotero.launchURL', () => {
	/** @type {any[]} */
	const doActionCalls = [];
	/** @type {any[]} */
	const launchURLCalls = [];
	const fakeExtension = { noContent: true, doAction: (/** @type {any} */ uri) => doActionCalls.push(uri) };
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Services: {
			io: {
				newURI: (/** @type {string} */ href) => ({ spec: href }),
				getProtocolHandler: (/** @type {string} */ scheme) => {
					assert.strictEqual(scheme, 'zotero');
					return { wrappedJSObject: { getExtension: () => fakeExtension } };
				},
			},
		},
		Zotero: { launchURL: (/** @type {string} */ href) => launchURLCalls.push(href), logError: () => {} },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.openZoteroLink('zotero://open-pdf/library/items/ABC?page=5');

	assert.strictEqual(doActionCalls.length, 1);
	assert.strictEqual(doActionCalls[0].spec, 'zotero://open-pdf/library/items/ABC?page=5');
	assert.strictEqual(launchURLCalls.length, 0, 'Zotero.launchURL should never be called for a noContent extension — that is the OS round-trip this fix avoids');
});

test('openZoteroLink falls back to Zotero.launchURL when the resolved extension is not a noContent one', () => {
	/** @type {any[]} */
	const launchURLCalls = [];
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Services: {
			io: {
				newURI: (/** @type {string} */ href) => ({ spec: href }),
				getProtocolHandler: () => ({ wrappedJSObject: { getExtension: () => ({ noContent: false }) } }),
			},
		},
		Zotero: { launchURL: (/** @type {string} */ href) => launchURLCalls.push(href), logError: () => {} },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.openZoteroLink('zotero://something-else');

	assert.deepStrictEqual(launchURLCalls, ['zotero://something-else']);
});

test('openZoteroLink swallows and logs an error instead of throwing, if URI resolution fails', () => {
	/** @type {any[]} */
	const loggedErrors = [];
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Services: { io: { newURI: () => { throw new Error('bad URI'); } } },
		Zotero: { launchURL: () => { throw new Error('should not be called'); }, logError: (/** @type {any} */ e) => loggedErrors.push(e) },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	assert.doesNotThrow(() => ContextDialog.openZoteroLink('zotero://malformed'));
	assert.strictEqual(loggedErrors.length, 1);
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

/**
 * Build a document stub wired up with fake elements for both the input-state
 * status section (#status-section/#status-messages, nested inside
 * #input-content) and the result-state one (#result-status-section/
 * #result-status-messages, inside #result-section) so showStatus()'s routing
 * between the two can be exercised directly.
 * @returns {{context: any, progressSection: any, statusSection: any, statusMessages: any, resultStatusSection: any, resultStatusMessages: any}}
 */
function makeShowStatusContext() {
	const progressSection = { style: {} };
	const statusSection = { style: {} };
	// innerHTML setter mirrors real DOM semantics (setting it to '' clears children), since
	// clearStatusMessages() clears via `container.innerHTML = ''`.
	const statusMessages = {
		children: /** @type {any[]} */ ([]), appendChild(el) { this.children.push(el); }, scrollTop: 0, scrollHeight: 1,
		get innerHTML() { return this.children.length ? '<div/>' : ''; },
		set innerHTML(/** @type {string} */ _v) { this.children = []; },
	};
	const resultStatusSection = { style: {} };
	const resultStatusMessages = {
		children: /** @type {any[]} */ ([]), appendChild(el) { this.children.push(el); }, scrollTop: 0, scrollHeight: 1,
		get innerHTML() { return this.children.length ? '<div/>' : ''; },
		set innerHTML(/** @type {string} */ _v) { this.children = []; },
	};
	const elementsById = {
		'progress-section': progressSection,
		'status-section': statusSection,
		'status-messages': statusMessages,
		'result-status-section': resultStatusSection,
		'result-status-messages': resultStatusMessages,
	};
	const context = {
		document: {
			readyState: 'loading', addEventListener() {},
			getElementById: (/** @type {string} */ id) => elementsById[id] || null,
			createElement: () => ({ className: '', textContent: '' }),
		},
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	return { context, progressSection, statusSection, statusMessages, resultStatusSection, resultStatusMessages };
}

test('showStatus writes an input-state error into #status-section/#status-messages when not in result state', () => {
	const { context, progressSection, statusSection, statusMessages, resultStatusSection, resultStatusMessages } = makeShowStatusContext();
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.showStatus.call({ _resultStateActive: false, _getStatusElementIds: ContextDialog._getStatusElementIds }, 'Something broke', 'error');

	assert.strictEqual(progressSection.style.display, 'none');
	assert.strictEqual(statusSection.style.display, '');
	assert.strictEqual(statusMessages.children.length, 1);
	assert.strictEqual(statusMessages.children[0].textContent, 'Something broke');
	assert.strictEqual(resultStatusSection.style.display, undefined);
	assert.strictEqual(resultStatusMessages.children.length, 0);
});

test('showStatus writes a result-state error into #result-status-section/#result-status-messages once switchToResultState has run', () => {
	const { context, progressSection, statusSection, resultStatusSection, resultStatusMessages, statusMessages } = makeShowStatusContext();
	const ContextDialog = context.ZoteroRAGDialog;

	ContextDialog.showStatus.call({ _resultStateActive: true, _getStatusElementIds: ContextDialog._getStatusElementIds }, 'Follow-up failed', 'error');

	assert.strictEqual(resultStatusSection.style.display, '');
	assert.strictEqual(resultStatusMessages.children.length, 1);
	assert.strictEqual(resultStatusMessages.children[0].textContent, 'Follow-up failed');
	// The input-state elements (already hidden behind #input-content) are left untouched.
	assert.strictEqual(progressSection.style.display, undefined);
	assert.strictEqual(statusSection.style.display, undefined);
	assert.strictEqual(statusMessages.children.length, 0);
});

test('clearStatusMessages clears and hides the input-state elements when not in result state', () => {
	const { context, progressSection, statusSection, statusMessages, resultStatusSection, resultStatusMessages } = makeShowStatusContext();
	const ContextDialog = context.ZoteroRAGDialog;

	// Seed stale content in both pairs to prove only the active (input-state) one is touched.
	statusMessages.children.push({ textContent: 'stale input error' });
	statusSection.style.display = '';
	resultStatusMessages.children.push({ textContent: 'stale result error' });
	resultStatusSection.style.display = '';
	progressSection.style.display = 'none';

	ContextDialog.clearStatusMessages.call({ _resultStateActive: false, _getStatusElementIds: ContextDialog._getStatusElementIds });

	assert.strictEqual(statusMessages.children.length, 0);
	assert.strictEqual(statusSection.style.display, 'none');
	assert.strictEqual(progressSection.style.display, '');
	// Result-state elements are untouched.
	assert.strictEqual(resultStatusMessages.children.length, 1);
	assert.strictEqual(resultStatusSection.style.display, '');
});

test('clearStatusMessages clears and hides the result-state elements when in result state', () => {
	const { context, progressSection, statusSection, statusMessages, resultStatusSection, resultStatusMessages } = makeShowStatusContext();
	const ContextDialog = context.ZoteroRAGDialog;

	// Seed stale content in both pairs to prove only the active (result-state) one is touched.
	statusMessages.children.push({ textContent: 'stale input error' });
	statusSection.style.display = '';
	resultStatusMessages.children.push({ textContent: 'stale result error' });
	resultStatusSection.style.display = '';
	progressSection.style.display = 'none';

	ContextDialog.clearStatusMessages.call({ _resultStateActive: true, _getStatusElementIds: ContextDialog._getStatusElementIds });

	assert.strictEqual(resultStatusMessages.children.length, 0);
	assert.strictEqual(resultStatusSection.style.display, 'none');
	// The result state has no progress widget to reset, so it's left untouched.
	assert.strictEqual(progressSection.style.display, 'none');
	// Input-state elements are untouched.
	assert.strictEqual(statusMessages.children.length, 1);
	assert.strictEqual(statusSection.style.display, '');
});

test('submitFollowUp clears a stale result-status error before appending a turn and re-renders, without touching a note when none has been saved', async () => {
	const fakeInput = { value: 'Follow-up question', disabled: false };
	const fakeButton = { disabled: false };
	const resultStatusMessages = {
		children: /** @type {any[]} */ ([{ textContent: 'stale error from a previous failed attempt' }]),
		appendChild(/** @type {any} */ el) { this.children.push(el); },
		get innerHTML() { return this.children.length ? '<div>stale</div>' : ''; },
		set innerHTML(/** @type {string} */ _v) { this.children = []; },
	};
	const resultStatusSection = { style: { display: '' } };
	const elementsById = {
		'followup-input': fakeInput,
		'result-submit-button': fakeButton,
		'result-status-messages': resultStatusMessages,
		'result-status-section': resultStatusSection,
	};
	/** @type {number[]} */
	const renderCalls = [];
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any[]} */
	const submittedOptions = [];
	const fakeThis = {
		hideStandinOffer() {}, quotaDetailOf: ContextDialog.quotaDetailOf, errorText: ContextDialog.errorText,
		plugin: {
			submitQuery: async (/** @type {string} */ _q, /** @type {string[]} */ _libIds, /** @type {any} */ opts) => {
				submittedOptions.push(opts);
				return { status: 'complete', answer: 'Follow-up answer.', sources: [] };
			},
		},
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: null,
		queryOptions: { minScore: 0.3, topK: 10, llmModel: 'test-model', enableRouting: true },
		_resultStateActive: true,
		buildConversationHistory: ContextDialog.buildConversationHistory,
		runQuery: ContextDialog.runQuery,
		renderResultContent() { renderCalls.push(this.turns.length); },
		showStatus() {},
		clearStatusMessages: ContextDialog.clearStatusMessages,
		_getStatusElementIds: ContextDialog._getStatusElementIds,
	};

	await ContextDialog.submitFollowUp.call(fakeThis);

	// The stale error from a previous failed attempt was cleared before the new answer rendered.
	assert.strictEqual(resultStatusMessages.children.length, 0);
	assert.strictEqual(resultStatusSection.style.display, 'none');
	assert.strictEqual(fakeThis.turns.length, 2);
	assert.strictEqual(fakeThis.turns[1].question, 'Follow-up question');
	assert.strictEqual(fakeInput.value, '');
	assert.deepStrictEqual(renderCalls, [2]);

	// The original turn's minScore/topK/llmModel/enableRouting were forwarded into the
	// follow-up's runQuery/submitQuery call — this is the regression this test locks in.
	assert.strictEqual(submittedOptions.length, 1);
	assert.strictEqual(submittedOptions[0].minScore, 0.3);
	assert.strictEqual(submittedOptions[0].topK, 10);
	assert.strictEqual(submittedOptions[0].llmModel, 'test-model');
	assert.strictEqual(submittedOptions[0].enableRouting, true);
});

test('submitFollowUp shows the "Processing query" indicator while awaiting the response, and hides it afterward', async () => {
	const fakeInput = { value: 'Follow-up question', disabled: false };
	const fakeButton = { disabled: false };
	const loadingIndicator = { style: { display: 'none' } };
	/** @type {string[]} */
	const displayDuringRunQuery = [];
	const elementsById = {
		'followup-input': fakeInput,
		'result-submit-button': fakeButton,
		'followup-loading': loadingIndicator,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		hideStandinOffer() {}, quotaDetailOf: ContextDialog.quotaDetailOf, errorText: ContextDialog.errorText,
		plugin: {
			submitQuery: async () => {
				displayDuringRunQuery.push(loadingIndicator.style.display);
				return { status: 'complete', answer: 'Follow-up answer.', sources: [] };
			},
		},
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: null,
		buildConversationHistory: ContextDialog.buildConversationHistory,
		runQuery: ContextDialog.runQuery,
		renderResultContent() {},
		showStatus() {},
		clearStatusMessages() {},
	};

	await ContextDialog.submitFollowUp.call(fakeThis);

	assert.deepStrictEqual(displayDuringRunQuery, ['flex'], 'indicator should be visible while the query is in flight');
	assert.strictEqual(loadingIndicator.style.display, 'none', 'indicator should be hidden again once the response arrives');
});

test('submitFollowUp hides the "Processing query" indicator even when the query fails', async () => {
	const fakeInput = { value: 'Follow-up question', disabled: false };
	const fakeButton = { disabled: false };
	const loadingIndicator = { style: { display: 'none' } };
	const elementsById = {
		'followup-input': fakeInput,
		'result-submit-button': fakeButton,
		'followup-loading': loadingIndicator,
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		hideStandinOffer() {}, quotaDetailOf: ContextDialog.quotaDetailOf, errorText: ContextDialog.errorText,
		plugin: { submitQuery: async () => { throw new Error('boom'); } },
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: null,
		buildConversationHistory: ContextDialog.buildConversationHistory,
		runQuery: ContextDialog.runQuery,
		renderResultContent() {},
		showStatus() {},
		clearStatusMessages() {},
	};

	await ContextDialog.submitFollowUp.call(fakeThis);

	assert.strictEqual(loadingIndicator.style.display, 'none');
});

test('submitFollowUp regenerates the note from the full turn history when one has already been saved', async () => {
	const fakeInput = { value: 'Follow-up question', disabled: false };
	const elementsById = { 'followup-input': fakeInput, 'result-submit-button': { disabled: false } };
	/** @type {string[]} */
	const notedHtml = [];
	const noteStub = { setNote: (/** @type {string} */ html) => notedHtml.push(html), saveTx: async () => {} };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
		Zotero: { Items: { get: (/** @type {number} */ id) => (id === 42 ? noteStub : null) } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any} */
	let receivedTurns = null;
	const fakeThis = {
		hideStandinOffer() {}, quotaDetailOf: ContextDialog.quotaDetailOf, errorText: ContextDialog.errorText,
		plugin: {
			submitQuery: async () => ({ status: 'complete', answer: 'Follow-up answer.', sources: [] }),
			formatNoteHTML: (/** @type {any} */ turns, /** @type {string[]} */ _libIds) => { receivedTurns = turns; return '<div>full regenerated html</div>'; },
		},
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: 42,
		buildConversationHistory: ContextDialog.buildConversationHistory,
		runQuery: ContextDialog.runQuery,
		renderResultContent() {},
		showStatus() {},
		clearStatusMessages() {},
	};

	await ContextDialog.submitFollowUp.call(fakeThis);

	assert.strictEqual(notedHtml.length, 1);
	assert.strictEqual(notedHtml[0], '<div>full regenerated html</div>');
	assert.strictEqual(receivedTurns, fakeThis.turns);
	assert.strictEqual(receivedTurns.length, 2);
	assert.strictEqual(receivedTurns[1].question, 'Follow-up question');
});

test('submitFollowUp does nothing when the input is empty', async () => {
	const fakeInput = { value: '   ', disabled: false };
	const elementsById = { 'followup-input': fakeInput };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = {
		hideStandinOffer() {}, quotaDetailOf: ContextDialog.quotaDetailOf, errorText: ContextDialog.errorText, plugin: { submitQuery: async () => { throw new Error('should not be called'); } }, turns: [] };
	await ContextDialog.submitFollowUp.call(fakeThis);
	assert.strictEqual(fakeThis.turns.length, 0);
});

test('saveAsNote creates the note from every turn and disables the button', async () => {
	const fakeButton = { disabled: false, textContent: 'Save as Note' };
	const elementsById = { 'save-note-button': fakeButton };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any} */
	let receivedTurns = null;
	const fakeThis = {
		plugin: { createResultNote: async (/** @type {any} */ turns, /** @type {string[]} */ _libIds) => { receivedTurns = turns; return { id: 99 }; } },
		libraryIds: ['u1'],
		turns: [{ question: 'Q0', result: { answer: 'A0' } }],
		noteID: null,
		clearStatusMessages() {},
	};

	await ContextDialog.saveAsNote.call(fakeThis);

	assert.strictEqual(fakeThis.noteID, 99);
	assert.strictEqual(receivedTurns, fakeThis.turns);
	assert.strictEqual(fakeButton.disabled, true);
	assert.strictEqual(fakeButton.textContent, 'Saved');
});

test('saveAsNote does nothing if a note has already been saved', async () => {
	const ZoteroRAGDialog = loadDialogMethods();
	const fakeThis = {
		plugin: { createResultNote: async () => { throw new Error('should not be called'); } },
		turns: [], noteID: 42,
	};
	await ZoteroRAGDialog.saveAsNote.call(fakeThis);
	assert.strictEqual(fakeThis.noteID, 42);
});

test('saveAsNote re-enables the button and shows an error if note creation fails', async () => {
	const fakeButton = { disabled: false, textContent: 'Save as Note' };
	const elementsById = { 'save-note-button': fakeButton };
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => elementsById[id] || null },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any[]} */
	const statusCalls = [];
	const fakeThis = {
		plugin: { createResultNote: async () => { throw new Error('disk full'); } },
		libraryIds: ['u1'], turns: [{ question: 'Q0', result: { answer: 'A0' } }], noteID: null,
		showStatus(/** @type {string} */ msg, /** @type {string} */ type) { statusCalls.push({ msg, type }); },
		clearStatusMessages() {},
	};

	await ContextDialog.saveAsNote.call(fakeThis);

	assert.strictEqual(fakeThis.noteID, null);
	assert.strictEqual(fakeButton.disabled, false);
	assert.strictEqual(statusCalls.length, 1);
	assert.strictEqual(statusCalls[0].type, 'error');
});

test('exportDebugInfo writes the first turn\'s trace as formatted JSON to the picked file path', async () => {
	/** @type {string[]} */
	const written = [];
	const fakePicker = {
		appendFilter() {}, init() {}, defaultString: '',
		file: { path: '/fake/path/trace.json' },
		open: (/** @type {(rv: number) => void} */ callback) => callback(1 /* returnOK */),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: { browsingContext: {} }, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => fakePicker } },
		Ci: { nsIFilePicker: { modeSave: 0, returnOK: 1, returnReplace: 2 } },
		IOUtils: { writeUTF8: async (/** @type {string} */ p, /** @type {string} */ text) => { written.push(`${p}::${text}`); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	const fakeThis = { turns: [{ question: 'Q', result: { trace: { step: 1 } } }], clearStatusMessages() {} };
	await ContextDialog.exportDebugInfo.call(fakeThis);

	assert.strictEqual(written.length, 1);
	assert.ok(written[0].startsWith('/fake/path/trace.json::'));
	assert.ok(written[0].includes('"step": 1'));
});

test('exportDebugInfo does nothing when the first turn has no trace', async () => {
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: { browsingContext: {} }, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => { throw new Error('should not be called'); } } },
		Ci: { nsIFilePicker: {} },
		IOUtils: { writeUTF8: async () => { throw new Error('should not be called'); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await ContextDialog.exportDebugInfo.call({ turns: [{ question: 'Q', result: {} }] });
	// No assertion beyond "did not throw" — the stubs above throw if called.
});

test('exportDebugInfo does not write a file when the user cancels the save dialog', async () => {
	/** @type {string[]} */
	const written = [];
	const fakePicker = {
		appendFilter() {}, init() {}, defaultString: '',
		file: { path: '/fake/path/trace.json' },
		open: (/** @type {(rv: number) => void} */ callback) => callback(-1 /* returnCancel */),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: { browsingContext: {} }, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => fakePicker } },
		Ci: { nsIFilePicker: { modeSave: 0, returnOK: 1, returnReplace: 2 } },
		IOUtils: { writeUTF8: async (/** @type {string} */ p) => { written.push(p); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	await ContextDialog.exportDebugInfo.call({ turns: [{ question: 'Q', result: { trace: { a: 1 } } }], clearStatusMessages() {} });
	assert.strictEqual(written.length, 0);
});

test('exportDebugInfo shows an error status if writing the file fails', async () => {
	const fakePicker = {
		appendFilter() {}, init() {}, defaultString: '',
		file: { path: '/fake/path/trace.json' },
		open: (/** @type {(rv: number) => void} */ callback) => callback(1 /* returnOK */),
	};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: { browsingContext: {} }, console,
		Cc: { '@mozilla.org/filepicker;1': { createInstance: () => fakePicker } },
		Ci: { nsIFilePicker: { modeSave: 0, returnOK: 1, returnReplace: 2 } },
		IOUtils: { writeUTF8: async () => { throw new Error('disk full'); } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ContextDialog = context.ZoteroRAGDialog;

	/** @type {any[]} */
	const statusCalls = [];
	const fakeThis = {
		turns: [{ question: 'Q', result: { trace: { a: 1 } } }],
		showStatus(/** @type {string} */ msg, /** @type {string} */ type) { statusCalls.push({ msg, type }); },
		clearStatusMessages() {},
	};

	await ContextDialog.exportDebugInfo.call(fakeThis);

	assert.strictEqual(statusCalls.length, 1);
	assert.strictEqual(statusCalls[0].type, 'error');
	assert.ok(statusCalls[0].msg.includes('disk full'));
});

// --- describeEndpointBlock: what stops Submit/Index while an endpoint is not ready ---

test('describeEndpointBlock: nothing blocks when both endpoints are ready or have no health', () => {
	const D = loadDialogMethods();
	assert.strictEqual(D.describeEndpointBlock(null, false), null);
	assert.strictEqual(D.describeEndpointBlock({ embedding: null, llm: null }, false), null);
	assert.strictEqual(D.describeEndpointBlock({ embedding: { status: 'ready' }, llm: { status: 'throttled' } }, false), null);
});

test('describeEndpointBlock: a cold endpoint blocks and is re-checked soon', () => {
	const D = loadDialogMethods();
	const block = D.describeEndpointBlock({ embedding: { status: 'ready' }, llm: { status: 'cold' } }, false);
	assert.match(block.message, /^Starting the answering model - /);
	assert.strictEqual(block.retryMs, 5000);
});

test('describeEndpointBlock: both cold sides are named', () => {
	const D = loadDialogMethods();
	const block = D.describeEndpointBlock({ embedding: { status: 'cold' }, llm: { status: 'cold' } }, false);
	assert.match(block.message, /^Starting the embedding and answering models - /);
});

test('describeEndpointBlock: indexing ignores the answering model', () => {
	const D = loadDialogMethods();
	const health = { embedding: { status: 'ready' }, llm: { status: 'cold' } };
	assert.strictEqual(D.describeEndpointBlock(health, true), null);
	assert.match(D.describeEndpointBlock({ embedding: { status: 'paused' }, llm: { status: 'ready' } }, true).message, /embedding model endpoint is paused/);
});

test('describeEndpointBlock: unreachable shows the provider detail', () => {
	const D = loadDialogMethods();
	const block = D.describeEndpointBlock({ embedding: { status: 'unreachable', detail: 'not provisioned' }, llm: null }, false);
	assert.match(block.message, /embedding model is not available: not provisioned/);
});

// --- re-ranking option and notice ---

test('describeRerankNotice: nothing to say without a re-ranker, when disabled or when ready', () => {
	const D = loadDialogMethods();
	const info = { model_type: 'remote', provider: { supports_provisioning: true, operable_by_caller: true } };
	assert.strictEqual(D.describeRerankNotice(null, { status: 'unreachable' }, true), null);
	assert.strictEqual(D.describeRerankNotice(info, { status: 'unreachable' }, false), null);
	assert.strictEqual(D.describeRerankNotice(info, { status: 'ready' }, true), null);
	assert.strictEqual(D.describeRerankNotice(info, null, true), null);
});

test('describeRerankNotice: a stopped endpoint the caller can start offers the button', () => {
	const D = loadDialogMethods();
	const info = { model_type: 'remote', provider: { supports_provisioning: true, operable_by_caller: true } };
	const view = D.describeRerankNotice(info, { status: 'paused', detail: '' }, true);
	assert.strictEqual(view.canStart, true);
	assert.match(view.message, /skipped/);
	const readOnly = D.describeRerankNotice({ ...info, provider: { supports_provisioning: false, operable_by_caller: false } }, { status: 'unreachable', detail: 'no key' }, true);
	assert.strictEqual(readOnly.canStart, false);
	assert.match(readOnly.message, /\(no key\).*preferences/);
	assert.strictEqual(D.describeRerankNotice(info, { status: 'cold' }, true).canStart, false);
});

test('rerankEnabledForQuery: the dialog checkbox only counts while the preset re-ranks', () => {
	const D = loadDialogMethods();
	const self = { rerankPrefEnabled: () => true, rerankInfo: { model_type: 'local', provider: null } };
	assert.strictEqual(D.rerankEnabledForQuery.call(self, true), false);
	assert.strictEqual(D.rerankEnabledForQuery.call(self, false), true);
	assert.strictEqual(D.rerankEnabledForQuery.call({ ...self, rerankInfo: null }, true), true);
	assert.strictEqual(D.rerankEnabledForQuery.call({ ...self, rerankPrefEnabled: () => false }, false), false);
});

// --- describePipelineProblems / checkPipelineOnOpen: the check when the dialog opens ---

test('describePipelineProblems: ready, starting, busy and unchecked sides need nothing', () => {
	const D = loadDialogMethods();
	assert.deepStrictEqual([...D.describePipelineProblems(null, true).lines], []);
	const health = { embedding: null, llm: { status: 'ready' }, rerank: { status: 'cold' } };
	assert.deepStrictEqual([...D.describePipelineProblems(health, true).lines], []);
	assert.deepStrictEqual([...D.describePipelineProblems({ llm: { status: 'throttled' } }, true).lines], []);
});

test('describePipelineProblems: paused and not running sides are listed in pipeline order', () => {
	const D = loadDialogMethods();
	const health = {
		embedding: { status: 'paused' },
		llm: { status: 'unreachable', detail: 'the MPCDF session ended; log in again' },
		rerank: { status: 'unreachable' },
	};
	const result = D.describePipelineProblems(health, true);
	assert.deepStrictEqual([...result.lines], [
		'The embedding endpoint is paused.',
		'The re-ranking endpoint is not running.',
		'The answering endpoint is not running (the MPCDF session ended; log in again).',
	]);
	assert.strictEqual(result.rerankOnly, false);
});

test('describePipelineProblems: a stopped re-ranker counts only when the query would use it', () => {
	const D = loadDialogMethods();
	const health = { embedding: null, llm: null, rerank: { status: 'unreachable' } };
	assert.deepStrictEqual([...D.describePipelineProblems(health, false).lines], []);
	const result = D.describePipelineProblems(health, true);
	assert.strictEqual(result.lines.length, 1);
	assert.strictEqual(result.rerankOnly, true);
});

/**
 * Load dialog.js with a recording Services.prompt.alert, and a fake `this` for
 * checkPipelineOnOpen with the given health and re-ranking info.
 * @param {any} health
 * @param {any} rerankInfo
 */
function pipelineCheckSetup(health, rerankInfo, offer = null) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const alerts = [];
	const opened = [];
	/** @type {any[]} */
	const offered = [];
	let offerFetches = 0;
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: () => null },
		window: { name: 'query-dialog' },
		Services: { prompt: { alert: (_win, _title, text) => alerts.push(text) } },
		Zotero: { Prefs: { get: () => true } },
		console,
	};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'dialog.js' });
	const D = context.ZoteroRAGDialog;
	const self = Object.assign(Object.create(D), {
		plugin: {
			openPresetDialog: (win) => opened.push(win),
			fetchStandinOffer: async () => { offerFetches++; return offer; },
		},
		_healthClosed: false,
		rerankInfo: null,
		endpointHealth: null,
		loadRerankInfo: async function () { this.rerankInfo = rerankInfo; },
		refreshEndpointHealth: async function () { this.endpointHealth = health; },
		showStandinOffer: (...args) => offered.push(args),
	});
	return { self, alerts, opened, offered, context, fetches: () => offerFetches };
}

test('checkPipelineOnOpen: warns and opens the Configure preset dialog for a stopped re-ranker', async () => {
	const { self, alerts, opened, context } = pipelineCheckSetup(
		{ embedding: null, llm: null, rerank: { status: 'unreachable', detail: 'the MPCDF session ended; log in again' } },
		{ model_type: 'remote', provider: null });
	await self.checkPipelineOnOpen();
	assert.strictEqual(alerts.length, 1);
	assert.match(alerts[0], /re-ranking endpoint is not running \(the MPCDF session ended; log in again\)/);
	assert.match(alerts[0], /answered without re-ranking until it is up/);
	assert.deepStrictEqual(opened, [context.window]);
});

test('checkPipelineOnOpen: says nothing when every side is usable or re-ranking is not part of the preset', async () => {
	const ready = pipelineCheckSetup({ embedding: { status: 'ready' }, llm: null, rerank: { status: 'ready' } }, { model_type: 'remote' });
	await ready.self.checkPipelineOnOpen();
	const noRerank = pipelineCheckSetup({ embedding: null, llm: null, rerank: { status: 'unreachable' } }, null);
	await noRerank.self.checkPipelineOnOpen();
	assert.deepStrictEqual([...ready.alerts, ...noRerank.alerts], []);
	assert.deepStrictEqual([...ready.opened, ...noRerank.opened], []);
});

test('checkPipelineOnOpen: a stopped answering model is reported as blocking questions', async () => {
	const { self, alerts, opened } = pipelineCheckSetup({ embedding: { status: 'paused' }, llm: { status: 'unreachable' } }, null);
	await self.checkPipelineOnOpen();
	assert.match(alerts[0], /cannot be answered until they are up/);
	assert.match(alerts[0], /start them or log in/);
	assert.strictEqual(opened.length, 1);
});

test('checkPipelineOnOpen: a down side with stand-ins on offer shows the offer instead of the alert and Configure preset', async () => {
	const offer = {
		code: 'quota_exhausted', message: "KISSKI's embedding quota is used up until 23:00 UTC.",
		side: 'embedding', provider: 'kisski', available_at: '2026-11-16T23:00:00Z',
		standins: [{ preset: 'remote-mpcdf', cost: 'free', label: 'MPCDF', needs: ['login:mpcdf'] }],
	};
	const { self, alerts, opened, offered } = pipelineCheckSetup({ embedding: { status: 'unreachable', detail: 'quota used up' }, llm: null }, null, offer);
	assert.strictEqual(await self.checkPipelineOnOpen(), true);
	assert.deepStrictEqual(alerts, []);
	assert.deepStrictEqual(opened, []);
	assert.strictEqual(offered.length, 1);
	assert.strictEqual(offered[0][0], offer);
	// Nothing to send again at open time.
	assert.strictEqual(offered[0][1], null);
});

test('checkPipelineOnOpen: without an offer (or one with no stand-ins) it alerts and opens Configure preset as before', async () => {
	const none = pipelineCheckSetup({ embedding: { status: 'unreachable' }, llm: null }, null, null);
	assert.strictEqual(await none.self.checkPipelineOnOpen(), true);
	assert.strictEqual(none.fetches(), 1);
	assert.strictEqual(none.alerts.length, 1);
	assert.strictEqual(none.opened.length, 1);
	assert.deepStrictEqual(none.offered, []);
	const empty = pipelineCheckSetup({ embedding: { status: 'unreachable' }, llm: null }, null, { code: 'quota_exhausted', standins: [] });
	await empty.self.checkPipelineOnOpen();
	assert.strictEqual(empty.alerts.length, 1);
	assert.deepStrictEqual(empty.offered, []);
	// No problem and no offer: nothing is shown.
	const ready = pipelineCheckSetup({ embedding: { status: 'ready' }, llm: null }, null, null);
	assert.strictEqual(await ready.self.checkPipelineOnOpen(), false);
	assert.strictEqual(ready.fetches(), 1);
	assert.deepStrictEqual([...ready.alerts, ...ready.opened, ...ready.offered], []);
});

test('checkPipelineOnOpen: offers stand-ins when the health shows no problem (a provider without a status check)', async () => {
	const offer = {
		code: 'quota_exhausted', message: "KISSKI's embedding quota is used up until 23:00 UTC.",
		side: 'embedding', provider: 'kisski', available_at: '2026-11-16T23:00:00Z',
		standins: [{ preset: 'remote-mpcdf', cost: 'free', label: 'MPCDF', needs: ['login:mpcdf'] }],
	};
	const t = pipelineCheckSetup({ embedding: null, llm: null, rerank: null }, null, offer);
	assert.strictEqual(await t.self.checkPipelineOnOpen(), true);
	assert.strictEqual(t.fetches(), 1);
	assert.deepStrictEqual(t.alerts, []);
	assert.deepStrictEqual(t.opened, []);
	assert.strictEqual(t.offered.length, 1);
	assert.strictEqual(t.offered[0][0], offer);
	assert.strictEqual(t.offered[0][1], null);
});

test('Use on an offer shown at open time switches, reloads config and health, and sends nothing', async () => {
	const { self, steps, sent, statuses } = standinRetrySetup(async () => {});
	self._standinRetry = null;
	await self.useStandinAndRetry('remote-mpcdf');
	assert.deepStrictEqual(steps, ['use remote-mpcdf kisski', 'hide', 'check', 'config']);
	assert.deepStrictEqual(sent, []);
	assert.deepStrictEqual(statuses, []);
});

test('a refused stand-in at open time shows the server message', async () => {
	const { self, sent, statuses } = standinRetrySetup(async () => { throw new Error('No quota is exhausted any more.'); });
	self._standinRetry = null;
	await self.useStandinAndRetry('remote-mpcdf');
	assert.deepStrictEqual(sent, []);
	assert.deepStrictEqual(statuses, ['No quota is exhausted any more.']);
});

test('showStandinOffer at open time shows the choices and keeps nothing to send again', () => {
	const { fakeNode } = require('./fake-dom.js');
	const nodes = { 'standin-offer': fakeNode(), 'standin-offer-text': fakeNode(), 'standin-offer-list': fakeNode() };
	nodes['standin-offer'].style.display = 'none';
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => nodes[id] || null, createElement: (/** @type {string} */ t) => fakeNode(t) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const D = context.ZoteroRAGDialog;
	const self = Object.assign(Object.create(D), { _resultStateActive: false, hasStoredKey: () => false, _standinRetry: { question: 'old' } });
	self.showStandinOffer(QUOTA_DETAIL, null);
	assert.strictEqual(nodes['standin-offer'].style.display, '');
	assert.strictEqual(nodes['standin-offer-list'].children.length, 2);
	assert.strictEqual(self._standinRetry, null);
	assert.strictEqual(self._standinOffer, QUOTA_DETAIL);
});

// ── Stand-in presets ────────────────────────────────────────────────────────

const QUOTA_DETAIL = {
	code: 'quota_exhausted',
	message: "KISSKI's embedding quota is used up until 23:00 UTC.",
	side: 'embedding', provider: 'kisski', available_at: '2026-11-16T23:00:00Z',
	standins: [
		{ preset: 'remote-mpcdf', cost: 'free', label: 'MPCDF', needs: ['login:mpcdf'] },
		{ preset: 'runpod', cost: 'paid', label: 'RunPod', needs: ['key:RUNPOD_API_KEY', 'login:runpod'] },
	],
};

/** The local wall-clock time of an ISO datetime as HH:MM (the dialog shows local time). */
function localClock(/** @type {string} */ iso) {
	const d = new Date(iso);
	return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

test('describeStandinOffer: says when the quota resets in local time and how long that is', () => {
	const D = loadDialogMethods();
	const { text } = D.describeStandinOffer(QUOTA_DETAIL, new Date('2026-11-16T19:00:00Z'), () => false);
	assert.strictEqual(text, `KISSKI's embedding quota is used up until ${localClock(QUOTA_DETAIL.available_at)} (in 4 h).`);
	const later = D.describeStandinOffer(QUOTA_DETAIL, new Date('2026-11-16T22:15:00Z'), () => false);
	assert.match(later.text, /\(in 45 min\)\.$/);
	const mixed = D.describeStandinOffer(QUOTA_DETAIL, new Date('2026-11-16T20:30:00Z'), () => false);
	assert.match(mixed.text, /\(in 2 h 30 min\)\.$/);
});

test('describeStandinOffer: keeps the server order and drops a key need the user has stored', () => {
	const D = loadDialogMethods();
	const { choices } = D.describeStandinOffer(QUOTA_DETAIL, new Date('2026-11-16T19:00:00Z'), (/** @type {string} */ name) => name === 'RUNPOD_API_KEY');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(choices)), [
		{ preset: 'remote-mpcdf', cost: 'free', label: 'MPCDF', needs: ['login:mpcdf'] },
		{ preset: 'runpod', cost: 'paid', label: 'RunPod', needs: ['login:runpod'] },
	]);
	const none = D.describeStandinOffer(QUOTA_DETAIL, new Date('2026-11-16T19:00:00Z'), () => false);
	assert.deepStrictEqual([...none.choices[1].needs], ['key:RUNPOD_API_KEY', 'login:runpod']);
});

test('describeStandinNeeds: names what must happen first, or that it is ready', () => {
	const D = loadDialogMethods();
	assert.strictEqual(D.describeStandinNeeds([]), 'ready');
	assert.strictEqual(D.describeStandinNeeds(['login:mpcdf', 'key:RUNPOD_API_KEY', 'login:runpod']),
		'needs MPCDF login, RUNPOD_API_KEY, RUNPOD login');
});

/**
 * A fake `this` for useStandinAndRetry recording the order of the steps.
 * @param {(preset: string) => Promise<void>} useStandin
 * @param {{blocked?: boolean, storedKeys?: string[], followUp?: boolean}} [opts]
 */
function standinRetrySetup(useStandin, opts = {}) {
	const D = loadDialogMethods();
	/** @type {string[]} */
	const steps = [];
	/** @type {any[]} */
	const sent = [];
	/** @type {string[]} */
	const statuses = [];
	const self = Object.assign(Object.create(D), {
		plugin: {
			useStandin: async (/** @type {string} */ p, /** @type {string} */ provider) => { steps.push(`use ${p} ${provider}`); await useStandin(p); },
			openPresetDialog: () => { steps.push('configure'); },
		},
		_standinRetry: { question: 'What is Zotero?', libraryIds: ['u1'], options: { minScore: 0.3, topK: 10, llmModel: 'old-model', enableRouting: true }, followUp: !!opts.followUp },
		_standinOffer: QUOTA_DETAIL,
		queryOptions: { minScore: 0.3, topK: 10, llmModel: 'old-model', enableRouting: true },
		hasStoredKey: (/** @type {string} */ k) => (opts.storedKeys || []).includes(k),
		hideStandinOffer() { steps.push('hide'); },
		checkPipelineOnOpen: async () => { steps.push('check'); return !!opts.blocked; },
		loadPresetConfig: async () => { steps.push('config'); },
		sendQuestion: async (/** @type {string} */ q, /** @type {string[]} */ ids, /** @type {any} */ opts) => { steps.push('send'); sent.push({ q, ids, opts }); },
		submitFollowUp: async function () { steps.push('follow-up'); sent.push({ opts: this.queryOptions }); },
		showStatus: (/** @type {string} */ m) => { statuses.push(m); },
	});
	return { self, steps, sent, statuses };
}

test('retry after a stand-in drops llmModel', async () => {
	const { self, steps, sent } = standinRetrySetup(async () => {});
	await self.useStandinAndRetry('remote-mpcdf');
	assert.deepStrictEqual(steps, ['use remote-mpcdf kisski', 'hide', 'check', 'config', 'send']);
	assert.strictEqual(sent.length, 1);
	assert.strictEqual(sent[0].q, 'What is Zotero?');
	assert.deepStrictEqual([...sent[0].ids], ['u1']);
	assert.strictEqual('llmModel' in sent[0].opts, false);
	assert.strictEqual(sent[0].opts.topK, 10);
});

test('a refused stand-in shows the server message and does not resend', async () => {
	const { self, steps, sent, statuses } = standinRetrySetup(async () => { throw new Error('No quota is exhausted any more.'); });
	await self.useStandinAndRetry('remote-mpcdf');
	assert.deepStrictEqual(sent, []);
	assert.ok(!steps.includes('check'));
	assert.deepStrictEqual(statuses, ['No quota is exhausted any more.']);
});

test('a stand-in whose pipeline is not ready is not re-sent; the user is told to submit again', async () => {
	const { self, steps, sent, statuses } = standinRetrySetup(async () => {}, { blocked: true });
	await self.useStandinAndRetry('remote-mpcdf');
	assert.deepStrictEqual(sent, []);
	assert.ok(!steps.includes('send'));
	assert.deepStrictEqual(statuses, ['Submit again once remote-mpcdf is ready.']);
});

test('a stand-in needing a key the user has not stored opens Configure preset and is not re-sent', async () => {
	const { self, steps, sent, statuses } = standinRetrySetup(async () => {});
	await self.useStandinAndRetry('runpod');
	assert.ok(steps.includes('configure'));
	assert.deepStrictEqual(sent, []);
	assert.deepStrictEqual(statuses, ['Submit again once runpod is ready.']);
});

test('a stand-in needing a key the user has stored is re-sent', async () => {
	const { self, sent } = standinRetrySetup(async () => {}, { storedKeys: ['RUNPOD_API_KEY'] });
	await self.useStandinAndRetry('runpod');
	assert.strictEqual(sent.length, 1);
});

test('a follow-up is re-sent as a follow-up without llmModel', async () => {
	const { self, steps, sent } = standinRetrySetup(async () => {}, { followUp: true });
	// The follow-up question stays in #followup-input after the failure; submitFollowUp sends it again.
	await self.useStandinAndRetry('remote-mpcdf');
	assert.ok(steps.includes('follow-up'));
	assert.ok(!steps.includes('send'));
	assert.strictEqual('llmModel' in sent[0].opts, false);
	assert.strictEqual(sent[0].opts.topK, 10);
});

test('a follow-up that hits an exhausted quota shows the offer in the result view', async () => {
	const { fakeNode } = require('./fake-dom.js');
	const nodes = {
		'followup-input': { value: 'Follow-up', disabled: false },
		'result-submit-button': { disabled: false },
		'result-standin-offer': fakeNode(),
		'result-standin-offer-text': fakeNode(),
		'result-standin-offer-list': fakeNode(),
	};
	nodes['result-standin-offer'].style.display = 'none';
	const context = {
		document: { readyState: 'loading', addEventListener() {}, getElementById: (/** @type {string} */ id) => nodes[id] || null, createElement: (/** @type {string} */ t) => fakeNode(t) },
		window: {}, console,
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const D = context.ZoteroRAGDialog;
	const err = Object.assign(new Error(QUOTA_DETAIL.message), { name: 'QuotaExhaustedError', detail: QUOTA_DETAIL });
	/** @type {string[]} */
	const statuses = [];
	const self = Object.assign(Object.create(D), {
		plugin: { submitQuery: async () => { throw err; } },
		libraryIds: ['u1'], turns: [{ question: 'Q0', result: { answer: 'A0' } }], noteID: null,
		queryOptions: { topK: 10, llmModel: 'old-model' },
		_resultStateActive: true,
		hasStoredKey: () => false,
		showStatus: (/** @type {string} */ m) => { statuses.push(m); },
		clearStatusMessages() {},
	});
	await self.submitFollowUp();
	assert.strictEqual(nodes['result-standin-offer'].style.display, '');
	assert.match(nodes['result-standin-offer-text'].textContent, /quota is used up until .* Use a stand-in until then\?$/);
	assert.strictEqual(nodes['result-standin-offer-list'].children.length, 2);
	assert.deepStrictEqual(statuses, []);
	assert.strictEqual(self._standinRetry.followUp, true);
	assert.strictEqual(self._standinRetry.question, 'Follow-up');
	assert.strictEqual(nodes['followup-input'].value, 'Follow-up'); // kept for a later submit
});

test('rememberModelChoice does not save a stand-in model as the user\'s own choice', () => {
	/** @type {Record<string, any>} */
	const prefs = {};
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Zotero: { Prefs: { set: (/** @type {string} */ k, /** @type {any} */ v) => { prefs[k] = v; } } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const self = Object.create(context.ZoteroRAGDialog);
	self.activeStandin = { preset: 'remote-mpcdf', until: '2026-11-16T23:00:00Z', replaces: 'kisski' };
	self.rememberModelChoice('standin-model');
	assert.deepStrictEqual(prefs, {});
	self.activeStandin = null;
	self.rememberModelChoice('own-model');
	assert.deepStrictEqual(prefs, { 'extensions.zotero-rag.llmModel': 'own-model' });
});

/** A fake `this` for currentLlmModel with a model select of two options. */
function llmModelSetup(/** @type {any} */ standin) {
	const rendered = [];
	let reloads = 0;
	/** @type {Record<string, any>} */
	const prefs = {};
	const context = {
		document: {
			readyState: 'loading', addEventListener() {},
			getElementById: (/** @type {string} */ id) => id === 'llm-model-select' ? { options: [1, 2], value: 'picked' } : null,
		},
		window: {}, console,
		Zotero: { Prefs: { set: (/** @type {string} */ k, /** @type {any} */ v) => { prefs[k] = v; } } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const self = Object.create(context.ZoteroRAGDialog);
	self.activeStandin = standin;
	self.plugin = null;
	self.loadPresetConfig = async () => { reloads++; };
	return { self, prefs, reloads: () => reloads, rendered };
}

test('currentLlmModel sends no model while a stand-in answers', async () => {
	const t = llmModelSetup({ preset: 'remote-mpcdf', until: new Date(Date.now() + 3600e3).toISOString(), replaces: 'kisski' });
	assert.strictEqual(await t.self.currentLlmModel(), undefined);
	assert.deepStrictEqual(t.prefs, {});
});

test('currentLlmModel ends an expired stand-in, refreshes the config and sends the own model', async () => {
	const t = llmModelSetup({ preset: 'remote-mpcdf', until: new Date(Date.now() - 1000).toISOString(), replaces: 'kisski' });
	assert.strictEqual(await t.self.currentLlmModel(), 'picked');
	assert.strictEqual(t.self.activeStandin, null);
	assert.strictEqual(t.reloads(), 1);
});

test('currentLlmModel sends the selected model without a stand-in', async () => {
	const t = llmModelSetup(null);
	assert.strictEqual(await t.self.currentLlmModel(), 'picked');
});

test('checkPipelineOnOpen reports whether a problem keeps questions from being answered', async () => {
	const blocked = pipelineCheckSetup({ embedding: { status: 'paused' }, llm: null }, null);
	assert.strictEqual(await blocked.self.checkPipelineOnOpen(), true);
	const rerankOnly = pipelineCheckSetup({ embedding: null, llm: null, rerank: { status: 'unreachable' } }, { model_type: 'remote', provider: null });
	assert.strictEqual(await rerankOnly.self.checkPipelineOnOpen(), false);
	const ready = pipelineCheckSetup({ embedding: { status: 'ready' }, llm: { status: 'ready' } }, null);
	assert.strictEqual(await ready.self.checkPipelineOnOpen(), false);
});

test('checkPipelineOnOpen: an offer already holding a pending retry is not replaced', async () => {
	const offer = {
		code: 'quota_exhausted', message: 'used up', side: 'embedding', provider: 'kisski',
		standins: [{ preset: 'remote-mpcdf', cost: 'free', label: 'MPCDF', needs: [] }],
	};
	const t = pipelineCheckSetup({ embedding: null, llm: null, rerank: null }, null, offer);
	t.self._standinRetry = { question: 'q', libraryIds: [1], options: {}, followUp: null };
	assert.strictEqual(await t.self.checkPipelineOnOpen(), true);
	assert.strictEqual(t.offered.length, 0);
});
