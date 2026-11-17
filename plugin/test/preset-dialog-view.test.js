// Tests for plugin/src/preset-dialog-view.js (the "Configure preset" dialog).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { fakeNode, createElementNS } = require('./fake-dom.js');

const SRC = path.join(__dirname, '..', 'src');

function load() {
	const context = { console, setTimeout };
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(path.join(SRC, 'preset-dialog-view.js'), 'utf8'), context, { filename: 'preset-dialog-view.js' });
	return context.ZoteroRAGPresetDialog;
}

/** A side as GET /api/config/preset-view returns it. */
const side = (name, over = {}) => ({
	side: name, title: name, optional: false, enabled: true, provider: { id: 'p1', label: 'Provider One' },
	account: 'p1', state: 'ready', summary: 'Ready', fields: [], messages: [], facts: [], log: [], actions: [], ...over,
});
const start = (sideName, over = {}) => ({
	id: 'start', label: 'Start endpoint', kind: 'primary', enabled: true, reason: '', workflow: 'start', account: 'p1',
	request: { method: 'POST', path: '/api/config/provision', body: { sides: [sideName] } }, ...over,
});
const pause = (sideName) => ({
	id: 'pause', label: 'Pause', kind: 'normal', enabled: true, reason: '', workflow: 'pause', account: 'p1',
	request: { method: 'POST', path: '/api/config/suspend', body: { sides: [sideName] } },
});
const card = (over = {}) => ({
	id: 'p1', label: 'Provider One', serves: ['embedding', 'llm'], state: 'ready', scope_note: 'your own account',
	fields: [
		{ id: 'P1_KEY', label: 'P1_KEY', type: 'secret', storage: 'client_secret', key: 'P1_KEY', header: 'X-P1-Key', is_set: true, editable: true, required_for: ['indexing'] },
		{ id: 'session:P1_KEY', label: 'Provisioning key', type: 'secret', storage: 'session', key: 'P1_KEY', optional: true, editable: true },
	],
	messages: [], auto_start: true, ...over,
});
const view = (sides, accounts = [card()], over = {}) => ({
	preset: { name: 'main', description: 'Main preset', is_admin: false, default_options: [], default_preset: '' },
	sides, accounts, busy: false, ...over,
});

test('overall state: down wins over starting, starting over ready; switched-off sides are ignored', () => {
	const D = load();
	assert.deepStrictEqual({ ...D.overall([side('a'), side('b', { state: 'down' }), side('c', { state: 'starting' })]) },
		{ state: 'down', text: '1 of 3 ready, 1 starting, 1 down' });
	assert.strictEqual(D.overall([side('a'), side('b', { state: 'starting' })]).state, 'starting');
	assert.deepStrictEqual({ ...D.overall([side('a'), side('b', { state: 'off' })]) }, { state: 'ready', text: 'All ready' });
	assert.match(D.overall([side('a'), side('b', { state: 'unknown' })]).text, /1 not checked/);
});

test('Start all merges requests to the same path into one, joining sides and accounts', () => {
	const D = load();
	const v = view([side('embedding', { actions: [start('embedding')] }), side('llm', { actions: [start('llm'), pause('llm')] })]);
	const requests = D.workflowRequests(v, 'start');
	assert.strictEqual(requests.length, 1);
	assert.deepStrictEqual(JSON.parse(JSON.stringify(requests[0])), { path: '/api/config/provision', body: { sides: ['embedding', 'llm'] }, accounts: ['p1'] });
	// The source view is not mutated by merging.
	assert.deepStrictEqual([...v.sides[0].actions[0].request.body.sides], ['embedding']);
});

test('workflow requests skip disabled actions and can be limited to some sides', () => {
	const D = load();
	const v = view([
		side('embedding', { actions: [start('embedding', { enabled: false, reason: 'busy' })] }),
		side('llm', { actions: [start('llm')] }),
		side('other', { actions: [start('other', { request: { method: 'POST', path: '/api/config/warmup', body: {} }, account: null })] }),
	]);
	assert.deepStrictEqual([...D.workflowRequests(v, 'start').map((r) => r.path)], ['/api/config/provision', '/api/config/warmup']);
	assert.deepStrictEqual([...D.workflowRequests(v, 'start', ['llm']).map((r) => [...r.body.sides])], [['llm']]);
	assert.strictEqual(D.workflowRequests(v, 'pause').length, 0);
});

test('validation: numbers must be whole and at least min; patterns are anchored', () => {
	const D = load();
	const num = { label: 'Floor', type: 'number', min: 1 };
	assert.strictEqual(D.validate(num, ''), '');
	assert.strictEqual(D.validate(num, '3'), '');
	assert.match(D.validate(num, '0'), /at least 1/);
	assert.match(D.validate(num, '2.5'), /whole number/);
	const bounded = { label: 'Pool', type: 'number', min: 1, max: 500 };
	assert.strictEqual(D.validate(bounded, '500'), '');
	assert.match(D.validate(bounded, '501'), /at most 500/);
	const key = { label: 'Key', type: 'secret', pattern: 'k_[a-z]+' };
	assert.strictEqual(D.validate(key, 'k_abc'), '');
	assert.match(D.validate(key, 'xk_abc'), /expected format/);
});

