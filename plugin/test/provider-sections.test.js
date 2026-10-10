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
	const running = { status: 'running', progress: [], sides: {} };
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
