// Tests for plugin/src/indexed-tags.js — emoji "indexed" tag maintenance.
//
// Same technique as plugin/test/mentions.test.js: evaluate the source in a vm
// context with stubbed Zotero/fetch globals and pull `IndexedTags` back out.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'indexed-tags.js');
const TAG = '\u{1F4C7} rag-indexed';

// Objects created inside the vm context have a different Object.prototype, so
// compare through a JSON round trip instead of deepStrictEqual's prototype check.
const deepEq = (/** @type {any} */ actual, /** @type {any} */ expected) =>
	assert.deepStrictEqual(JSON.parse(JSON.stringify(actual)), expected);

/**
 * A fake Zotero attachment that records writes.
 * @param {string} key
 * @param {{tagged?: boolean, attachment?: boolean, libraryID?: number}} [o]
 */
function makeItem(key, { tagged = false, attachment = true, libraryID = 1 } = {}) {
	const tags = new Set(tagged ? [TAG] : []);
	return {
		key,
		libraryID,
		saves: /** @type {any[]} */ ([]),
		added: /** @type {any[]} */ ([]),
		isAttachment: () => attachment,
		hasTag: (/** @type {string} */ t) => tags.has(t),
		addTag(/** @type {string} */ t, /** @type {number} */ type) { tags.add(t); this.added.push([t, type]); },
		removeTag(/** @type {string} */ t) { tags.delete(t); },
		async saveTx(/** @type {any} */ opts) { this.saves.push(opts); },
	};
}

/**
 * @param {{items?: any[], indexedOnBackend?: string[], editable?: boolean, prefs?: Record<string, any>, responses?: Record<string, any>}} [o]
 */
function setup({ items = [], indexedOnBackend = [], editable = true, prefs = {}, responses = {} } = {}) {
	const byKey = new Map(items.map(i => [i.key, i]));
	const stored = { ...prefs };
	/** @type {Array<{url: string, init: any}>} */
	const calls = [];
	const zotero = {
		Prefs: { get: (/** @type {string} */ k) => stored[k], set: (/** @type {string} */ k, /** @type {any} */ v) => { stored[k] = v; } },
		Libraries: { userLibraryID: 1, get: () => ({ editable }) },
		Groups: { get: (/** @type {number} */ id) => (id === 77 ? { libraryID: 5 } : null) },
		Items: { getByLibraryAndKey: (/** @type {number} */ _l, /** @type {string} */ k) => byKey.get(k) || false, get: (/** @type {number[]} */ ids) => ids.map(i => items[i]) },
		Tags: { getID: () => 9, getTagItems: async () => items.map((_, i) => i) },
	};
	const fetchStub = async (/** @type {string} */ url, /** @type {any} */ init = {}) => {
		calls.push({ url, init });
		let body;
		if (url.includes('/api/indexed-tags/check')) {
			const req = JSON.parse(init.body);
			body = { indexed: req.attachment_keys.filter((/** @type {string} */ k) => indexedOnBackend.includes(k)) };
		} else {
			const key = Object.keys(responses).find(k => url.includes(k));
			body = key ? (typeof responses[key] === 'function' ? responses[key](url) : responses[key]) : {};
		}
		return { ok: true, status: 200, json: async () => body };
	};
	const context = { Zotero: zotero, fetch: fetchStub, console, setTimeout, clearTimeout };
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'indexed-tags.js' });
	const IndexedTags = vm.runInContext('IndexedTags', context);
	IndexedTags.plugin = {
		backendURL: 'http://backend',
		getAuthHeaders: (/** @type {any} */ extra) => ({ ...extra }),
		getCurrentZoteroUserId: () => 42,
		getBackendLibraryId: (/** @type {number} */ id) => (id === 1 ? 'u42' : '77'),
		log: () => {},
	};
	return { T: IndexedTags, calls, stored };
}

test('setTagged adds an automatic tag once and is idempotent', async () => {
	const { T } = setup();
	const item = makeItem('A');
	assert.strictEqual(await T.setTagged(item, true), 'changed');
	deepEq(item.added, [[TAG, 1]]);
	deepEq(item.saves, [{ skipDateModifiedUpdate: true }]);
	assert.strictEqual(await T.setTagged(item, true), 'unchanged');
	assert.strictEqual(item.saves.length, 1, 'no second write');
	assert.strictEqual(await T.setTagged(item, false), 'changed');
	assert.strictEqual(await T.setTagged(item, false), 'unchanged');
	assert.strictEqual(item.saves.length, 2);
});