// --- controller --------------------------------------------------------------------

function memoryPrefs() {
	const store = {};
	return { store, get: (n) => store[n], set: (n, v) => { store[n] = v; }, clear: (n) => { delete store[n]; } };
}

function memorySession() {
	const store = {};
	return { store, get: (e) => store[e] || '', set: (e, v) => { store[e] = v; }, forget: (e) => { delete store[e]; } };
}

function setup({ views, postReply, extra = {} }) {
	const D = load();
	const posts = [];
	let i = 0;
	const painted = [];
	const prefs = memoryPrefs();
	const sessionKeys = memorySession();
	const controller = D.createController({
		get: async (p) => (p === '/api/config/preset-view' ? views[Math.min(i++, views.length - 1)] : null),
		post: async (p, body) => { posts.push([p, JSON.parse(JSON.stringify(body))]); return postReply || { ok: true, status: 202, data: {} }; },
		prefs,
		saveShared: async () => ({ ok: true }),
		sessionKeys,
		paint: (v, local) => painted.push({ v, notes: { ...local.notes } }),
		sleep: async () => {},
		...extra,
	});
	return { D, controller, posts, painted, prefs, sessionKeys };
}

test('polling continues while something is starting and stops once everything settles', async () => {
	const starting = view([side('embedding', { state: 'starting' })]);
	const ready = view([side('embedding')]);
	const { controller, painted } = setup({ views: [starting, starting, ready, ready] });
	await controller.poll();
	assert.strictEqual(painted.length, 3);
	assert.strictEqual(controller.state.view.sides[0].state, 'ready');
});

test('client preference fields show the value stored in this Zotero', async () => {
	const field = { id: 'diversityFloor', label: 'Diversity floor', type: 'number', min: 1, storage: 'client_pref', key: 'diversityFloor', value: null };
	const { controller, prefs } = setup({ views: [view([side('retrieval', { fields: [field] })])] });
	prefs.set('extensions.zotero-rag.diversityFloor', 7);
	await controller.refresh();
	assert.strictEqual(controller.state.view.sides[0].fields[0].value, 7);
});

test('a backend that does not answer shows a header error instead of polling', async () => {
	const { controller, painted } = setup({ views: [null] });
	await controller.poll();
	assert.match(painted.at(-1).notes.header.text, /did not answer/);
});

test('an action posts its request with the session keys of its account', async () => {
	const v = view([side('llm', { actions: [start('llm')] })]);
	const { controller, posts, sessionKeys } = setup({ views: [v] });
	sessionKeys.set('P1_KEY', 'k_once');
	await controller.refresh();
	await controller.runAction('llm', v.sides[0].actions[0]);
	assert.deepStrictEqual(posts, [['/api/config/provision', { sides: ['llm'], keys: { P1_KEY: 'k_once' } }]]);
});

test('a refused session key is forgotten and the error is shown on the side', async () => {
	const v = view([side('llm', { actions: [start('llm')] })]);
	const { controller, sessionKeys, painted } = setup({ views: [v], postReply: { ok: false, status: 403, data: { detail: 'forbidden key' } } });
	sessionKeys.set('P1_KEY', 'k_bad');
	await controller.refresh();
	await controller.runAction('llm', v.sides[0].actions[0]);
	assert.strictEqual(sessionKeys.get('P1_KEY'), '');
	assert.ok(painted.some((p) => p.notes['side:llm'] && /forbidden key/.test(p.notes['side:llm'].text)));
});

test('Start all sends one merged request; Pause all with nothing to pause says so', async () => {
	const v = view([side('embedding', { actions: [start('embedding')] }), side('llm', { actions: [start('llm')] })]);
	const { controller, posts, painted } = setup({ views: [v] });
	await controller.refresh();
	assert.strictEqual(await controller.runWorkflow('start'), 1);
	assert.deepStrictEqual(posts, [['/api/config/provision', { sides: ['embedding', 'llm'] }]]);
	assert.strictEqual(await controller.runWorkflow('pause'), 0);
	assert.match(painted.at(-1).notes.header.text, /Nothing to pause/);
});

test('a client preference field saves a number, clears on empty and rejects bad input', async () => {
	const field = { id: 'diversityFloor', label: 'Diversity floor', type: 'number', min: 1, storage: 'client_pref', key: 'diversityFloor' };
	const { controller, prefs, painted } = setup({ views: [view([side('retrieval', { fields: [field] })])] });
	await controller.refresh();
	controller.setSideField(field, '5', 'side:retrieval');
	assert.strictEqual(prefs.store['extensions.zotero-rag.diversityFloor'], 5);
	controller.setSideField(field, '0', 'side:retrieval');
	assert.strictEqual(prefs.store['extensions.zotero-rag.diversityFloor'], 5);
	assert.match(painted.at(-1).notes['side:retrieval'].text, /at least 1/);
	controller.setSideField(field, '', 'side:retrieval');
	assert.ok(!('extensions.zotero-rag.diversityFloor' in prefs.store));
});

