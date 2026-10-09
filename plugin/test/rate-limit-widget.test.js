// Tests for plugin/src/rate-limit-widget.js and the Ask dialog's use of it.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WIDGET_PATH = path.join(__dirname, '..', 'src', 'rate-limit-widget.js');
const DIALOG_PATH = path.join(__dirname, '..', 'src', 'dialog.js');

/**
 * Run the widget (and optionally dialog.js) in one vm realm.
 * `fetch` forwards to the outer global so tests can swap it.
 * @param {Record<string, any>} elements - id -> fake element
 * @param {boolean} withDialog
 * @returns {any} the vm context
 */
function load(elements = {}, withDialog = false) {
	const context = {
		document: {
			readyState: 'loading',
			addEventListener() {},
			getElementById: (id) => elements[id] || null,
		},
		window: {},
		console,
	};
	Object.defineProperty(context, 'fetch', {
		get() { return global.fetch; }, set(v) { global.fetch = v; }, enumerable: true, configurable: true,
	});
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(WIDGET_PATH, 'utf8'), context, { filename: 'rate-limit-widget.js' });
	if (withDialog) vm.runInContext(fs.readFileSync(DIALOG_PATH, 'utf8'), context, { filename: 'dialog.js' });
	return context;
}

const h = (limit, remaining, period = 'hour') => ({
	[`x-ratelimit-limit-${period}`]: String(limit),
	[`x-ratelimit-remaining-${period}`]: String(remaining),
});

test('describe thresholds: 74% green, 75% amber, 94% amber, 95% red', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	assert.strictEqual(w.describe(h(100, 26), 'hour').color, '#2e9e4f');
	assert.strictEqual(w.describe(h(100, 25), 'hour').color, '#e6a817');
	assert.strictEqual(w.describe(h(100, 6), 'hour').color, '#e6a817');
	assert.strictEqual(w.describe(h(100, 5), 'hour').color, '#cc3300');
});

test('describe returns usedPct and text', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	const d = w.describe(h(200, 50, 'day'), 'day');
	assert.strictEqual(d.usedPct, 75);
	assert.strictEqual(d.remaining, 50);
	assert.strictEqual(d.text, '50 requests left/day');
});

test('describe returns null for missing/zero limits or headers', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	assert.strictEqual(w.describe(null, 'hour'), null);
	assert.strictEqual(w.describe({}, 'hour'), null);
	assert.strictEqual(w.describe(h(0, 0), 'hour'), null);
});

test('render honours visible flag and id prefix', () => {
	const mk = () => ({ style: {}, textContent: '' });
	const els = { 'x-rate-limit-section': mk(), 'x-rate-limit-bar-hour': mk(), 'x-rate-limit-text-hour': mk(),
		'x-rate-limit-bar-day': mk(), 'x-rate-limit-text-day': mk() };
	const ctx = load(els);
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, { ...h(100, 50), ...h(10, 9, 'day') }, { visible: true, prefix: 'x-' });
	assert.strictEqual(els['x-rate-limit-section'].style.display, '');
	assert.strictEqual(els['x-rate-limit-bar-hour'].style.width, '50%');
	assert.strictEqual(els['x-rate-limit-text-day'].textContent, '9 requests left/day');
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, null, { visible: false, prefix: 'x-' });
	assert.strictEqual(els['x-rate-limit-section'].style.display, 'none');
});

test('fetch resolves headers, or null when unavailable / failing', async () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	const plugin = { backendURL: 'http://x', getAuthHeaders: () => ({}) };
	global.fetch = async () => ({ ok: true, json: async () => ({ available: true, limits: h(1, 1) }) });
	assert.deepStrictEqual({ ...await w.fetch(plugin) }, h(1, 1));
	global.fetch = async () => ({ ok: true, json: async () => ({ available: false }) });
	assert.strictEqual(await w.fetch(plugin), null);
	global.fetch = async () => { throw new Error('down'); };
	assert.strictEqual(await w.fetch(plugin), null);
});

