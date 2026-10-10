// Tests for plugin/src/provider-sections.js (per-side configuration sections).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { fakeNode, createElementNS } = require('./fake-dom.js');

const SRC = path.join(__dirname, '..', 'src');

function load() {
	const context = { console };
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(path.join(SRC, 'provider-sections.js'), 'utf8'), context, { filename: 'provider-sections.js' });
	return context.ZoteroRAGProviderSections;
}

const provisioning = {
	credential: { env: 'SOME_API_KEY', label: 'API key', help: 'Used once.', pattern: '^k_[a-z]+$', optional: true },
};

/** A descriptor as GET /api/config/providers returns it. */
const descriptor = (over = {}) => ({
	id: 'p1', label: 'Provider One', key_scope: 'user', operable_by_caller: true,
	supports_provisioning: true, supports_suspend: true, provisioning,
	unavailable_hint: 'Provision it from here.', ...over,
});
const remote = (over) => ({ model_type: 'remote', provider: descriptor(over) });
const local = { model_type: 'local', provider: null };
const idle = { status: 'idle', progress: [], sides: {} };

test('same provider on both sides renders two independent sections', () => {
	const S = load();
	const a = S.buildModel('embedding', remote(), { status: 'ready', detail: '' }, idle);
	const b = S.buildModel('llm', remote(), { status: 'unreachable', detail: 'not provisioned' }, idle);
	assert.strictEqual(a.provision.visible, false);
	assert.strictEqual(b.provision.visible, true);
	assert.match(b.health.text, /unreachable \(not provisioned\)/);
	assert.strictEqual(b.health.color, 'red');
	assert.strictEqual(b.hint, 'Provision it from here.');
	assert.strictEqual(a.hint, '');
});

test('two provisioning providers: each side uses its own descriptor', () => {
	const S = load();
	const a = S.buildModel('embedding', remote({ label: 'Alpha' }), { status: 'throttled', detail: 'no capacity' }, idle);
	const b = S.buildModel('llm', remote({ label: 'Beta' }), { status: 'paused', detail: '' }, idle);
	assert.match(a.providerText, /Alpha \(your own account\)/);
	assert.match(b.providerText, /Beta/);
	assert.strictEqual(a.provision.label, 'Provision endpoint');
	assert.strictEqual(b.provision.label, 'Resume');
});

test('a provisioning side next to a non-provisioning one: only the first gets actions or a credential field', () => {
	const S = load();
	const plain = { model_type: 'remote', provider: descriptor({ supports_provisioning: false, supports_suspend: false, provisioning: null }) };
	const a = S.buildModel('embedding', remote(), { status: 'unreachable', detail: '' }, idle);
	const b = S.buildModel('llm', plain, { status: 'unreachable', detail: '' }, idle);
	assert.strictEqual(a.credential.visible, true);
	assert.strictEqual(b.credential.visible, false);
	assert.strictEqual(b.provision.visible, false);
	assert.strictEqual(b.readOnlyNote, false);
});

test('a local side has nothing to configure', () => {
	const S = load();
	const m = S.buildModel('embedding', local, undefined, idle);
	assert.deepStrictEqual([m.local, m.provision.visible, m.credential.visible, m.health], [true, false, false, null]);
});

test('a shared, non-operable side shows the provider hint and no actions', () => {
	const S = load();
	const shared = { model_type: 'remote', provider: descriptor({ key_scope: 'shared', operable_by_caller: false, supports_provisioning: false, supports_suspend: false, provisioning: null, unavailable_hint: 'Start a new job.' }) };
	const m = S.buildModel('llm', shared, { status: 'unreachable', detail: 'job expired' }, idle);
	assert.strictEqual(m.provision.visible, false);
	assert.strictEqual(m.hint, 'Start a new job.');
	assert.match(m.providerText, /provided by your institution/);
});