test('saving an account stores a personal key, keeps a one-time key in memory and starts the sides it serves', async () => {
	const down = view([side('embedding', { state: 'down', actions: [start('embedding')] }), side('extraction', { account: null, actions: [start('extraction', { request: { method: 'POST', path: '/api/config/warmup', body: {} } })] })]);
	const changed = [];
	const { controller, posts, prefs, sessionKeys } = setup({ views: [down], extra: { onKeysChanged: async (fields) => { changed.push(...fields.map((f) => f.id)); } } });
	await controller.refresh();
	controller.state.local.drafts['account:p1/P1_KEY'] = ' k_personal ';
	controller.state.local.drafts['account:p1/session:P1_KEY'] = 'k_admin';
	await controller.saveAccount(card());
	assert.strictEqual(prefs.store['extensions.zotero-rag.serviceApiKey.P1_KEY'], 'k_personal');
	assert.strictEqual(sessionKeys.get('P1_KEY'), 'k_admin');
	assert.deepStrictEqual(changed, ['P1_KEY']);
	// Only the sides the card serves are started (not the extraction side's own action).
	assert.deepStrictEqual(posts, [['/api/config/provision', { sides: ['embedding'], keys: { P1_KEY: 'k_admin' } }]]);
	assert.deepStrictEqual(Object.keys(controller.state.local.drafts), []);
});

test('a shared value that the server refuses stops the save and is shown on the card', async () => {
	const shared = { id: 'P1_URL', label: 'P1_URL', type: 'text', storage: 'shared', key: 'P1_URL', is_set: false, editable: true };
	const { controller, posts, painted } = setup({
		views: [view([side('llm', { actions: [start('llm')] })])],
		extra: { saveShared: async () => ({ ok: false, error: 'bad format' }) },
	});
	await controller.refresh();
	controller.state.local.drafts['account:p1/P1_URL'] = 'nope';
	await controller.saveAccount(card({ fields: [shared] }));
	assert.strictEqual(posts.length, 0);
	assert.match(painted.at(-1).notes['account:p1'].text, /bad format/);
});

test('the server default can be changed by an admin and other windows are told', async () => {
	let told = 0;
	const { controller, posts } = setup({ views: [view([side('llm')])], extra: { onPresetChanged: () => { told++; } } });
	await controller.refresh();
	await controller.setServerDefault('other');
	assert.deepStrictEqual(posts, [['/api/config', { preset_name: 'other' }]]);
	assert.strictEqual(told, 1);
});

// --- DOM ------------------------------------------------------------------------

function fakeDoc() {
	const make = (tag) => {
		const node = fakeNode(tag);
		node.contains = () => false;
		return node;
	};
	return { createElementNS: (_ns, tag) => make(tag), createTextNode: (t) => t, activeElement: null };
}

/** All text in a fake subtree. */
function text(node) {
	if (typeof node === 'string') return node;
	return [node.textContent || '', ...(node.children || []).map(text)].join(' ');
}

test('the dialog paints header, one tab per side with its state, the selected side and the account cards', () => {
	const D = load();
	const doc = fakeDoc();
	const refs = D.mount(doc, createElementNS(null, 'div'));
	const v = view([
		side('embedding', { title: 'Embedding', state: 'down', summary: 'Down', actions: [start('embedding')], log: ['line 1', 'line 2'],
			messages: [{ level: 'warn', text: 'Start it here.' }], facts: [{ label: 'Model', value: 'm1' }] }),
		side('llm', { title: 'Answering', state: 'starting', summary: 'Starting' }),
	]);
	const local = { tab: '', logOpen: {}, drafts: {}, notes: {} };
	const calls = [];
	const handlers = {
		onTab: (s) => calls.push(['tab', s]), onToggleLog: (s) => calls.push(['log', s]), onAction: (a) => calls.push(['action', a.id]),
		onFieldChange() {}, onSaveAccount: (c) => calls.push(['save', c.id]), onDefaultChange() {},
	};
	D.paint(doc, refs, v, local, handlers);
	assert.strictEqual(refs.title.textContent, 'main');
	assert.strictEqual(local.tab, 'embedding');
	assert.strictEqual(refs.tabs.children.length, 2);
	assert.match(refs.tabs.children[0].children[0].className, /pd-down/);
	assert.match(refs.tabs.children[1].children[0].className, /pd-starting/);
	assert.strictEqual(refs.startAll.disabled, false);
	assert.strictEqual(refs.pauseAll.disabled, true);
	assert.strictEqual(refs.defaultRow.hidden, true);
	const panel = text(refs.panel);
	assert.match(panel, /Start it here\./);
	assert.match(panel, /Show log \(2 lines\)/);
	assert.doesNotMatch(panel, /line 1/);
	assert.match(text(refs.cards), /Provider One/);
	assert.match(text(refs.cards), /Save and start/);

	refs.tabs.children[1].click();
	assert.deepStrictEqual(calls.at(-1), ['tab', 'llm']);
	local.logOpen.embedding = true;
	D.paint(doc, refs, v, local, handlers);
	assert.match(text(refs.panel), /line 1\nline 2/);
});

