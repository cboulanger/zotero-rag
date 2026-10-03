// Tests for plugin/src/fix-unavailable-debug.js (DebugReport helper).
// Loaded into a bare vm context — no Zotero dependency.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'fix-unavailable-debug.js');

/** @returns {any} */
function loadDebug() {
	const context = {};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'fix-unavailable-debug.js' });
	return context.ZoteroFixDebug;
}

/** Normalise objects created in the vm context (cross-realm prototypes). */
const plain = (v) => JSON.parse(JSON.stringify(v));

const info = (over = {}) => ({
	attachmentItem: { key: 'ATT1', attachmentContentType: 'application/pdf' },
	parentItem: { key: 'PAR1' },
	title: 'A title',
	...over,
});

test('redactPaths replaces known prefixes (posix + windows) and generic home dirs', () => {
	const d = loadDebug();
	const prefixes = [{ path: '/data/zotero', label: '<zotero-data>' }, { path: 'C:\\Users\\bob\\Zotero', label: '<zotero-data>' }];
	assert.strictEqual(d.redactPaths('open /data/zotero/storage/X/a.pdf failed', prefixes), 'open <zotero-data>/storage/X/a.pdf failed');
	assert.strictEqual(d.redactPaths('C:\\Users\\bob\\Zotero\\storage\\a.pdf', prefixes), '<zotero-data>\\storage\\a.pdf');
	assert.strictEqual(d.redactPaths('/Users/alice/Library/x', []), '~/Library/x');
	assert.strictEqual(d.redactPaths('/home/carol/x', []), '~/x');
});

test('finalize drops sensitive keys and scrubs secret-looking strings', () => {
	const d = loadDebug();
	const report = d.createReport({ selectionCount: 1, totalRows: 1 });
	const item = report.startItem(info());
	item.addStep('timeout_retry').finish('error', {
		headers: { Authorization: 'Bearer abc', 'X-Kisski-Api-Key': 'kk', ok: 'fine' },
		message: 'failed with api_key=SEKRET at /home/dave/file.pdf',
	});
	const json = JSON.stringify(report.finalize({}));
	for (const leaked of ['Bearer abc', '"kk"', 'SEKRET', 'dave']) assert.ok(!json.includes(leaked), `leaked ${leaked}`);
	assert.ok(json.includes('fine'));
});

test('report has the documented schema and every row has at least one step when skipped', () => {
	const d = loadDebug();
	const report = d.createReport({
		plugin: { version: '1.0', zoteroVersion: '8', platform: 'linux' },
		backend: { urlHost: 'rag.example.com', isLocal: false },
		library: { backend_library_id: 'groups/1' },
		selectionCount: 2, totalRows: 5,
	});
	const a = report.startItem(info({ isLinked: true }), { typeLabel: 'linked' });
	a.skip('skipped_linked_file', 'linked');
	a.setFinalStatus('not-found', 'Linked file');
	const b = report.startItem(info({ skipReason: 'timeout' }));
	const step = b.addStep('timeout_retry');
	step.note('x', { n: 1 });
	step.finish('still_timed_out', { timeout_multiplier: 2 }, { request_id: 'r1' });
	const out = report.finalize({ fixed: 0, not_found: 2, errors: 0 });

	assert.strictEqual(out.schema_version, 1);
	assert.strictEqual(out.tool, 'fix-unavailable');
	assert.strictEqual(out.backend.url_host, 'rag.example.com');
	assert.deepStrictEqual(plain(out.selection), { count: 2, dialog_total_rows: 5 });
	assert.strictEqual(out.items.length, 2);
	for (const it of out.items) assert.ok(it.steps.length >= 1);
	assert.strictEqual(out.items[0].steps[0].outcome, 'skipped');
	assert.strictEqual(out.items[1].steps[0].backend.request_id, 'r1');
	assert.deepStrictEqual(plain(out.items[1].steps[0].plugin.trail), [{ name: 'x', data: { n: 1 } }]);
	assert.strictEqual(out.items[1].initial_state.skip_reason, 'timeout');
});

test('overlong strings are truncated', () => {
	const d = loadDebug();
	const report = d.createReport({ selectionCount: 1, totalRows: 1 });
	report.startItem(info()).addStep('x').finish('error', { big: 'a'.repeat(20000) });
	const out = report.finalize({});
	assert.ok(out.items[0].steps[0].plugin.big.length < 9000);
	assert.ok(out.items[0].steps[0].plugin.big.includes('truncated'));
});

test('fileName sanitises the library id and is stable', () => {
	const d = loadDebug();
	assert.strictEqual(
		d.fileName('groups/6297749', new Date(Date.UTC(2026, 9, 3, 12, 34, 56))),
		'zotero-rag-fix-debug-groups-6297749-20261003-123456.json'
	);
});