test('button visibility follows operable_by_caller, supports_provisioning and health', () => {
	const S = load();
	const cases = [
		// [operable, supportsProvisioning, status, expectedVisible, expectedNote]
		[true, true, 'unreachable', true, false],
		[true, true, 'paused', true, false],
		[true, true, 'throttled', true, false],
		[true, true, 'ready', false, false],
		[true, true, 'cold', false, false],       // a cold endpoint wakes by itself
		[false, true, 'unreachable', false, true], // admin-operated, caller is not an admin
		[true, false, 'unreachable', false, false],
	];
	for (const [operable, canProv, status, visible, note] of cases) {
		const info = remote({ operable_by_caller: operable, supports_provisioning: canProv });
		const m = S.buildModel('llm', info, { status, detail: '' }, idle);
		assert.strictEqual(m.provision.visible, visible, JSON.stringify([operable, canProv, status]));
		assert.strictEqual(m.readOnlyNote, note, JSON.stringify([operable, canProv, status]));
	}
});

test('credential field only for a side the caller can provision, with the descriptor label and pattern', () => {
	const S = load();
	const ok = S.buildModel('llm', remote(), { status: 'ready', detail: '' }, idle);
	assert.deepStrictEqual([ok.credential.visible, ok.credential.label, ok.credential.pattern], [true, 'API key', '^k_[a-z]+$']);
	const notOperable = S.buildModel('llm', remote({ operable_by_caller: false }), { status: 'ready', detail: '' }, idle);
	assert.strictEqual(notOperable.credential.visible, false);
});

test('while the callers job runs, actions are disabled with a reason; progress is routed per side', () => {
	const S = load();
	const job = { status: 'running', progress: ['embedding: creating', 'llm: waiting', 'embedding: ready'], sides: { embedding: { status: 'running', message: null } } };
	const e = S.buildModel('embedding', remote(), { status: 'unreachable', detail: '' }, job);
	assert.strictEqual(e.provision.disabled, true);
	assert.match(e.provision.reason, /already running/);
	assert.deepStrictEqual(e.progress, ['creating', 'ready']);
	assert.deepStrictEqual(S.buildModel('llm', remote(), { status: 'unreachable', detail: '' }, job).progress, ['waiting']);
});

test('a failed side offers Retry for that side only, and shows its message', () => {
	const S = load();
	const job = { status: 'failed', progress: [], sides: { embedding: { status: 'succeeded', message: null }, llm: { status: 'failed', message: 'boom' } } };
	const e = S.buildModel('embedding', remote(), { status: 'ready', detail: '' }, job);
	const l = S.buildModel('llm', remote(), { status: 'unreachable', detail: '' }, job);
	assert.strictEqual(e.retry.visible, false);
	assert.strictEqual(e.status, 'Done.');
	assert.strictEqual(l.retry.visible, true);
	assert.strictEqual(l.status, 'Failed: boom');
});

test('the DOM layer builds both sections once, updates them in place and clears the one-time key after use', () => {
	const S = load();
	const doc = { createElementNS };
	const container = fakeNode();
	const calls = [];
	const refs = S.ensureSections(doc, container, {
		onProvision: (side, key) => calls.push(['provision', side, key]),
		onRetry: (side, key) => calls.push(['retry', side, key]),
	});
	assert.strictEqual(container.children.length, 2);
	const models = {
		embedding: S.buildModel('embedding', remote(), { status: 'unreachable', detail: '' }, idle),
		llm: S.buildModel('llm', remote(), { status: 'ready', detail: '' }, idle),
	};
	S.update(refs, models);
	assert.strictEqual(refs.embedding.provision.hidden, false);
	assert.strictEqual(refs.llm.provision.hidden, true);

	refs.embedding.credInput.value = '  k_abc  ';
	refs.embedding.provision.click();
	assert.deepStrictEqual(calls, [['provision', 'embedding', 'k_abc']]);
	assert.strictEqual(refs.embedding.credInput.value, '');   // never kept
	refs.embedding.provision.click();                          // second click sends no key
	assert.deepStrictEqual(calls[1], ['provision', 'embedding', '']);

	// A refresh updates text but keeps the typed (not yet used) credential.
	refs.llm.credInput.value = 'typing';
	S.update(refs, models);
	assert.strictEqual(refs.llm.credInput.value, 'typing');
	assert.strictEqual(container.children.length, 2);        // no rebuild
});