test('a field the caller may not change is shown read-only, and a card without editable fields has no Save button', () => {
	const D = load();
	const doc = fakeDoc();
	const refs = D.mount(doc, createElementNS(null, 'div'));
	const readOnly = card({ fields: [{ id: 'S_KEY', label: 'S_KEY', type: 'secret', storage: 'shared', key: 'S_KEY', is_set: true, editable: false, placeholder: 'Set by the server admin' }] });
	D.paint(doc, refs, view([side('llm')], [readOnly]), { tab: '', logOpen: {}, drafts: {}, notes: {} },
		{ onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {} });
	const cardText = text(refs.cards);
	assert.doesNotMatch(cardText, /Save/);
	const input = refs.cards.children[0].children[2].children[1];
	assert.strictEqual(input.readOnly, true);
	assert.strictEqual(input.value, 'Set by the server admin');
});

// -- re-ranking switch and account login ---------------------------------------------

const toggleField = () => ({ id: 'enableReranking', label: 'Use re-ranking', type: 'toggle', default: true, storage: 'client_pref', key: 'enableReranking' });
const rerankSide = (over = {}) => side('rerank', { optional: true, state: 'down', fields: [toggleField()],
	actions: [start('rerank', { workflow: null }), pause('rerank')], ...over });

test('a switched-off optional side is shown as off, ignored by Start all but still paused by Pause all', async () => {
	const v = view([side('embedding', { actions: [start('embedding')] }), rerankSide()]);
	const { controller, prefs, D, posts } = setup({ views: [v] });
	await controller.refresh();
	assert.strictEqual(controller.state.view.sides[1].state, 'down');
	assert.strictEqual(controller.state.view.sides[1].fields[0].value, true);
	await controller.setSideField(controller.state.view.sides[1].fields[0], 'false', 'side:rerank');
	assert.strictEqual(prefs.store['extensions.zotero-rag.enableReranking'], false);
	assert.strictEqual(controller.state.view.sides[1].state, 'off');
	const off = controller.state.view;
	assert.deepStrictEqual([...D.workflowRequests(off, 'start').map((r) => [...r.body.sides])], [['embedding']]);
	assert.deepStrictEqual([...D.workflowRequests(off, 'pause').map((r) => [...r.body.sides])], [['rerank']]);
	assert.strictEqual(posts.length, 0);
});

const loginCard = (over = {}) => ({
	id: 'p2', label: 'Provider Two', serves: ['embedding', 'llm'], state: 'needs_input', scope_note: '', auto_start: false,
	fields: [
		{ id: 'login:username', label: 'Username', type: 'text', storage: 'login', key: 'username' },
		{ id: 'login:password', label: 'Password', type: 'secret', storage: 'login', key: 'password' },
		{ id: 'login:token', label: 'Token', type: 'secret', storage: 'login', key: 'token', optional: true },
	],
	messages: [],
	actions: [{ id: 'login', label: 'Log in and start', kind: 'primary', enabled: true, reason: '', sends_login: true,
		request: { method: 'POST', path: '/api/config/session/login', body: { side: 'embedding' } } }],
	...over,
});

test('logging in sends the form once, clears it and reports a start that did not begin', async () => {
	const v = view([side('embedding', { account: 'p2', state: 'down' })], [loginCard()]);
	const { controller, posts, painted } = setup({ views: [v], postReply: { ok: true, status: 200, data: { state: 'ok', provisioning: null, provisioning_error: 'no capacity' } } });
	await controller.refresh();
	const c = controller.state.view.accounts[0];
	controller.state.local.drafts['account:p2/login:username'] = 'me';
	await controller.runCardAction(c, c.actions[0]);
	assert.strictEqual(posts.length, 0); // password missing
	assert.match(painted.at(-1).notes['account:p2'].text, /Password/);
	controller.state.local.drafts['account:p2/login:password'] = 'pw';
	await controller.runCardAction(c, c.actions[0]);
	assert.deepStrictEqual(posts, [['/api/config/session/login', { side: 'embedding', values: { username: 'me', password: 'pw' }, start_rerank: true }]]);
	assert.deepStrictEqual(Object.keys(controller.state.local.drafts), []);
	assert.ok(painted.some((p) => p.notes['account:p2'] && /did not start: no capacity/.test(p.notes['account:p2'].text)));
});

test('logging in asks the server not to start re-ranking when the switch is off', async () => {
	const v = view([side('embedding', { account: 'p2', state: 'down' })], [loginCard()]);
	const { controller, posts, prefs } = setup({ views: [v], postReply: { ok: true, status: 200, data: { state: 'ok', provisioning: null } } });
	prefs.store['extensions.zotero-rag.enableReranking'] = false;
	await controller.refresh();
	const c = controller.state.view.accounts[0];
	controller.state.local.drafts['account:p2/login:username'] = 'me';
	controller.state.local.drafts['account:p2/login:password'] = 'pw';
	await controller.runCardAction(c, c.actions[0]);
	assert.strictEqual(posts.length, 1);
	assert.strictEqual(posts[0][1].start_rerank, false);
});

