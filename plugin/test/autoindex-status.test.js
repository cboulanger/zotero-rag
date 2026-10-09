// Tests for plugin/src/autoindex-status.js.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'autoindex-status.js');
const WIDGET_PATH = path.join(__dirname, '..', 'src', 'rate-limit-widget.js');

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
	// The real dialog loads rate-limit-widget.js into the same window first.
	vm.runInContext(fs.readFileSync(WIDGET_PATH, 'utf8'), context, { filename: 'rate-limit-widget.js' });
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

// --- Rate-limit widget in the Status section ---------------------------------

const HEADERS = {
	'x-ratelimit-limit-hour': '100', 'x-ratelimit-remaining-hour': '40',
	'x-ratelimit-limit-day': '1000', 'x-ratelimit-remaining-day': '10',
};

/** Fake element with a style bag; id-keyed registry for the rate-limit/preset markup. */
function fakeEl(extra = {}) {
	return { style: {}, textContent: '', addEventListener() {}, appendChild() {}, querySelector: () => null, ...extra };
}

function statusElements() {
	const ids = ['ai-rate-limit-section', 'ai-rate-limit-bar-hour', 'ai-rate-limit-text-hour',
		'ai-rate-limit-bar-day', 'ai-rate-limit-text-day', 'ai-rate-limit-asof', 'rate-limit-banner',
		'admin-preset-row', 'admin-preset-status', 'run-banner', 'admin-controls', 'run-now-button', 'admin-run-now-button'];
	const els = Object.fromEntries(ids.map((id) => [id, fakeEl()]));
	els['admin-preset-select'] = fakeEl({ value: '', disabled: false, title: '', innerHTML: '', options: [] });
	return els;
}

function widgetDialog(els) {
	const dialog = loadDialog(els);
	dialog.plugin = {
		backendURL: 'http://x',
		getAuthHeaders: () => ({}),
		isClientIndexingActive: () => false,
		/** @type {string[]} */
		presetNotifications: [],
		/** @param {string} source */
		notifyPresetChanged(source) { this.presetNotifications.push(source); },
	};
	dialog.renderLibraries = () => {};
	dialog.renderProblems = () => {};
	dialog.renderSystemHealth = () => {};
	return dialog;
}

test('rate-limit widget stays hidden when rate_limits.available is false', () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	dialog.rateLimitFallbackTried = true; // skip the open-time fallback fetch
	dialog.render({ enabled: true, keys_registered: 1, rate_limits: { available: false } });
	assert.strictEqual(els['ai-rate-limit-section'].style.display, 'none');
});

test('rate-limit widget shows bars from data.rate_limits', () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	dialog.render({ enabled: true, keys_registered: 1, rate_limits: { available: true, limits: HEADERS, source: 'run' } });
	assert.strictEqual(els['ai-rate-limit-section'].style.display, '');
	assert.strictEqual(els['ai-rate-limit-bar-hour'].style.width, '60%');
	assert.strictEqual(els['ai-rate-limit-text-hour'].textContent, '40 requests left/hour');
	assert.strictEqual(els['ai-rate-limit-bar-day'].style.backgroundColor, '#cc3300');
});

test('rate-limit widget falls back once to GET /api/rate-limits when status has no limits', async () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	const urls = [];
	global.fetch = async (url) => {
		urls.push(url);
		return { ok: true, json: async () => ({ available: true, limits: HEADERS }) };
	};
	const data = { enabled: true, keys_registered: 1, rate_limits: { available: false } };
	dialog.render(data);
	dialog.render(data);
	await new Promise((r) => setImmediate(r));
	assert.deepStrictEqual(urls, ['http://x/api/rate-limits']);
	assert.strictEqual(els['ai-rate-limit-section'].style.display, '');
});