test('a disabled provision button cannot be clicked', () => {
	const S = load();
	const doc = { createElementNS };
	const calls = [];
	const refs = S.ensureSections(doc, fakeNode(), { onProvision: (...a) => calls.push(a), onRetry: () => {} });
	const running = { status: 'running', progress: [], sides: { embedding: { status: 'running', message: null } } };
	S.update(refs, {
		embedding: S.buildModel('embedding', remote(), { status: 'unreachable', detail: '' }, running),
		llm: S.buildModel('llm', local, undefined, running),
	});
	refs.embedding.provision.click();
	assert.deepStrictEqual(calls, []);
});

test('the plugin sources name no provider (the backend descriptors carry all vendor knowledge)', () => {
	const files = fs.readdirSync(SRC).filter((f) => /\.(js|xhtml)$/.test(f) && !f.startsWith('toolkit'));
	for (const f of files) {
		const text = fs.readFileSync(path.join(SRC, f), 'utf8');
		assert.doesNotMatch(text, /runpod|kisski|mpcdf|hugging ?face|openai|anthropic/i, f);
	}
});

// --- Controller: actions, polling, retry and job resume ------------------------

/** A fake backend: GET answers from `routes`, POST is recorded and answered from `postReply`. */
function fakeBackend({ providers, health, jobs, postReply }) {
	const posts = [];
	let jobIndex = 0;
	return {
		posts,
		get: async (p) => {
			if (p === '/api/config/providers') return providers;
			if (p === '/api/config/health') return typeof health === 'function' ? health() : health;
			if (p === '/api/config/provision/status') return jobs[Math.min(jobIndex++, jobs.length - 1)];
			return null;
		},
		post: async (p, body) => { posts.push([p, body]); return postReply || { ok: true, status: 202, data: {} }; },
	};
}

const bothRemote = { sides: { embedding: remote(), llm: remote() } };
const sick = { embedding: { status: 'unreachable', detail: '' }, llm: { status: 'unreachable', detail: '' } };

function setup(backendOpts, extra = {}) {
	const S = load();
	const doc = { createElementNS };
	const refs = S.ensureSections(doc, fakeNode(), { onProvision() {}, onRetry() {} });
	const backend = fakeBackend(backendOpts);
	const controller = S.createController({ refs, get: backend.get, post: backend.post, sleep: async () => {}, ...extra });
	return { S, refs, backend, controller };
}

test('Provision posts only that side, with the one-time key under the descriptor\'s credential name', async () => {
	const { backend, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle] });
	await controller.refresh();
	await controller.provision('llm', 'k_secret');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(backend.posts[0])), ['/api/config/provision', { sides: ['llm'], keys: { SOME_API_KEY: 'k_secret' } }]);
});

test('without a one-time key no keys object is sent', async () => {
	const { backend, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle] });
	await controller.refresh();
	await controller.provision('embedding', '');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(backend.posts[0][1])), { sides: ['embedding'] });
});

test('a malformed one-time key is rejected before anything is sent', async () => {
	const { backend, refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle] });
	await controller.refresh();
	await controller.provision('llm', 'WRONG KEY');
	assert.deepStrictEqual(backend.posts, []);
	assert.match(refs.llm.status.textContent, /does not look right/);
});

test('Retry after a failed job sends only the failed side', async () => {
	const failedJob = { status: 'failed', progress: [], sides: { embedding: { status: 'succeeded', message: null }, llm: { status: 'failed', message: 'boom' } } };
	const { backend, refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [failedJob, idle] });
	await controller.refresh();
	assert.strictEqual(refs.llm.retry.hidden, false);
	assert.strictEqual(refs.embedding.retry.hidden, true);
	await controller.provision('llm', '');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(backend.posts.map(([, b]) => b.sides))), [['llm']]);
});