test('Save ignores login fields, and a login card shows its actions instead of Save and start', () => {
	const D = load();
	const doc = { createElementNS, createTextNode: (t) => t, activeElement: null };
	const root = fakeNode('div');
	const refs = D.mount(doc, root);
	const v = view([side('embedding', { account: 'p2' })], [loginCard()]);
	const local = { tab: '', logOpen: {}, drafts: {}, notes: {} };
	D.paint(doc, refs, v, local, { onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {}, onCardAction() {} });
	const cardNode = refs.cards.children[0];
	const text = JSON.stringify(cardNode, (k, val) => (k === 'parentNode' ? undefined : val));
	assert.match(text, /Log in and start/);
	assert.match(text, /Not logged in/);
	assert.doesNotMatch(text, /Save and start/);
});

test('a failed load shows why the server refused instead of the generic message', async () => {
	const { controller, painted } = setup({ views: [null], extra: { lastError: () => 'This server does not have the Configure preset dialog yet (HTTP 404).' } });
	await controller.refresh();
	assert.match(painted.at(-1).notes.header.text, /HTTP 404/);
});

test('quota: the tab shows one bar per meter and the card the combined value', () => {
	const D = load();
	assert.deepStrictEqual({ ...D.describeMeter({ id: 'requests/hour', unit: 'requests', period: 'hour', limit: 100, remaining: 20 }) },
		{ usedPct: 80, level: 'warn', text: '20 of 100 requests left per hour' });
	assert.strictEqual(D.describeMeter({ id: 'x', unit: 'requests', limit: 0, remaining: 0 }), null);
	const quota = { used_pct: 80, level: 'warn', text: '80 % of the daily quota used', meters: [
		{ id: 'requests/hour', unit: 'requests', period: 'hour', limit: 100, remaining: 90 },
		{ id: 'requests/day', unit: 'requests', period: 'day', limit: 1000, remaining: 200 },
	] };
	const doc = { createElementNS, createTextNode: (t) => t, activeElement: null };
	const refs = D.mount(doc, fakeNode('div'));
	const v = view([side('llm', { quota })], [card({ quota })]);
	D.paint(doc, refs, v, { tab: 'llm', logOpen: {}, drafts: {}, notes: {} },
		{ onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {} });
	const dump = (n) => JSON.stringify(n, (k, val) => (k === 'parentNode' ? undefined : val));
	const panel = dump(refs.panel);
	assert.match(panel, /90 of 100 requests left per hour/);
	assert.match(panel, /"width":"80%"/);
	assert.match(dump(refs.cards), /Quota: 80 % of the daily quota used/);
});

// -- stand-ins for an exhausted quota -------------------------------------------------

const standinAction = () => ({ id: 'standin', label: 'Use a stand-in until 23:00', kind: 'normal', enabled: true, reason: '',
	workflow: null, request: null, account: null, sends_login: false });
const OFFER = {
	code: 'quota_exhausted', message: "KISSKI's embedding quota is used up until 23:00 UTC.",
	side: 'embedding', provider: 'kisski', available_at: '2026-11-16T23:00:00Z',
	standins: [
		{ preset: 'remote-mpcdf', cost: 'free', label: 'MPCDF', needs: ['login:mpcdf'] },
		{ preset: 'runpod', cost: 'paid', label: 'RunPod', needs: ['key:RUNPOD_API_KEY'] },
	],
};
const quotaSide = () => side('embedding', { state: 'down', summary: 'Down',
	messages: [{ level: 'info', text: OFFER.message }], actions: [standinAction()] });

function standinSetup({ offer, useStandin }) {
	const used = [];
	let fetches = 0;
	const kisski = view([quotaSide()]);
	const mpcdf = view([side('embedding')], [card()], { preset: { name: 'remote-mpcdf', description: '', is_admin: false, default_options: [], default_preset: '' } });
	const t = setup({
		views: [kisski, mpcdf],
		extra: {
			fetchStandinOffer: async () => { fetches++; return offer; },
			useStandin: async (preset, provider, start) => { used.push([preset, provider, start]); if (useStandin) await useStandin(preset); },
		},
	});
	return { ...t, used, fetches: () => fetches };
}

test('the standin action fetches the offer and lists the choices without posting anything', async () => {
	const { controller, posts, used, fetches, prefs } = standinSetup({ offer: OFFER });
	prefs.set('extensions.zotero-rag.serviceApiKey.RUNPOD_API_KEY', 'k');
	await controller.refresh();
	await controller.runAction('embedding', controller.state.view.sides[0].actions[0]);
	// Once when the dialog first found the used-up quota, once for the button.
	assert.strictEqual(fetches(), 2);
	assert.deepStrictEqual(posts, []);
	assert.deepStrictEqual(used, []);
	const offer = controller.state.local.standins;
	assert.strictEqual(offer.side, 'embedding');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(offer.choices)), [
		{ preset: 'remote-mpcdf', text: 'remote-mpcdf (free): MPCDF; needs MPCDF login', ready: false },
		{ preset: 'runpod', text: 'runpod (paid): RunPod; ready', ready: true },
	]);
});