test('setTagged skips non-attachments and read-only libraries', async () => {
	assert.strictEqual(await setup().T.setTagged(makeItem('N', { attachment: false }), true), 'skipped');
	assert.strictEqual(await setup({ editable: false }).T.setTagged(makeItem('A'), true), 'skipped');
});

test('indexed event tags immediately; unindexed is deferred then confirmed against the backend', async () => {
	const item = makeItem('A', { tagged: true });
	const { T, calls } = setup({ items: [item], indexedOnBackend: [] });
	await T.applyEvents([{ seq: 5, type: 'unindexed', library_id: 'u42', attachment_key: 'A' }], 1000);
	assert.strictEqual(item.saves.length, 0, 'not removed yet');
	await T.flushDueRemovals(1000 + T.REMOVE_SETTLE_MS - 1);
	assert.strictEqual(item.saves.length, 0, 'still settling');
	await T.flushDueRemovals(1000 + T.REMOVE_SETTLE_MS);
	assert.strictEqual(item.hasTag(TAG), false);
	assert.ok(calls.some(c => c.url.endsWith('/api/indexed-tags/check')));
});

test('a re-index flap (unindexed then indexed) never removes the tag', async () => {
	const item = makeItem('A', { tagged: true });
	const { T } = setup({ items: [item] });
	await T.applyEvents([{ seq: 1, type: 'unindexed', library_id: 'u42', attachment_key: 'A' }], 0);
	await T.applyEvents([{ seq: 2, type: 'indexed', library_id: 'u42', attachment_key: 'A' }], 10);
	await T.flushDueRemovals(1e9);
	assert.strictEqual(item.hasTag(TAG), true);
	assert.strictEqual(item.saves.length, 0);
});

test('a removal is cancelled when the backend says the attachment is indexed again', async () => {
	const item = makeItem('A', { tagged: true });
	const { T } = setup({ items: [item], indexedOnBackend: ['A'] });
	await T.applyEvents([{ seq: 1, type: 'unindexed', library_id: 'u42', attachment_key: 'A' }], 0);
	await T.flushDueRemovals(1e9);
	assert.strictEqual(item.hasTag(TAG), true);
});

test('events for libraries this profile does not have are ignored', async () => {
	const item = makeItem('A');
	const { T } = setup({ items: [item] });
	await T.applyEvents([
		{ seq: 1, type: 'indexed', library_id: 'u999', attachment_key: 'A' },
		{ seq: 2, type: 'indexed', library_id: '12345', attachment_key: 'A' },
	]);
	assert.strictEqual(item.saves.length, 0);
});

test('group library ids map through Zotero.Groups', async () => {
	const { T } = setup();
	assert.strictEqual(T.slugToLibraryID('groups/77'), 5);
	assert.strictEqual(T.slugToLibraryID('users/42'), 1);
	assert.strictEqual(T.slugToLibraryID('users/7'), null);
	assert.strictEqual(T.slugToLibraryID('groups/1'), null);
});

test('applyOps is idempotent: re-applying the same plan performs no writes', async () => {
	const a = makeItem('A');
	const b = makeItem('B', { tagged: true });
	const { T } = setup({ items: [a, b], indexedOnBackend: [] });
	const ops = [{ op: 'add', attachment_key: 'A' }, { op: 'remove', attachment_key: 'B' }];
	const first = await T.applyOps('users/42', ops, 0);
	deepEq(first, { added: 1, removed: 1, skipped: 0 });
	const second = await T.applyOps('users/42', ops, 0);
	deepEq(second, { added: 0, removed: 0, skipped: 0 });
	assert.strictEqual(a.saves.length + b.saves.length, 2);
});