test('polling continues while the job runs and stops when it ends; progress lands in the right section', async () => {
	const running = { status: 'running', progress: ['embedding: creating', 'llm: waiting'], sides: { embedding: { status: 'running', message: null } } };
	const done = { status: 'succeeded', progress: ['embedding: ready'], sides: { embedding: { status: 'succeeded', message: null } } };
	let sleeps = 0;
	const { refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [running, running, done] }, { sleep: async () => { sleeps += 1; } });
	await controller.poll();
	assert.strictEqual(sleeps, 2);
	assert.strictEqual(controller.state.job.status, 'succeeded');
	assert.strictEqual(refs.embedding.provision.disabled, false);
});

test('opening the pane while a job is running resumes polling and shows its progress', async () => {
	const running = { status: 'running', progress: ['llm: warming up'], sides: { llm: { status: 'running', message: null } } };
	const done = { status: 'succeeded', progress: ['llm: warming up'], sides: { llm: { status: 'succeeded', message: null } } };
	const { refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [running, done] });
	const first = controller.poll();
	await first;
	assert.strictEqual(refs.llm.progress.textContent, 'warming up');
	assert.strictEqual(refs.embedding.progress.hidden, true);
});

test('a side\'s buttons are disabled with a reason only while that side runs a job', async () => {
	const running = { status: 'running', progress: [], sides: { llm: { status: 'running', message: null } } };
	const { refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [running, { status: 'succeeded', progress: [], sides: {} }] });
	await controller.refresh();
	assert.strictEqual(refs.llm.provision.disabled, true);
	assert.match(refs.llm.provision.title, /already running/);
	assert.strictEqual(refs.embedding.provision.disabled, false);
});

test('401 / 403 / 409 from the backend are shown inline in the section', async () => {
	for (const [status, detail] of [[401, 'Missing or invalid Zotero API key.'], [403, 'not an admin'], [409, 'A provisioning job is already running.']]) {
		const { refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle], postReply: { ok: false, status, data: { detail } } });
		await controller.refresh();
		await controller.provision('llm', '');
		assert.strictEqual(refs.llm.status.textContent, `Provisioning failed: ${detail}`);
		assert.strictEqual(refs.llm.status.hidden, false);
	}
});

test('a network error while starting is shown inline', async () => {
	const { refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle] });
	const S = load();
	const failing = S.createController({ refs, get: async () => null, post: async () => { throw new Error('down'); }, sleep: async () => {} });
	await failing.provision('llm', '');
	assert.match(refs.llm.status.textContent, /down/);
});

// --- Preset controls ------------------------------------------------------------

const cfg = { default_preset: 'main', preset_name: 'mine', compatible_presets: ['main', 'mine', 'other'], switchable_presets: [{ name: 'main' }, { name: 'other' }] };
const my = {
	loopback: false, is_admin: false, default: 'main', effective: 'mine', fell_back: null,
	selectable: [
		{ name: 'main', credentials: 'ok', missing_keys: [] },
		{ name: 'mine', credentials: 'ok', missing_keys: [] },
		{ name: 'other', credentials: 'missing', missing_keys: ['OTHER_KEY'] },
	],
};

test('preset controls for a user: their own choice, default marked, presets needing a key flagged', () => {
	const v = load().buildPresetControls(my, cfg);
	assert.deepStrictEqual([v.mode, v.label, v.selected, v.showDefault], ['user', 'My preset:', 'mine', false]);
	assert.deepStrictEqual(v.options.map((o) => o.text), ['main (server default)', 'mine', 'other — needs a key']);
});

test('admins also get the Server default control', () => {
	const v = load().buildPresetControls({ ...my, is_admin: true }, cfg);
	assert.strictEqual(v.showDefault, true);
	assert.deepStrictEqual([...v.defaultOptions.map((o) => o.value)], ['main', 'other']);
	assert.strictEqual(v.defaultSelected, 'main');
});

test('a loopback server shows one control that changes the default', () => {
	const v = load().buildPresetControls({ ...my, loopback: true }, cfg);
	assert.deepStrictEqual([v.mode, v.label, v.selected, v.showDefault], ['loopback', 'Preset:', 'main', false]);
	assert.deepStrictEqual(v.options.map((o) => o.value), ['main', 'mine', 'other']);
});

