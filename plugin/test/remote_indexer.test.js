// Tests for plugin/src/remote_indexer.js.
//
// remote_indexer.js is a plain script (not a CommonJS module — it's loaded by
// dialog.xhtml as a <script> tag inside Zotero's chrome environment, where
// `Zotero` is a global). To unit test it in plain Node without touching that
// loading contract, we read the source and evaluate it inside a vm context
// with a stubbed `Zotero` global, then pull the `RemoteIndexer` object (a
// top-level `var`) back out of that context.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'remote_indexer.js');

/**
 * Load RemoteIndexer into a fresh vm context with the given Zotero stub.
 * @param {any} zoteroStub
 * @returns {any} the RemoteIndexer object
 */
function loadRemoteIndexer(zoteroStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'remote_indexer.js' });
	return context.RemoteIndexer;
}

/**
 * Build a minimal Zotero stub whose Zotero.Search() returns a fake search
 * object recording every addCondition(...) call, and whose .search() /
 * Zotero.Items.getAsync() resolve to an empty item list (sufficient for
 * exercising the trash-exclusion call itself).
 * @returns {{ zotero: any, addedConditions: Array<any[]> }}
 */
function makeZoteroStub() {
	const addedConditions = [];
	const fakeSearch = {
		libraryID: null,
		addCondition(...args) { addedConditions.push(args); },
		async search() { return []; },
	};
	const zotero = {
		Groups: { get: () => ({ libraryID: 1 }) },
		Libraries: { userLibraryID: 1 },
		Search: function () { return fakeSearch; },
		Items: { getAsync: async () => [] },
	};
	return { zotero, addedConditions };
}

test('countIndexableAttachments excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer.countIndexableAttachments('123', 'group');

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

test('_collectAttachments excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer._collectAttachments('123', 'group', () => {});

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

test('_collectAbstractItems excludes trashed items from the search', async () => {
	const { zotero, addedConditions } = makeZoteroStub();
	const RemoteIndexer = loadRemoteIndexer(zotero);

	await RemoteIndexer._collectAbstractItems('123', 'group', () => {}, []);

	assert.deepStrictEqual(addedConditions, [['deleted', 'false']]);
});

// ---------------------------------------------------------------------------
// _uploadAttachment: includeDiagnostics
// ---------------------------------------------------------------------------

/** Build a RemoteIndexer whose network layer is stubbed. */
function makeUploader(result, { asyncStatus = 'done' } = {}) {
	const bodies = [];
	const urls = [];
	const zotero = { ZoteroRAG: { _extractAuthors: () => [], _extractYear: () => null } };
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {
		Zotero: zotero,
		IOUtils: { read: async () => new Uint8Array([1, 2, 3]) },
		FormData: class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } },
		Blob: class {},
	};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'remote_indexer.js' });
	const ri = context.RemoteIndexer;
	ri._apiFetch = async (_m, url, opts) => {
		urls.push(url);
		bodies.push(opts.body.f);
		return { status: 200, json: async () => ({ status: asyncStatus, result }) };
	};
	const att = {
		attachment_key: 'A', item_key: 'I', mime_type: 'application/pdf', item_version: 1, attachment_version: 1,
		filePath: '/x.pdf', zoteroItem: {}, parentItem: { getField: () => 't', itemType: 'book', dateModified: 'd' },
	};
	const call = (extra = {}) => ri._uploadAttachment({
		att, libraryId: 'u1', libraryType: 'user', backendURL: 'http://x', userId: 1,
		getAuthHeaders: () => ({}), log: () => {}, ...extra,
	});
	return { call, bodies, urls };
}

/** Build a RemoteIndexer whose network layer is stubbed to respond like the /cache endpoint. */
function makeDeferUploader(cacheResponse) {
	const urls = [];
	const zotero = { ZoteroRAG: { _extractAuthors: () => [], _extractYear: () => null } };
	const context = {
		Zotero: zotero,
		IOUtils: { read: async () => new Uint8Array([1, 2, 3]) },
		FormData: class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } },
		Blob: class {},
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'remote_indexer.js' });
	const ri = context.RemoteIndexer;
	ri._apiFetch = async (_m, url, _opts) => { urls.push(url); return { status: 200, json: async () => ({ status: 'queued', ...cacheResponse }) }; };
	const att = { attachment_key: 'A', item_key: 'I', mime_type: 'application/pdf', item_version: 1, attachment_version: 1,
		filePath: '/x.pdf', zoteroItem: {}, parentItem: { getField: () => 't', itemType: 'book', dateModified: 'd' } };
	const call = (extra = {}) => ri._uploadAttachment({ att, libraryId: 'u1', libraryType: 'user', backendURL: 'http://x', userId: 1, getAuthHeaders: () => ({}), log: () => {}, ...extra });
	return { ri, urls, call };
}

test('_uploadAttachment sends include_diagnostics only when requested', async () => {
	const { call, bodies } = makeUploader({ status: 'indexed', chunks_added: 1, library_id: 'u1' });
	await call();
	await call({ includeDiagnostics: true });
	assert.strictEqual(bodies[0].include_diagnostics, undefined);
	assert.strictEqual(bodies[1].include_diagnostics, 'true');
});

test('_uploadAttachment returns diagnostics + pluginDiag on skipped_timeout', async () => {
	const diag = { request_id: 'r1' };
	const { call } = makeUploader({ status: 'skipped_timeout', chunks_added: 0, diagnostics: diag, error_detail: 'slow' });
	const r = await call({ includeDiagnostics: true, timeoutMultiplier: 2 });
	assert.strictEqual(r.skippedTimeout, true);
	assert.deepStrictEqual(r.diagnostics, diag);
	assert.strictEqual(r.pluginDiag.timeout_multiplier, 2);
	assert.strictEqual(r.pluginDiag.result_status, 'skipped_timeout');
	assert.strictEqual(r.pluginDiag.http_status, 200);
});