test('the standin action with no offer says so plainly and lists nothing', async () => {
	const { controller, painted } = standinSetup({ offer: null });
	await controller.refresh();
	await controller.runAction('embedding', controller.state.view.sides[0].actions[0]);
	assert.ok(!controller.state.local.standins);
	assert.match(painted.at(-1).notes['side:embedding'].text, /No stand-in is on offer right now\./);
});

test('Use switches to the stand-in and refreshes the view, which then shows the stand-in preset', async () => {
	const { controller, used } = standinSetup({ offer: OFFER });
	await controller.refresh();
	await controller.runAction('embedding', controller.state.view.sides[0].actions[0]);
	await controller.useStandin('remote-mpcdf');
	// remote-mpcdf needs a login here, so its endpoints are not started yet.
	assert.deepStrictEqual(used, [['remote-mpcdf', 'kisski', false]]);
	assert.ok(!controller.state.local.standins);
	assert.strictEqual(controller.state.view.preset.name, 'remote-mpcdf');
});

test('a refused stand-in (409) shows the server message and keeps the choices', async () => {
	const { controller, painted } = standinSetup({ offer: OFFER, useStandin: async () => { throw new Error('No quota is exhausted any more.'); } });
	await controller.refresh();
	await controller.runAction('embedding', controller.state.view.sides[0].actions[0]);
	await controller.useStandin('remote-mpcdf');
	assert.strictEqual(controller.state.view.preset.name, 'main');
	assert.match(painted.at(-1).notes['side:embedding'].text, /No quota is exhausted any more\./);
	assert.ok(controller.state.local.standins);
});

test('the side panel lists the stand-ins with a Use button each', () => {
	const D = load();
	const doc = { createElementNS, createTextNode: (t) => t, activeElement: null };
	const refs = D.mount(doc, fakeNode('div'));
	const local = { tab: 'embedding', logOpen: {}, drafts: {}, notes: {}, standins: { side: 'embedding', provider: 'kisski', text: OFFER.message,
		choices: [{ preset: 'remote-mpcdf', text: 'remote-mpcdf (free): MPCDF; needs MPCDF login' }, { preset: 'runpod', text: 'runpod (paid): RunPod; ready' }] } };
	const picked = [];
	D.paint(doc, refs, view([quotaSide()]), local,
		{ onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {}, onUseStandin: (p) => picked.push(p) });
	const dump = JSON.stringify(refs.panel, (k, val) => (k === 'parentNode' || k === 'listeners' ? undefined : val));
	assert.match(dump, /remote-mpcdf \(free\): MPCDF; needs MPCDF login/);
	assert.match(dump, /runpod \(paid\): RunPod; ready/);
	/** Find the buttons labelled Use. */
	const uses = [];
	const walk = (n) => { if (n && typeof n === 'object') { if (n.tag === 'button' && n.textContent === 'Use') uses.push(n); (n.children || []).forEach(walk); } };
	walk(refs.panel);
	assert.strictEqual(uses.length, 2);
	uses[1].click();
	assert.deepStrictEqual(picked, ['runpod']);
});

test('an account card of a provider that cannot start endpoints reads Save, not Save and start', () => {
	const D = load();
	const doc = { createElementNS, createTextNode: (t) => t, activeElement: null };
	const refs = D.mount(doc, fakeNode('div'));
	D.paint(doc, refs, view([side('embedding')], [card({ auto_start: false })]), { tab: '', logOpen: {}, drafts: {}, notes: {} },
		{ onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {} });
	const dump = JSON.stringify(refs.cards, (k, val) => (k === 'parentNode' || k === 'listeners' ? undefined : val));
	assert.match(dump, /"textContent":"Save"/);
	assert.doesNotMatch(dump, /Save and start/);
});

test('opening the dialog on a used-up quota offers the stand-ins at once, on that side, only once', async () => {
	const other = side('extraction');
	const kisski = view([other, quotaSide()]);
	let fetches = 0;
	const { controller, painted } = setup({ views: [kisski, kisski], extra: { fetchStandinOffer: async () => { fetches++; return OFFER; } } });
	await controller.refresh();
	assert.strictEqual(fetches, 1);
	assert.strictEqual(controller.state.local.tab, 'embedding');
	assert.strictEqual(controller.state.local.standins.choices.length, 2);
	assert.match(painted.at(-1).notes.header.text, /quota is used up until 23:00 UTC\. Choose a stand-in/);
	// Later refreshes (polling, Check now) do not ask again or move the user to another tab.
	controller.selectTab('extraction');
	await controller.refresh();
	assert.strictEqual(fetches, 1);
	assert.strictEqual(controller.state.local.tab, 'extraction');
});