test('a saved choice that is no longer honoured is explained', () => {
	const v = load().buildPresetControls({ ...my, effective: 'main', fell_back: 'gone' }, cfg);
	assert.match(v.note, /"gone" is not available any more/);
});

test('header names for key names match the backend rule', () => {
	const S = load();
	assert.strictEqual(S.headerForKey('KISSKI_API_KEY'), 'X-Kisski-Api-Key');
	assert.strictEqual(S.headerForKey('SOME_API_KEY'), 'X-Some-Api-Key');
});

test('wizard intro names the starting preset, counts only the keys it needs and mentions the upgrade path', () => {
	const S = load();
	const text = S.keysIntro({ preset_name: 'main', preset_description: 'Shared gateway.' }, [{ key_name: 'A_KEY' }]);
	assert.match(text, /"main" — Shared gateway\./);
	assert.match(text, /this key/);
	assert.match(text, /Model preset/);
	assert.match(S.keysIntro({ preset_name: 'main' }, []), /needs no key/);
	assert.match(S.keysIntro({ preset_name: 'main' }, [{ key_name: 'A' }, { key_name: 'B' }]), /these keys/);
});

// --- Pause / Resume --------------------------------------------------------------

test('Pause is offered for a ready or cold side the caller can operate, Resume for a paused one', () => {
	const S = load();
	const cases = [
		// [status, operable, supportsSuspend, pauseVisible, provisionLabel, provisionVisible]
		['ready', true, true, true, 'Provision endpoint', false],
		['cold', true, true, true, 'Provision endpoint', false],
		['paused', true, true, false, 'Resume', true],
		['unreachable', true, true, false, 'Provision endpoint', true],
		['ready', false, true, false, 'Provision endpoint', false],   // not operable by this caller
		['ready', true, false, false, 'Provision endpoint', false],   // provider cannot pause
		['throttled', true, true, false, 'Provision endpoint', true],
	];
	for (const [status, operable, canSuspend, pauseVisible, label, provisionVisible] of cases) {
		const m = S.buildModel('llm', remote({ operable_by_caller: operable, supports_suspend: canSuspend }), { status, detail: '' }, idle);
		assert.strictEqual(m.pause.visible, pauseVisible, JSON.stringify([status, operable, canSuspend]));
		assert.strictEqual(m.provision.visible, provisionVisible, JSON.stringify([status, operable, canSuspend]));
		assert.strictEqual(m.provision.label, label);
	}
});

test('a paused side shows a neutral (non-error) status row', () => {
	const S = load();
	const m = S.buildModel('llm', remote(), { status: 'paused', detail: 'paused; resume it to use it again' }, idle);
	assert.strictEqual(m.health.color, 'orange');
	assert.match(m.health.text, /paused/);
});

test('Pause posts only that side to the suspend route, and Resume is the provision route', async () => {
	const { backend, controller } = setup({ providers: bothRemote, health: { embedding: { status: 'ready', detail: '' }, llm: { status: 'paused', detail: '' } }, jobs: [idle] });
	await controller.refresh();
	await controller.pause('embedding');
	await controller.provision('llm', '');
	assert.deepStrictEqual(JSON.parse(JSON.stringify(backend.posts)), [
		['/api/config/suspend', { sides: ['embedding'] }],
		['/api/config/provision', { sides: ['llm'] }],
	]);
});

test('pausing shows its failures inline too', async () => {
	const { refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle], postReply: { ok: false, status: 403, data: { detail: 'not an admin' } } });
	await controller.refresh();
	await controller.pause('llm');
	assert.strictEqual(refs.llm.status.textContent, 'Pausing failed: not an admin');
});

test('the Pause button of a section calls the onPause handler and is disabled while a job runs', () => {
	const S = load();
	const calls = [];
	const refs = S.ensureSections({ createElementNS }, fakeNode(), { onProvision() {}, onRetry() {}, onPause: (side) => calls.push(side) });
	const readyEverywhere = { status: 'ready', detail: '' };
	S.update(refs, { embedding: S.buildModel('embedding', remote(), readyEverywhere, idle), llm: S.buildModel('llm', local, undefined, idle) });
	refs.embedding.pause.click();
	assert.deepStrictEqual(calls, ['embedding']);
	S.update(refs, { embedding: S.buildModel('embedding', remote(), readyEverywhere, { status: 'running', progress: [], sides: { embedding: { status: 'running', message: null } } }), llm: S.buildModel('llm', local, undefined, idle) });
	refs.embedding.pause.click();
	assert.deepStrictEqual(calls, ['embedding']);
});

