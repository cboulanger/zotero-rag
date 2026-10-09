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
			// Minimal stand-in so renderLibraries/renderProblems/etc. (which
			// build rows via document.createElement) can run against non-empty
			// data without a real DOM — just enough surface (className,
			// textContent, dataset, appendChild, querySelector/addEventListener
			// no-ops) for those methods to not throw.
			createElement: () => ({
				style: {},
				dataset: {},
				children: [],
				appendChild(child) { this.children.push(child); },
				addEventListener: () => {},
				querySelector: () => null,
				removeAttribute: () => {},
			}),
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

test('_formatSkipOrErrorReason prefers error over skip_reason', () => {
	const dialog = loadDialog({});
	assert.strictEqual(
		dialog._formatSkipOrErrorReason({ status: 'error', error: 'boom', skip_reason: 'embedding_rate_limit' }),
		'boom'
	);
});

test('_formatSkipOrErrorReason turns embedding_rate_limit into a human-readable message with the reset time', () => {
	const dialog = loadDialog({});
	const until = new Date(Date.now() + 3600000).toISOString();
	const msg = dialog._formatSkipOrErrorReason({ status: 'skipped', skip_reason: 'embedding_rate_limit', rate_limit_until: until });
	assert.match(msg, /Embedding quota exhausted/);
	assert.match(msg, /resumes automatically at/);
	assert.ok(!msg.includes('embedding_rate_limit'), 'must not leak the raw machine-readable string');
});

test('_formatSkipOrErrorReason falls back to a generic message for embedding_rate_limit with no timestamp', () => {
	// Covers status entries written before rate_limit_until existed.
	const dialog = loadDialog({});
	const msg = dialog._formatSkipOrErrorReason({ status: 'skipped', skip_reason: 'embedding_rate_limit' });
	assert.match(msg, /Embedding quota exhausted/);
	assert.ok(!msg.includes('embedding_rate_limit'));
});

test('_formatSkipOrErrorReason passes through other skip reasons unchanged', () => {
	const dialog = loadDialog({});
	assert.strictEqual(
		dialog._formatSkipOrErrorReason({ status: 'skipped', skip_reason: 'Skipped by admin request' }),
		'Skipped by admin request'
	);
});

test('_formatSkipOrErrorReason surfaces a done run with partial item failures', () => {
	// A run can finish with status "done" and still have skipped some items
	// (e.g. an attachment download failure) — this must not be silently
	// dropped just because there's no top-level error/skip_reason.
	const dialog = loadDialog({});
	const msg = dialog._formatSkipOrErrorReason({ status: 'done', items_processed: 1, chunks_added: 0, items_failed: 1 });
	assert.match(msg, /1 item\(s\) failed to index/);
});

test('_formatSkipOrErrorReason prefers error and skip_reason over items_failed', () => {
	const dialog = loadDialog({});
	assert.strictEqual(
		dialog._formatSkipOrErrorReason({ status: 'error', error: 'boom', items_failed: 3 }),
		'boom'
	);
	assert.strictEqual(
		dialog._formatSkipOrErrorReason({ status: 'skipped', skip_reason: 'Skipped by admin request', items_failed: 3 }),
		'Skipped by admin request'
	);
});

test('_formatSkipOrErrorReason returns empty string when nothing failed', () => {
	const dialog = loadDialog({});
	assert.strictEqual(dialog._formatSkipOrErrorReason({ status: 'done', items_processed: 1, chunks_added: 1 }), '');
});

test('_advancePendingFlag returns false when there is no pending marker', () => {
	const dialog = loadDialog({});
	assert.strictEqual(dialog._advancePendingFlag(null, false), false);
});

test('_advancePendingFlag stays true and marks confirmedStarted once the signal confirms it started', () => {
	const dialog = loadDialog({});
	const pending = { since: Date.now(), confirmedStarted: false };
	assert.strictEqual(dialog._advancePendingFlag(pending, true), true);
	assert.strictEqual(pending.confirmedStarted, true);
});

test('_advancePendingFlag goes false once a confirmed-started marker sees the signal go terminal again', () => {
	const dialog = loadDialog({});
	const pending = { since: Date.now(), confirmedStarted: true };
	assert.strictEqual(dialog._advancePendingFlag(pending, false), false);
});

test('_advancePendingFlag stays true within the grace window even if the signal still reads not-started', () => {
	const dialog = loadDialog({});
	const pending = { since: Date.now(), confirmedStarted: false };
	assert.strictEqual(dialog._advancePendingFlag(pending, false), true);
});