test('_uploadAttachment attaches diagnostics to the thrown error for status "error"', async () => {
	const diag = { request_id: 'r2', error: { type: 'RuntimeError' } };
	const { call } = makeUploader({ status: 'error', message: 'boom', chunks_added: 0, diagnostics: diag });
	await assert.rejects(call({ includeDiagnostics: true }), (err) => {
		assert.strictEqual(err.message, 'boom');
		assert.deepStrictEqual(err.diagnostics, diag);
		assert.strictEqual(err.pluginDiag.upload_attempts, 1);
		return true;
	});
});

test('_uploadAttachmentInner includes the attachment\'s own title (not the parent\'s) as attachment_title in the upload metadata', async () => {
	const { call, bodies } = makeUploader({ status: 'done', chunks_added: 1 });
	const att = {
		attachment_key: 'SNAP1', item_key: 'ITEM1', mime_type: 'text/html', item_version: 1, attachment_version: 1,
		filePath: '/fake/path.html',
		zoteroItem: { getField: (f) => (f === 'title' ? 'Snapshot' : '') },
		parentItem: { getField: (f) => (f === 'title' ? 'Parent Title' : ''), itemType: 'webpage', dateModified: 'd' },
	};
	await call({ att });
	const sentMetadata = JSON.parse(bodies[0].metadata);
	assert.strictEqual(sentMetadata.attachment_title, 'Snapshot');
	assert.strictEqual(sentMetadata.title, 'Parent Title');
});

test('_uploadAttachment without includeDiagnostics adds no diagnostics fields', async () => {
	const { call } = makeUploader({ status: 'skipped_empty', chunks_added: 0 });
	const r = await call();
	assert.strictEqual(r.skippedEmpty, true);
	assert.ok(!('diagnostics' in r));
	assert.ok(!('pluginDiag' in r));
});

// ---------------------------------------------------------------------------
// _uploadAttachment: defer mode
// ---------------------------------------------------------------------------

test('_uploadAttachment with defer:true posts to the cache endpoint and returns queued status without polling', async () => {
	const { call, urls } = makeDeferUploader({ eta: '2026-10-06T15:30:00Z', reason: null });
	const result = await call({ defer: true });
	assert.strictEqual(result.queued, true);
	assert.strictEqual(result.eta, '2026-10-06T15:30:00Z');
	assert.strictEqual(result.queueBlockReason, null);
	assert.ok(urls[0].endsWith('/api/index/document/cache'));
});

test('_uploadAttachment with defer:true surfaces a block reason when present', async () => {
	const { call } = makeDeferUploader({ eta: null, reason: 'key_invalid' });
	const result = await call({ defer: true });
	assert.strictEqual(result.eta, null);
	assert.strictEqual(result.queueBlockReason, 'key_invalid');
});

test('_uploadAttachment with defer:false (default) still posts to the async endpoint', async () => {
	const { call, urls } = makeUploader({ status: 'indexed', chunks_added: 1 });
	await call();
	assert.ok(urls[0].endsWith('/api/index/document/async'));
});

test('_processQueuedNow posts to the process-now endpoint and maps a success result', async () => {
	const { ri } = makeDeferUploader({});
	ri._apiFetch = async (_m, url, _opts) => {
		assert.ok(url.endsWith('/api/index/document/cache/u1/ATT1/process-now'));
		return { status: 200, json: async () => ({ status: 'indexed', chunks_added: 3, library_id: 'u1', item_key: 'I', attachment_key: 'ATT1' }) };
	};
	const result = await ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} });
	assert.strictEqual(result.parseError, undefined);
	assert.strictEqual(result.skippedEmpty, undefined);
});

test('_processQueuedNow maps skipped_parse_error the same way as a polled upload', async () => {
	const { ri } = makeDeferUploader({});
	ri._apiFetch = async () => ({ status: 200, json: async () => ({ status: 'skipped_parse_error', error_detail: 'binary data', library_id: 'u1', item_key: 'I', attachment_key: 'ATT1' }) });
	const result = await ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} });
	assert.strictEqual(result.parseError, true);
	assert.strictEqual(result.errorDetail, 'binary data');
});

test('_processQueuedNow throws on an error result, same as a polled upload', async () => {
	const { ri } = makeDeferUploader({});
	ri._apiFetch = async () => ({ status: 200, json: async () => ({ status: 'error', message: 'boom', library_id: 'u1', item_key: 'I', attachment_key: 'ATT1' }) });
	await assert.rejects(
		() => ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} }),
		/boom/
	);
});

test('_processQueuedNow propagates a real 404 from _apiFetch instead of swallowing it', async () => {
	// _apiFetch itself throws on any non-ok response (including 404) before ever
	// returning a response object to the caller — so _processQueuedNow must not
	// (and, after the fix, does not) try to special-case response.status === 404
	// on the resolved value. Stub _apiFetch the way the real one behaves on a
	// 404: by throwing, not by resolving to a {status: 404, ...} object.
	const { ri } = makeDeferUploader({});
	ri._apiFetch = async () => {
		throw new Error('POST /api/index/document/cache/u1/ATT1/process-now: HTTP 404 — No cached upload found for this attachment');
	};
	await assert.rejects(
		() => ri._processQueuedNow({ libraryId: 'u1', attachmentKey: 'ATT1', backendURL: 'http://x', getAuthHeaders: () => ({}), log: () => {} }),
		/HTTP 404/
	);
});