test('applyOps drops operations older than an already-applied real-time event', async () => {
	const a = makeItem('A');
	const { T } = setup({ items: [a] });
	await T.applyEvents([{ seq: 10, type: 'unindexed', library_id: 'u42', attachment_key: 'A' }]);
	const r = await T.applyOps('users/42', [{ op: 'add', attachment_key: 'A' }], 9);
	deepEq(r, { added: 0, removed: 0, skipped: 1 });
	assert.strictEqual(a.saves.length, 0);
	const fresh = await T.applyOps('users/42', [{ op: 'add', attachment_key: 'A' }], 10);
	assert.strictEqual(fresh.added, 1);
});

test('applyOps refuses to remove a tag from an attachment the backend reports indexed', async () => {
	const a = makeItem('A', { tagged: true });
	const { T } = setup({ items: [a], indexedOnBackend: ['A'] });
	const r = await T.applyOps('users/42', [{ op: 'remove', attachment_key: 'A' }], 0);
	assert.strictEqual(r.removed, 0);
	assert.strictEqual(a.hasTag(TAG), true);
});

test('pollOnce follows from the log head on first contact, then applies and advances the cursor', async () => {
	const item = makeItem('A');
	let step = 0;
	const { T, stored } = setup({
		items: [item],
		prefs: { 'extensions.zotero-rag.indexedTags.enabled': true },
		responses: {
			'/api/indexed-tags/events': (/** @type {string} */ url) => (
				url.includes('since=')
					? { tag: TAG, events: [{ seq: 4, type: 'indexed', library_id: 'u42', attachment_key: 'A' }], last_seq: 4, gap: false }
					: (step++, { tag: TAG, events: [], last_seq: 3, gap: false })
			),
		},
	});
	await T.pollOnce();
	assert.strictEqual(T.cursor, 3);
	assert.strictEqual(item.saves.length, 0, 'history is not replayed');
	await T.pollOnce();
	assert.strictEqual(item.hasTag(TAG), true);
	assert.strictEqual(T.cursor, 4);
	assert.strictEqual(stored['extensions.zotero-rag.indexedTags.cursor'], '4');
});

test('pollOnce does nothing while the feature is disabled', async () => {
	const { T, calls } = setup();
	await T.pollOnce();
	assert.strictEqual(calls.length, 0);
});

test('refresh streams records, applies ops and aggregates stats across libraries', async () => {
	const a = makeItem('A');
	const { T } = setup({
		items: [a],
		responses: {
			'/api/indexed-tags/refresh/': (/** @type {string} */ url) => (url.endsWith('offset=0')
				? { offset: 100, done: false, records: [
					{ type: 'libraries', libraries: ['users/42', 'groups/77'] },
					{ type: 'library_start', library: 'users/42' },
					{ type: 'ops', library: 'users/42', as_of_seq: 0, ops: [{ op: 'add', attachment_key: 'A' }] },
					{ type: 'progress', library: 'users/42', attachments_checked: 3 },
					{ type: 'library_done', library: 'users/42', attachments_checked: 3 },
				] }
				: { offset: 200, done: true, records: [
					{ type: 'library_start', library: 'groups/77' },
					{ type: 'progress', library: 'groups/77', attachments_checked: 2 },
					{ type: 'library_error', library: 'groups/77', error: 'HTTP 403' },
					{ type: 'done' },
				] }),
			'/api/indexed-tags/refresh': { run_id: 'r1', tag: TAG, already_running: false },
		},
	});
	T.REFRESH_POLL_MS = 1;
	/** @type {any[]} */
	const progress = [];
	const stats = await T.refresh({ onProgress: s => progress.push(s) });
	assert.strictEqual(stats.added, 1);
	assert.strictEqual(stats.librariesTotal, 2);
	assert.strictEqual(stats.librariesDone, 2);
	assert.strictEqual(stats.attachmentsChecked, 5); // 3 in users/42 + 2 checked in groups/77 before it failed
	deepEq(stats.errors, ['groups/77: HTTP 403']);
	assert.strictEqual(progress.length, 2);
	assert.strictEqual(a.hasTag(TAG), true);
});

test('refresh surfaces a terminal error record', async () => {
	const { T } = setup({
		responses: {
			'/api/indexed-tags/refresh/': { offset: 1, done: true, records: [{ type: 'error', message: 'boom' }] },
			'/api/indexed-tags/refresh': { run_id: 'r1', tag: TAG },
		},
	});
	await assert.rejects(() => T.refresh(), /boom/);
});
