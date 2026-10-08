// Tests for plugin/src/autoindex-status.js.

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

test('_formatDurationFromNow returns null for missing/invalid input', () => {
	const dialog = loadDialog({});
	assert.strictEqual(dialog._formatDurationFromNow(undefined), null);
	assert.strictEqual(dialog._formatDurationFromNow('not-a-date'), null);
});

test('_formatDurationFromNow omits hours when under an hour', () => {
	const dialog = loadDialog({});
	const fifteenMinutesAgo = new Date(Date.now() - 15 * 60000).toISOString();
	assert.strictEqual(dialog._formatDurationFromNow(fifteenMinutesAgo), '15 minutes');
});

test('_formatDurationFromNow includes both hours and minutes', () => {
	const dialog = loadDialog({});
	const twoHoursFiveMinutesAgo = new Date(Date.now() - (2 * 60 + 5) * 60000).toISOString();
	assert.strictEqual(dialog._formatDurationFromNow(twoHoursFiveMinutesAgo), '2 hours, 5 minutes');
});

test('_formatDurationFromNow omits minutes when exactly on the hour', () => {
	const dialog = loadDialog({});
	const threeHoursAgo = new Date(Date.now() - 3 * 60 * 60000).toISOString();
	assert.strictEqual(dialog._formatDurationFromNow(threeHoursAgo), '3 hours');
});

test('_formatDurationFromNow uses singular units for exactly 1', () => {
	const dialog = loadDialog({});
	const oneHourOneMinuteAgo = new Date(Date.now() - 61 * 60000).toISOString();
	assert.strictEqual(dialog._formatDurationFromNow(oneHourOneMinuteAgo), '1 hour, 1 minute');
});

test('_formatDurationFromNow works for future timestamps too (e.g. "next run")', () => {
	const dialog = loadDialog({});
	// +30s buffer so flooring to whole minutes can't flake below 45 due to
	// the few ms of overhead between building this timestamp and the
	// function's own Date.now() call.
	const in45Minutes = new Date(Date.now() + (45 * 60 + 30) * 1000).toISOString();
	assert.strictEqual(dialog._formatDurationFromNow(in45Minutes), '45 minutes');
});

test('_updatePendingRunState returns false when there is no pending entry for the slug', () => {
	const dialog = loadDialog({});
	assert.strictEqual(dialog._updatePendingRunState('users/1', { status: 'done' }), false);
});

test('_updatePendingRunState stays true and marks confirmedStarted once the server reports indexing', () => {
	const dialog = loadDialog({});
	dialog.pendingRunSlugs.set('users/1', { since: Date.now(), confirmedStarted: false });
	assert.strictEqual(dialog._updatePendingRunState('users/1', { status: 'indexing' }), true);
	assert.strictEqual(dialog.pendingRunSlugs.get('users/1').confirmedStarted, true);
});

test('_updatePendingRunState clears once a confirmed-started run reaches a terminal status', () => {
	const dialog = loadDialog({});
	dialog.pendingRunSlugs.set('users/1', { since: Date.now(), confirmedStarted: true });
	assert.strictEqual(dialog._updatePendingRunState('users/1', { status: 'done' }), false);
	assert.strictEqual(dialog.pendingRunSlugs.has('users/1'), false);
});

test('_updatePendingRunState stays true within the grace window even if status still reads stale/terminal', () => {
	// Regression: right after a click, the status poll can still show the
	// PREVIOUS run's terminal status (e.g. "skipped" from an earlier attempt)
	// because the new subprocess hasn't started yet. Must not be mistaken for
	// "this run already finished".
	const dialog = loadDialog({});
	dialog.pendingRunSlugs.set('users/1', { since: Date.now(), confirmedStarted: false });
	assert.strictEqual(dialog._updatePendingRunState('users/1', { status: 'skipped', skip_reason: 'embedding_rate_limit' }), true);
});

test('_updatePendingRunState gives up after the grace window if never confirmed started', () => {
	// Regression: a run-slug request can fail fast server-side (e.g. the
	// embedding key is already rate-limited) without ever writing a fresh
	// per-slug status, which would otherwise leave the button disabled
	// forever. A stale `since` simulates that grace window having elapsed.
	const dialog = loadDialog({});
	dialog.pendingRunSlugs.set('users/1', { since: Date.now() - 20000, confirmedStarted: false });
	assert.strictEqual(dialog._updatePendingRunState('users/1', { status: 'skipped' }), false);
	assert.strictEqual(dialog.pendingRunSlugs.has('users/1'), false);
});