// --- Ask dialog regression + footer button -----------------------------------

test('Ask dialog updateRateLimitDisplay paints the same bars via the widget', () => {
	const mk = () => ({ style: {}, textContent: '' });
	const els = { 'rate-limit-section': mk(), 'rate-limit-bar-hour': mk(), 'rate-limit-text-hour': mk(),
		'rate-limit-bar-day': mk(), 'rate-limit-text-day': mk() };
	const ctx = load(els, true);
	const fake = { rateLimitAvailable: true, isOperationInProgress: true, isIndexOnlyMode: () => false,
		rateLimitHeaders: { ...h(100, 20), ...h(100, 1, 'day') } };
	ctx.ZoteroRAGDialog.updateRateLimitDisplay.call(fake);
	assert.strictEqual(els['rate-limit-section'].style.display, '');
	assert.strictEqual(els['rate-limit-bar-hour'].style.width, '80%');
	assert.strictEqual(els['rate-limit-bar-hour'].style.backgroundColor, '#e6a817');
	assert.strictEqual(els['rate-limit-text-hour'].textContent, '20 requests left/hour');
	assert.strictEqual(els['rate-limit-bar-day'].style.backgroundColor, '#cc3300');
	fake.rateLimitAvailable = false;
	ctx.ZoteroRAGDialog.updateRateLimitDisplay.call(fake);
	assert.strictEqual(els['rate-limit-section'].style.display, 'none');
});

/** Run refreshAutoindexButton against a canned /api/autoindex/status result. */
async function buttonDisplay(status, { fail = false, noBackend = false } = {}) {
	const button = { style: { display: 'none' } };
	const ctx = load({ 'autoindex-status-button': button }, true);
	global.fetch = async () => {
		if (fail) throw new Error('down');
		return { ok: true, json: async () => status };
	};
	const D = ctx.ZoteroRAGDialog;
	const fake = { plugin: { backendURL: noBackend ? '' : 'http://x', getAuthHeaders: () => ({}) },
		fetchAutoindexStatus: D.fetchAutoindexStatus };
	await D.refreshAutoindexButton.call(fake);
	return button.style.display;
}

test('Indexing status button is hidden when auto-indexing is disabled', async () => {
	assert.strictEqual(await buttonDisplay({ enabled: false, keys_registered: 3, scheduler: { active: true } }), 'none');
});
test('Indexing status button is hidden with no scheduler and no keys', async () => {
	assert.strictEqual(await buttonDisplay({ enabled: true, keys_registered: 0, scheduler: { active: false } }), 'none');
});
test('Indexing status button is hidden on fetch error or without backend URL', async () => {
	assert.strictEqual(await buttonDisplay({}, { fail: true }), 'none');
	assert.strictEqual(await buttonDisplay({ enabled: true, keys_registered: 1 }, { noBackend: true }), 'none');
});
test('Indexing status button is shown when the scheduler is active or keys are registered', async () => {
	assert.strictEqual(await buttonDisplay({ enabled: true, keys_registered: 0, scheduler: { active: true } }), '');
	assert.strictEqual(await buttonDisplay({ enabled: true, keys_registered: 2 }), '');
});

test('isServerIndexingRunning uses the shared status fetch and fails open', async () => {
	const ctx = load({}, true);
	const D = ctx.ZoteroRAGDialog;
	const fake = { plugin: { backendURL: 'http://x', getAuthHeaders: () => ({}) }, fetchAutoindexStatus: D.fetchAutoindexStatus };
	global.fetch = async () => ({ ok: true, json: async () => ({ running: true }) });
	assert.strictEqual(await D.isServerIndexingRunning.call(fake), true);
	global.fetch = async () => { throw new Error('x'); };
	assert.strictEqual(await D.isServerIndexingRunning.call(fake), false);
});
