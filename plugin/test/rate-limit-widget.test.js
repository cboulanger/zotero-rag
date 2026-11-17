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

const { fakeNode, createElementNS, readRows } = require('./fake-dom.js');

const m = (limit, remaining, period = 'hour', extra = {}) => ({
	id: `requests/${period}`, side: 'embedding', unit: 'requests', period, limit, remaining, ...extra,
});

test('describe thresholds: 74% green, 75% amber, 94% amber, 95% red', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	assert.strictEqual(w.describe(m(100, 26)).color, '#2e9e4f');
	assert.strictEqual(w.describe(m(100, 25)).color, '#e6a817');
	assert.strictEqual(w.describe(m(100, 6)).color, '#e6a817');
	assert.strictEqual(w.describe(m(100, 5)).color, '#cc3300');
});

test('describe returns usedPct and a unit/period label', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	const d = w.describe(m(200, 50, 'day'));
	assert.strictEqual(d.usedPct, 75);
	assert.strictEqual(d.text, '50 requests left/day');
	assert.strictEqual(w.describe({ ...m(10, 3), unit: 'tokens', period: null }).text, '3 tokens left');
});

test('describe returns null for a missing or zero limit', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	assert.strictEqual(w.describe(null), null);
	assert.strictEqual(w.describe(m(0, 0)), null);
});

test('render paints one bar per meter, honours visible and the id prefix', () => {
	const els = { 'x-rate-limit-section': fakeNode(), 'x-rate-limit-bars': fakeNode() };
	const ctx = load(els);
	ctx.document.createElementNS = createElementNS;
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, [m(100, 50), m(10, 9, 'day')], { visible: true, prefix: 'x-' });
	assert.strictEqual(els['x-rate-limit-section'].style.display, '');
	const rows = readRows(els['x-rate-limit-bars']);
	assert.deepStrictEqual(rows.map((r) => r.text), ['50 requests left/hour', '9 requests left/day']);
	assert.strictEqual(rows[0].width, '50%');
	// A second render replaces the rows instead of appending.
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, [m(100, 50)], { visible: true, prefix: 'x-' });
	assert.strictEqual(els['x-rate-limit-bars'].children.length, 1);
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, null, { visible: false, prefix: 'x-' });
	assert.strictEqual(els['x-rate-limit-section'].style.display, 'none');
	assert.strictEqual(els['x-rate-limit-bars'].children.length, 0);
});

test('render labels the side only when meters of both sides are shown', () => {
	const els = { 'rate-limit-section': fakeNode(), 'rate-limit-bars': fakeNode() };
	const ctx = load(els);
	ctx.document.createElementNS = createElementNS;
	const llm = { ...m(5, 5, 'minute'), side: 'llm' };
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, [m(100, 50), llm], { visible: true });
	assert.deepStrictEqual(readRows(els['rate-limit-bars']).map((r) => r.text),
		['Embedding: 50 requests left/hour', 'Answering: 5 requests left/minute']);
});

test('fetch resolves meters, or null when unavailable / failing', async () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	const plugin = { backendURL: 'http://x', getAuthHeaders: () => ({}) };
	global.fetch = async () => ({ ok: true, json: async () => ({ available: true, meters: [m(1, 1)] }) });
	assert.strictEqual((await w.fetch(plugin))[0].limit, 1);
	global.fetch = async () => ({ ok: true, json: async () => ({ available: false, meters: [] }) });
	assert.strictEqual(await w.fetch(plugin), null);
	global.fetch = async () => { throw new Error('down'); };
	assert.strictEqual(await w.fetch(plugin), null);
});

// --- Ask dialog regression + footer button -----------------------------------

