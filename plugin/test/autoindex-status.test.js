// Tests for plugin/src/autoindex-status.js's admin-only Snapshot-indexing controls.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'autoindex-status.js');

/**
 * autoindex-status.js references `window` and `fetch` as bare ambient
 * globals throughout (confirmed via `grep -n "await fetch(\|window\."
 * plugin/src/autoindex-status.js` — e.g. `pauseScheduler` calls bare
 * `fetch(...)`, and `init()` reads `window.arguments`/`window.close()`
 * directly, with no vm-context injection for either). `vm.createContext`
 * makes the passed object the global object of a *separate* JS realm, so a
 * plain static `window`/`fetch` value baked in at context-creation time
 * would NOT pick up a later `global.window = ...` / `global.fetch = ...`
 * reassignment from inside a test. To let each test swap in its own mock
 * after `loadDialog()` has already run (and already executed the file's
 * auto-init), `window` and `fetch` are defined as accessor properties on
 * the context that forward reads/writes to the outer Node `global` object.
 *
 * @param {Record<string, any>} elements - map of element id -> fake element object
 * @returns {any} a fresh ZoteroRAGAutoIndexStatus object
 */
function loadDialog(elements = {}) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	// Safe default so the file's bottom-of-file auto-init (which reads
	// `window.arguments`) doesn't throw before a test installs its own mock.
	global.window = { confirm: () => true, addEventListener: () => {} };
	const context = {
		document: {
			getElementById: (id) => elements[id] || { addEventListener: () => {}, style: {} },
		},
		console,
	};
	Object.defineProperties(context, {
		window: {
			get() { return global.window; },
			set(v) { global.window = v; },
			enumerable: true,
			configurable: true,
		},
		fetch: {
			get() { return global.fetch; },
			set(v) { global.fetch = v; },
			enumerable: true,
			configurable: true,
		},
	});
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'autoindex-status.js' });
	return context.ZoteroRAGAutoIndexStatus;
}

function makeElement() {
	return { style: {}, addEventListener: () => {}, checked: false, disabled: false, textContent: '' };
}

test('toggleIndexSnapshots PUTs true with no confirmation when checking the box', async () => {
	const toggle = makeElement();
	toggle.checked = true;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = {
		backendURL: 'http://backend',
		getAuthHeaders: () => ({}),
	};
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: true }) }; };
	global.window = { confirm: () => { throw new Error('must not be called'); } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(calls.length, 1);
	assert.strictEqual(calls[0].url, 'http://backend/api/admin/settings');
	assert.strictEqual(calls[0].opts.method, 'PUT');
	assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { index_snapshots: true });
});

test('toggleIndexSnapshots unchecking runs the two-step confirm and purges only on double-yes', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: false, deleted_chunks: 3, deleted_attachments: 1 }) }; };
	let confirmCallCount = 0;
	global.window = { confirm: () => { confirmCallCount++; return true; } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(confirmCallCount, 2);
	assert.strictEqual(calls.length, 2); // PUT, then purge
	assert.strictEqual(calls[0].opts.method, 'PUT');
	assert.strictEqual(calls[1].url, 'http://backend/api/admin/settings/purge-snapshots');
	assert.strictEqual(calls[1].opts.method, 'POST');
});

test('toggleIndexSnapshots unchecking does not purge when the first confirm is declined', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: false }) }; };
	global.window = { confirm: () => false };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(calls.length, 1); // PUT only, the flag still gets turned off
	assert.strictEqual(calls[0].opts.method, 'PUT');
});

test('toggleIndexSnapshots unchecking does not purge when only the second confirm is declined', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: true, json: async () => ({ index_snapshots: false }) }; };
	let confirmCallCount = 0;
	global.window = { confirm: () => { confirmCallCount++; return confirmCallCount === 1; } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(confirmCallCount, 2);
	assert.strictEqual(calls.length, 1); // PUT only, no purge call
});

test('toggleIndexSnapshots renders an error banner and does not proceed to the purge confirm when the PUT fails', async () => {
	const toggle = makeElement();
	toggle.checked = false;
	const banner = makeElement();
	const dialog = loadDialog({ 'admin-index-snapshots-toggle': toggle, 'run-banner': banner });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => { calls.push({ url, opts }); return { ok: false, status: 500, json: async () => ({ detail: 'disk full' }) }; };
	let confirmCalled = false;
	global.window = { confirm: () => { confirmCalled = true; return true; } };

	await dialog.toggleIndexSnapshots();

	assert.strictEqual(confirmCalled, false);
	assert.strictEqual(calls.length, 1); // PUT only, no purge call
	assert.match(banner.textContent, /disk full/);
});

test('purgeSnapshotsNow runs the same two-step confirm independent of checkbox state, disables the button during the call, and reports the result in the message span', async () => {
	const button = makeElement();
	const message = makeElement();
	const dialog = loadDialog({ 'admin-purge-snapshots-button': button, 'admin-purge-snapshots-message': message });
	/** @type {Array<{url: string, opts: any}>} */
	const calls = [];
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async (url, opts) => {
		calls.push({ url, opts });
		// The button must already be disabled by the time the request is in flight.
		assert.strictEqual(button.disabled, true);
		return { ok: true, json: async () => ({ deleted_chunks: 7, deleted_attachments: 2 }) };
	};
	global.window = { confirm: () => true };

	await dialog.purgeSnapshotsNow();

	assert.strictEqual(calls.length, 1);
	assert.strictEqual(calls[0].url, 'http://backend/api/admin/settings/purge-snapshots');
	assert.strictEqual(calls[0].opts.method, 'POST');
	assert.strictEqual(button.disabled, false);
	assert.match(message.textContent, /7/);
	assert.match(message.textContent, /2/);
});

test('purgeSnapshotsNow re-enables the button and reports an error in the message span when the request fails', async () => {
	const button = makeElement();
	const message = makeElement();
	const dialog = loadDialog({ 'admin-purge-snapshots-button': button, 'admin-purge-snapshots-message': message });
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'disk full' }) });
	global.window = { confirm: () => true };

	await dialog.purgeSnapshotsNow();

	assert.strictEqual(button.disabled, false);
	assert.match(message.textContent, /disk full/);
	assert.match(message.className, /error/);
});

test("setButtonLabel updates the nested .button-label span's text without touching the icon span", () => {
	const dialog = loadDialog({});
	const labelSpan = { textContent: 'Run indexing now' };
	const button = { querySelector: (sel) => (sel === '.button-label' ? labelSpan : null), textContent: 'should not be used' };

	dialog.setButtonLabel(button, 'Indexing in progress…');

	assert.strictEqual(labelSpan.textContent, 'Indexing in progress…');
	assert.strictEqual(button.textContent, 'should not be used');
});

test('setButtonLabel falls back to the button itself when there is no .button-label span', () => {
	const dialog = loadDialog({});
	const button = { querySelector: () => null, textContent: 'Delete indexed Snapshot entries…' };

	dialog.setButtonLabel(button, 'Deleting…');

	assert.strictEqual(button.textContent, 'Deleting…');
});

test('purgeSnapshotsNow makes no network call when either confirm is declined', async () => {
	const dialog = loadDialog({ 'run-banner': makeElement() });
	let fetchCalled = false;
	dialog.plugin = { backendURL: 'http://backend', getAuthHeaders: () => ({}) };
	global.fetch = async () => { fetchCalled = true; return { ok: true, json: async () => ({}) }; };
	global.window = { confirm: () => false };

	await dialog.purgeSnapshotsNow();

	assert.strictEqual(fetchCalled, false);
});