test('Save of an account card stays disabled until a value is typed', () => {
	const D = load();
	const doc = { createElementNS, createTextNode: (t) => t, activeElement: null };
	const refs = D.mount(doc, fakeNode('div'));
	const local = { tab: '', logOpen: {}, drafts: {}, notes: {} };
	D.paint(doc, refs, view([side('embedding')], [card({ auto_start: false })]), local,
		{ onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {} });
	/** @returns {any[]} every node below `node` */
	const all = (node) => [node, ...(node.children || []).filter((c) => typeof c === 'object').flatMap(all)];
	const nodes = all(refs.cards);
	const save = nodes.find((n) => n.tag === 'button' && n.textContent === 'Save');
	const input = nodes.find((n) => n.tag === 'input');
	assert.strictEqual(save.disabled, true);
	input.value = '   ';
	input.listeners.input.forEach((fn) => fn({ target: input }));
	assert.strictEqual(save.disabled, true, 'blanks are not a value');
	input.value = 'new-key';
	input.listeners.input.forEach((fn) => fn({ target: input }));
	assert.strictEqual(save.disabled, false);
});

test('Use on a ready stand-in starts its endpoints and clears the quota note in the header', async () => {
	const { controller, used, painted, prefs } = standinSetup({ offer: OFFER });
	prefs.set('extensions.zotero-rag.serviceApiKey.RUNPOD_API_KEY', 'k'); // its only need is met
	await controller.refresh();
	assert.ok(painted.at(-1).notes.header);
	await controller.useStandin('runpod');
	assert.deepStrictEqual(used, [['runpod', 'kisski', true]]);
	assert.ok(!painted.at(-1).notes.header, 'the note about the used-up quota is gone');
});

test('starting an endpoint whose job already runs is not reported as a failure', async () => {
	const v = view([side('llm', { state: 'down', actions: [start('llm')] })]);
	const { controller, painted } = setup({ views: [v], postReply: { ok: false, status: 409, data: { detail: 'A job for this endpoint is already running.' } } });
	await controller.refresh();
	await controller.runAction('llm', v.sides[0].actions[0]);
	const notes = painted.map((p) => p.notes['side:llm']).filter(Boolean);
	assert.ok(!notes.some((n) => n.level === 'error'), JSON.stringify(notes));
});

test('a side changing state is announced to the other windows, an unchanged refresh is not', async () => {
	const starting = view([side('rerank', { state: 'starting' })]);
	const ready = view([side('rerank')]);
	let announced = 0;
	const { controller } = setup({ views: [starting, starting, ready], extra: { onPresetChanged: () => { announced++; } } });
	await controller.refresh();
	await controller.refresh();
	assert.strictEqual(announced, 0);
	await controller.refresh();
	assert.strictEqual(announced, 1);
});

test('Start all that ends with a failed side says so in the header and shows that side', async () => {
	const down = view([side('extraction'), side('llm', { state: 'down', actions: [start('llm')] })]);
	const failed = view([side('extraction'), side('llm', { state: 'down', actions: [start('llm')],
		messages: [{ level: 'error', text: 'Last job failed: no capacity' }] })]);
	const { controller, painted } = setup({ views: [down, failed] });
	await controller.refresh();
	controller.selectTab('extraction');
	await controller.runWorkflow('start');
	assert.strictEqual(controller.state.local.tab, 'llm');
	const header = painted.at(-1).notes.header;
	assert.strictEqual(header.level, 'error');
	assert.match(header.text, /llm.*Last job failed: no capacity/);
});

// -- buttons while their action runs (issue #117) ---------------------------------------

/** A post that stays open until `release()` is called. */
function heldPost() {
	const posts = [];
	let release;
	const done = new Promise((resolve) => { release = resolve; });
	return { posts, release: () => release(), post: async (p, body) => { posts.push([p, body]); await done; return { ok: true, status: 202, data: {} }; } };
}

test('while an action runs it is tracked as running, a second click sends nothing, and it is released afterwards', async () => {
	const v = view([side('llm', { state: 'down', actions: [start('llm')] })]);
	const held = heldPost();
	const running = [];
	const { controller } = setup({ views: [v], extra: { post: held.post, paint: (_v, local) => running.push(Object.keys(local.running || {})) } });
	await controller.refresh();
	const first = controller.runAction('llm', v.sides[0].actions[0]);
	await new Promise((r) => setImmediate(r));
	assert.deepStrictEqual(Object.keys(controller.state.local.running), ['side:llm/start']);
	await controller.runAction('llm', v.sides[0].actions[0]);
	await controller.runWorkflow('start');
	assert.strictEqual(held.posts.length, 1, 'clicks while it runs do nothing');
	held.release();
	await first;
	assert.deepStrictEqual(Object.keys(controller.state.local.running), []);
	assert.ok(running.some((keys) => keys.includes('side:llm/start')), 'a paint showed it running');
});