test('Ask dialog updateRateLimitDisplay paints the same bars via the widget', () => {
	const els = { 'rate-limit-section': fakeNode(), 'rate-limit-bars': fakeNode() };
	const ctx = load(els, true);
	ctx.document.createElementNS = createElementNS;
	const fake = { rateLimitAvailable: true, isOperationInProgress: true, isIndexOnlyMode: () => false,
		rateLimitMeters: [m(100, 20), m(100, 1, 'day')] };
	ctx.ZoteroRAGDialog.updateRateLimitDisplay.call(fake);
	assert.strictEqual(els['rate-limit-section'].style.display, '');
	const rows = readRows(els['rate-limit-bars']);
	assert.strictEqual(rows[0].width, '80%');
	assert.strictEqual(rows[0].color, '#e6a817');
	assert.strictEqual(rows[0].text, '20 requests left/hour');
	assert.strictEqual(rows[1].color, '#cc3300');
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

// --- fixed size (#105) and costs of paid providers (#119) ---------------------

test('render shows at most two rows, the meters closest to running out, and lists all in the tooltip', () => {
	const els = { 'rate-limit-section': fakeNode(), 'rate-limit-bars': fakeNode() };
	const ctx = load(els);
	ctx.document.createElementNS = createElementNS;
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, [m(100, 90, 'minute'), m(100, 10), m(100, 50, 'day')], { visible: true });
	assert.deepStrictEqual(readRows(els['rate-limit-bars']).map((r) => r.text),
		['10 requests left/hour', '50 requests left/day']);
	assert.strictEqual(els['rate-limit-section'].title.split('\n').length, 3);
	ctx.ZoteroRAGRateLimitWidget.render(ctx.document, [m(100, 90, 'minute'), m(100, 10), m(100, 50, 'day')],
		{ visible: true, maxRows: Infinity });
	assert.strictEqual(els['rate-limit-bars'].children.length, 3);
});

const cost = (side, amount, extra = {}) => ({ side, amount, currency: 'USD', requests: 1, input_tokens: 100, output_tokens: 10, ...extra });

test('addCosts merges entries of the same side and currency without changing its inputs', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	const first = [cost('llm', 0.01)];
	const total = w.addCosts(first, [cost('llm', 0.02), cost('embedding', 0.001), cost('llm', 1, { currency: 'EUR' })]);
	assert.strictEqual(first[0].amount, 0.01);
	assert.strictEqual(total.length, 3);
	assert.ok(Math.abs(total[0].amount - 0.03) < 1e-12);
	assert.strictEqual(total[0].requests, 2);
	assert.strictEqual(total[0].input_tokens, 200);
	assert.deepStrictEqual(w.addCosts([], null), []);
});

test('formatAmount keeps four decimals below one cent', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	assert.strictEqual(w.formatAmount(0.0042, 'USD'), '$0.0042');
	assert.strictEqual(w.formatAmount(1.5, 'USD'), '$1.50');
	assert.strictEqual(w.formatAmount(0, 'USD'), '$0.00');
});

test('describeCosts gives a total, a per-side line and a tooltip with notes', () => {
	const { ZoteroRAGRateLimitWidget: w } = load();
	const d = w.describeCosts([cost('llm', 0.02), cost('embedding', 0.005, { note: 'Idle time is not counted.' })]);
	assert.strictEqual(d.total, 'Est. cost this session: $0.03');
	assert.strictEqual(d.detail, 'Answering $0.02 · Embedding $0.0050');
	assert.match(d.title, /Idle time is not counted\./);
	assert.match(d.title, /100 input \/ 10 output tokens/);
	assert.strictEqual(w.describeCosts([]).total, 'Est. cost this session: $0.00');
});

test('renderCosts paints the total and the breakdown into the same widget', () => {
	const els = { 'result-rate-limit-section': fakeNode(), 'result-rate-limit-bars': fakeNode() };
	const ctx = load(els);
	ctx.document.createElementNS = createElementNS;
	ctx.ZoteroRAGRateLimitWidget.renderCosts(ctx.document, [cost('llm', 0.25)], { visible: true, prefix: 'result-' });
	const rows = els['result-rate-limit-bars'].children;
	assert.strictEqual(rows[0].textContent, 'Est. cost this session: $0.25');
	assert.strictEqual(rows[1].textContent, 'Answering $0.25');
	ctx.ZoteroRAGRateLimitWidget.renderCosts(ctx.document, [], { visible: false, prefix: 'result-' });
	assert.strictEqual(els['result-rate-limit-section'].style.display, 'none');
	assert.strictEqual(rows.length, 2); // old array; the container was emptied
	assert.strictEqual(els['result-rate-limit-bars'].children.length, 0);
});

test('Ask dialog shows session costs instead of quota bars for a paid preset', () => {
	const els = { 'rate-limit-section': fakeNode(), 'rate-limit-bars': fakeNode() };
	const ctx = load(els, true);
	ctx.document.createElementNS = createElementNS;
	const D = ctx.ZoteroRAGDialog;
	const fake = { rateLimitAvailable: true, isOperationInProgress: false, isIndexOnlyMode: () => false,
		rateLimitMeters: [m(100, 20)], pricedSides: ['llm'], sessionCosts: [],
		updateRateLimitDisplay: D.updateRateLimitDisplay };
	D.updateRateLimitDisplay.call(fake);
	assert.strictEqual(els['rate-limit-section'].style.display, '');
	assert.strictEqual(els['rate-limit-bars'].children[0].textContent, 'Est. cost this session: $0.00');
	D.addSessionCosts.call(fake, [cost('llm', 0.01)]);
	D.addSessionCosts.call(fake, [cost('llm', 0.02)]);
	assert.strictEqual(els['rate-limit-bars'].children[0].textContent, 'Est. cost this session: $0.03');
});