test('_advancePendingFlag gives up after the grace window if never confirmed started', () => {
	const dialog = loadDialog({});
	const pending = { since: Date.now() - 20000, confirmedStarted: false };
	assert.strictEqual(dialog._advancePendingFlag(pending, false), false);
});

/**
 * Minimal element stubs so render() can run end-to-end: it touches several
 * ids via document.getElementById, but with empty slugs/issues/health the
 * only ones that need more than the default `{ style: {} }` fallback (see
 * loadDialog's doc comment) are the banner and the two run-now buttons,
 * whose methods (setButtonLabel's querySelector) the fallback doesn't have.
 * @returns {Record<string, any>}
 */
function renderTestElements() {
	return {
		'run-banner': {},
		'run-now-button': { disabled: false, querySelector: () => null, textContent: '' },
		'admin-run-now-button': { disabled: false, querySelector: () => null, textContent: '' },
		'libraries-container': { innerHTML: '', appendChild: () => {} },
	};
}

test('render shows a "starting" banner instead of the previous run\'s stale result while a just-triggered own run is unconfirmed', () => {
	// Regression: POST /api/autoindex/run returns before the spawned
	// subprocess updates on-disk status, so the very next status poll can
	// still carry the *previous* run's result (e.g. a rate-limit skip). That
	// must not clobber the "starting" feedback or re-enable the button.
	const elements = renderTestElements();
	const dialog = loadDialog(elements);
	dialog.pendingOwnRun = { since: Date.now(), confirmedStarted: false };

	dialog.render({
		enabled: true,
		running: false,
		crashed: true,
		slugs: {},
	});

	assert.match(elements['run-banner'].textContent, /Starting indexing/);
	assert.strictEqual(elements['run-banner'].className, 'running');
	assert.strictEqual(elements['run-now-button'].disabled, true);
	assert.strictEqual(elements['admin-run-now-button'].disabled, true);
});

test('render keeps suppressing the stale result for a just-triggered full admin run too', () => {
	const elements = renderTestElements();
	const dialog = loadDialog(elements);
	dialog.pendingAdminRun = { since: Date.now(), confirmedStarted: false };

	dialog.render({
		enabled: true,
		running: false,
		finished_at: new Date(Date.now() - 3600000).toISOString(),
		slugs: {},
	});

	assert.match(elements['run-banner'].textContent, /Starting indexing/);
	assert.strictEqual(elements['run-now-button'].disabled, true);
});

test('render drops the pending override once the server confirms the run is actually running', () => {
	const elements = renderTestElements();
	const dialog = loadDialog(elements);
	dialog.pendingOwnRun = { since: Date.now(), confirmedStarted: false };

	dialog.render({
		enabled: true,
		running: true,
		started_at: new Date().toISOString(),
		slugs: { 'users/1': { status: 'indexing' } },
	});

	assert.match(elements['run-banner'].textContent, /Running since/);
	assert.strictEqual(dialog.pendingOwnRun.confirmedStarted, true);
});

test('render clears pendingOwnRun once a confirmed run goes back to a terminal (not-running) state', () => {
	const elements = renderTestElements();
	const dialog = loadDialog(elements);
	dialog.pendingOwnRun = { since: Date.now(), confirmedStarted: true };

	dialog.render({
		enabled: true,
		running: false,
		finished_at: new Date().toISOString(),
		slugs: {},
	});

	assert.strictEqual(dialog.pendingOwnRun, null);
	assert.ok(!elements['run-now-button'].disabled);
});

test('render leaves per-slug status untouched once the pending run is confirmed running', () => {
	// Once confirmed, the live per-slug status is accurate and must be shown
	// as-is — the stale-result override only applies during the unconfirmed
	// race window, not for the run's full duration.
	const elements = renderTestElements();
	const dialog = loadDialog(elements);
	dialog.pendingOwnRun = { since: Date.now(), confirmedStarted: false };

	const renderLibrariesCalls = [];
	dialog.renderLibraries = (slugs, isAdmin, running, pendingStart) => {
		renderLibrariesCalls.push({ slugs, pendingStart });
	};

	dialog.render({
		enabled: true,
		running: true,
		started_at: new Date().toISOString(),
		slugs: { 'users/1': { status: 'indexing', items_processed: 2, items_total: 10 } },
	});

	assert.strictEqual(renderLibrariesCalls.length, 1);
	assert.strictEqual(renderLibrariesCalls[0].pendingStart, false);
	assert.strictEqual(renderLibrariesCalls[0].slugs['users/1'].status, 'indexing');
});