test('the pane keeps polling while an endpoint is cold and stops once it is ready', async () => {
	const cold = { embedding: { status: 'cold', detail: 'starting' }, llm: { status: 'ready', detail: '' } };
	const ready = { embedding: { status: 'ready', detail: '' }, llm: { status: 'ready', detail: '' } };
	const seq = [cold, cold, ready];
	let sleeps = 0;
	const { refs, controller } = setup(
		{ providers: bothRemote, health: () => seq.length > 1 ? seq.shift() : seq[0], jobs: [idle] },
		{ sleep: async () => { sleeps += 1; } },
	);
	await controller.poll();
	assert.strictEqual(sleeps, 2);
	assert.match(refs.embedding.health.textContent, /ready/);
});

test('polling a cold endpoint is bounded', async () => {
	const cold = { embedding: { status: 'cold', detail: '' }, llm: { status: 'ready', detail: '' } };
	let sleeps = 0;
	const { controller } = setup({ providers: bothRemote, health: cold, jobs: [idle] }, { sleep: async () => { sleeps += 1; } });
	await controller.poll();
	assert.strictEqual(sleeps, 120);
});

test('Pause sends the one-time key too, so a call-only stored key can still pause', async () => {
	const S = load();
	const calls = [];
	const refs = S.ensureSections({ createElementNS }, fakeNode(), { onProvision() {}, onRetry() {}, onPause: (side, key) => calls.push([side, key]) });
	refs.embedding.credInput.value = ' k_secret ';
	refs.embedding.pause.click();
	assert.deepStrictEqual(calls, [['embedding', 'k_secret']]);
	assert.strictEqual(refs.embedding.credInput.value, '');
});

test('the credential field shows while Pause is on offer and hides when no action is', () => {
	const S = load();
	const ready = S.buildModel('embedding', remote(), { status: 'ready', detail: '' }, idle);
	assert.strictEqual(ready.pause.visible, true);
	assert.strictEqual(ready.credential.visible, true);
	const unknown = S.buildModel('embedding', remote(), undefined, idle);
	assert.strictEqual(unknown.credential.visible, false);
});

test('a management token typed once is remembered in memory and reused without re-entry', async () => {
	const mem = {};
	const sessionKeys = { get: (e) => mem[e] || '', set: (e, v) => { mem[e] = v; }, forget: (e) => { delete mem[e]; } };
	const { backend, refs, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle, idle, idle, idle] }, { sessionKeys });
	await controller.refresh();
	await controller.provision('llm', 'k_secret');
	assert.strictEqual(mem.SOME_API_KEY, 'k_secret');
	await controller.pause('llm', '');
	await controller.provision('embedding', '');
	const keys = backend.posts.map(([, b]) => b.keys && b.keys.SOME_API_KEY);
	assert.deepStrictEqual(keys, ['k_secret', 'k_secret', 'k_secret']);
	assert.match(refs.llm.credInput.placeholder, /Entered earlier/);
});

test('a token typed now replaces the remembered one, and a refused one is forgotten', async () => {
	const mem = { SOME_API_KEY: 'k_old' };
	const sessionKeys = { get: (e) => mem[e] || '', set: (e, v) => { mem[e] = v; }, forget: (e) => { delete mem[e]; } };
	const { backend, controller } = setup({ providers: bothRemote, health: sick, jobs: [idle, idle], postReply: { ok: false, status: 403, data: { detail: 'no' } } }, { sessionKeys });
	await controller.refresh();
	await controller.provision('llm', 'k_new');
	assert.strictEqual(backend.posts[0][1].keys.SOME_API_KEY, 'k_new');
	assert.strictEqual(mem.SOME_API_KEY, undefined);
});
