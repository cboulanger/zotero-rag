// Tests for plugin/src/provider-sections.js (preset chooser helpers shared by Settings and the wizard).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = path.join(__dirname, '..', 'src');

function load() {
	const context = { console };
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(path.join(SRC, 'provider-sections.js'), 'utf8'), context, { filename: 'provider-sections.js' });
	return context.ZoteroRAGProviderSections;
}

test('the plugin sources name no provider (the backend descriptors carry all vendor knowledge)', () => {
	const files = fs.readdirSync(SRC).filter((f) => /\.(js|xhtml)$/.test(f) && !f.startsWith('toolkit'));
	for (const f of files) {
		const text = fs.readFileSync(path.join(SRC, f), 'utf8');
		assert.doesNotMatch(text, /runpod|kisski|mpcdf|hugging ?face|openai|anthropic/i, f);
	}
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