test('Start all, Save and log in are tracked under their own buttons while their requests run', async () => {
	const v = view([side('embedding', { state: 'down', actions: [start('embedding')] })]);
	const cases = [
		['header/start', (c) => c.runWorkflow('start')],
		['account:p1/save', (c) => { c.state.local.drafts['account:p1/P1_KEY'] = 'k'; return c.saveAccount(card()); }],
		['account:p1/login', (c) => c.runCardAction(card(), { id: 'login', label: 'Log in', kind: 'primary', enabled: true, reason: '', request: { method: 'POST', path: '/api/x/login', body: {} } })],
	];
	for (const [key, act] of cases) {
		const held = heldPost();
		const { controller } = setup({ views: [v], extra: { post: held.post } });
		await controller.refresh();
		const pending = act(controller);
		await new Promise((r) => setImmediate(r));
		assert.deepStrictEqual(Object.keys(controller.state.local.running), [key], key);
		held.release();
		await pending;
		assert.deepStrictEqual(Object.keys(controller.state.local.running), [], key);
	}
});

test('Check now shows its spinner for one refresh and does not fetch twice', async () => {
	const v = view([side('llm')]);
	let fetches = 0;
	const seen = [];
	const { controller } = setup({ views: [v], extra: {
		get: async () => { fetches++; seen.push(Object.keys(controller.state.local.running)); return v; },
	} });
	await controller.checkNow();
	assert.strictEqual(fetches, 1);
	assert.deepStrictEqual(seen, [['header/check']]);
	assert.deepStrictEqual(Object.keys(controller.state.local.running), []);
});

test('the running action\'s button shows a spinner and every action button is disabled meanwhile', () => {
	const D = load();
	const doc = fakeDoc();
	const refs = D.mount(doc, createElementNS(null, 'div'));
	const v = view([side('llm', { state: 'down', actions: [start('llm'), pause('llm')] })], [card({ auto_start: false })]);
	const local = { tab: '', logOpen: {}, drafts: { 'account:p1/P1_KEY': 'k' }, notes: {}, running: { 'side:llm/start': true } };
	const handlers = { onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {} };
	D.paint(doc, refs, v, local, handlers);
	const all = (node) => [node, ...(node.children || []).filter((c) => typeof c === 'object').flatMap(all)];
	const buttons = all(refs.panel).filter((n) => n.tag === 'button' && /pd-button/.test(n.className));
	const [startButton, pauseButton] = buttons;
	assert.match(startButton.className, /pd-busy/);
	assert.strictEqual(startButton.attrs['aria-busy'], 'true');
	assert.strictEqual(startButton.disabled, true);
	assert.doesNotMatch(pauseButton.className, /pd-busy/);
	assert.strictEqual(pauseButton.disabled, true);
	assert.strictEqual(refs.startAll.disabled, true);
	assert.strictEqual(refs.checkNow.disabled, true);
	const save = all(refs.cards).find((n) => n.tag === 'button' && n.textContent === 'Save');
	assert.strictEqual(save.disabled, true);

	// Once it is done, the buttons are back, without spinner.
	local.running = {};
	D.paint(doc, refs, v, local, handlers);
	const after = all(refs.panel).filter((n) => n.tag === 'button' && /pd-button/.test(n.className));
	assert.doesNotMatch(after[0].className, /pd-busy/);
	assert.strictEqual(after[0].disabled, false);
	assert.strictEqual(refs.checkNow.disabled, false);
	assert.strictEqual(all(refs.cards).find((n) => n.tag === 'button' && n.textContent === 'Save').disabled, false);
});

test('a clicked (focused) Save button still shows its spinner; a focused text field keeps the card as typed', () => {
	const D = load();
	const doc = fakeDoc();
	const refs = D.mount(doc, createElementNS(null, 'div'));
	const v = view([side('llm')], [card({ auto_start: false })]);
	const local = { tab: '', logOpen: {}, drafts: { 'account:p1/P1_KEY': 'k' }, notes: {}, running: {} };
	const handlers = { onTab() {}, onToggleLog() {}, onAction() {}, onFieldChange() {}, onSaveAccount() {}, onDefaultChange() {} };
	D.paint(doc, refs, v, local, handlers);
	const all = (node) => [node, ...(node.children || []).filter((c) => typeof c === 'object').flatMap(all)];
	const save = all(refs.cards).find((n) => n.tag === 'button' && n.textContent === 'Save');
	refs.cards.contains = (node) => all(refs.cards).includes(node);
	doc.activeElement = save;
	local.running = { 'account:p1/save': true };
	D.paint(doc, refs, v, local, handlers);
	const repainted = all(refs.cards).find((n) => n.tag === 'button' && n.textContent === 'Save');
	assert.notStrictEqual(repainted, save, 'the card was rebuilt');
	assert.match(repainted.className, /pd-busy/);

	const input = all(refs.cards).find((n) => n.tag === 'input');
	doc.activeElement = input;
	D.paint(doc, refs, v, local, handlers);
	assert.strictEqual(all(refs.cards).find((n) => n.tag === 'input'), input, 'typing is not interrupted');
});