test('rate-limit banner shows the earliest rate_limit_until among rate-limited slugs', () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	dialog.rateLimitFallbackTried = true;
	const early = '2026-10-09T10:00:00Z';
	assert.strictEqual(dialog.earliestRateLimitUntil({
		a: { status: 'skipped', skip_reason: 'embedding_rate_limit', rate_limit_until: '2026-10-09T12:00:00Z' },
		b: { status: 'skipped', skip_reason: 'embedding_rate_limit', rate_limit_until: early },
		c: { status: 'skipped', skip_reason: 'other', rate_limit_until: '2026-10-09T08:00:00Z' },
	}), early);
	dialog.render({ enabled: true, keys_registered: 1, slugs: {
		b: { status: 'skipped', skip_reason: 'embedding_rate_limit', rate_limit_until: early },
	} });
	assert.match(els['rate-limit-banner'].textContent, /Embedding rate limit reached; resumes at/);
	assert.strictEqual(els['rate-limit-banner'].style.display, '');
	dialog.render({ enabled: true, keys_registered: 1, slugs: {} });
	assert.strictEqual(els['rate-limit-banner'].style.display, 'none');
});

// --- Admin preset row --------------------------------------------------------

const PRESETS = [
	{ name: 'remote-kisski', active: true, credentials: 'ok' },
	{ name: 'remote-mpcdf', active: false, credentials: 'ok' },
];

test('preset row is hidden for non-admins and with fewer than 2 options', () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	dialog.rateLimitFallbackTried = true;
	dialog.switchablePresets = PRESETS;
	dialog.render({ enabled: true, keys_registered: 1, is_admin: false });
	assert.strictEqual(els['admin-preset-row'].style.display, 'none');
	dialog.switchablePresets = [PRESETS[0]];
	dialog.render({ enabled: true, keys_registered: 1, is_admin: true });
	assert.strictEqual(els['admin-preset-row'].style.display, 'none');
	dialog.switchablePresets = PRESETS;
	dialog.render({ enabled: true, keys_registered: 1, is_admin: true });
	assert.strictEqual(els['admin-preset-row'].style.display, '');
});

test('preset select is disabled while a run is in progress', () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	dialog.rateLimitFallbackTried = true;
	dialog.switchablePresets = PRESETS;
	dialog.render({ enabled: true, keys_registered: 1, is_admin: true, running: true, started_at: new Date().toISOString() });
	assert.strictEqual(els['admin-preset-select'].disabled, true);
	dialog.render({ enabled: true, keys_registered: 1, is_admin: true, running: false });
	assert.strictEqual(els['admin-preset-select'].disabled, false);
});

test('switchPreset reverts the select and shows detail on a 400', async () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	dialog.switchablePresets = PRESETS;
	let populated = 0;
	dialog.populatePresetSelect = () => { populated++; };
	global.fetch = async () => ({ ok: false, status: 400, json: async () => ({ detail: 'missing credentials: KEY' }) });
	await dialog.switchPreset('remote-mpcdf');
	assert.strictEqual(els['admin-preset-status'].textContent, 'Error: missing credentials: KEY');
	assert.strictEqual(populated, 1);
	assert.strictEqual(dialog.presetSwitching, false);
});

test('switchPreset POSTs, then refreshes presets and status on success', async () => {
	const els = statusElements();
	const dialog = widgetDialog(els);
	const calls = [];
	global.fetch = async (url, opts = {}) => {
		calls.push({ url, method: opts.method || 'GET', body: opts.body });
		if (url.endsWith('/api/config') && opts.method === 'POST') return { ok: true, json: async () => ({}) };
		if (url.endsWith('/api/config')) return { ok: true, json: async () => ({ switchable_presets: PRESETS }) };
		return { ok: true, json: async () => ({ enabled: true, keys_registered: 1, rate_limits: { available: false } }) };
	};
	dialog.rateLimitHeaders = HEADERS;
	await dialog.switchPreset('remote-mpcdf');
	assert.deepStrictEqual(JSON.parse(calls[0].body), { preset_name: 'remote-mpcdf' });
	assert.strictEqual(els['admin-preset-status'].textContent, 'Switched.');
	assert.ok(calls.some((c) => c.url.endsWith('/api/autoindex/status')));
	assert.ok(calls.filter((c) => c.url.endsWith('/api/config') && c.method === 'GET').length === 1);
	assert.strictEqual(dialog.rateLimitHeaders, null);
	// Other open windows (the Preferences pane) are told to refresh.
	assert.deepStrictEqual([...dialog.plugin.presetNotifications], ['autoindex-status']);
});
