'use strict';

/**
 * Tests for plugin/src/api/collections.js
 *
 * Uses Node.js built-in test runner. Run with: node --test plugin/test/api_collections.test.js
 *
 * The collections.js module is loaded via vm to simulate the non-module Zotero plugin environment
 * (no import/export). We stub globalThis.fetch before each test.
 */

const { test, describe, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// ---------------------------------------------------------------------------
// Load the module into a shared context that mirrors the plugin environment.
// The file uses no import/export — CollectionsAPI is declared at module scope.
//
// Note: `const` declarations in a vm context are NOT enumerable on the sandbox
// object. We wrap the source so that the CollectionsAPI const is also assigned
// to `sandbox._CollectionsAPI` which IS accessible on the sandbox.
// ---------------------------------------------------------------------------

const moduleSrc = fs.readFileSync(
    path.join(__dirname, '../src/api/collections.js'),
    'utf8'
);

// Append an assignment that exposes CollectionsAPI on the context object.
const wrappedSrc = moduleSrc + '\n_CollectionsAPI = CollectionsAPI;\n';

/**
 * Create a fresh sandbox and evaluate the module source in it.
 * Returns an object with a `CollectionsAPI` property and a `fetch` property
 * that tests can override to mock HTTP requests.
 *
 * @returns {{ CollectionsAPI: object, fetch: Function }}
 */
function loadModule() {
    /** @type {any} */
    const sandbox = {
        // The module calls `fetch(...)` — we expose it as a property so tests
        // can swap it per-test. We initially set it to a no-op so the sandbox
        // context is valid even before tests set their mock.
        fetch: async () => { throw new Error('fetch not mocked'); },
        // Placeholder that the wrapped source writes into.
        _CollectionsAPI: null,
        // Make AbortSignal available (used in syncCollectionVectors tests).
        AbortSignal,
        // Expose console so any accidental logs don't crash.
        console,
    };
    vm.createContext(sandbox);
    vm.runInContext(wrappedSrc, sandbox);
    sandbox.CollectionsAPI = sandbox._CollectionsAPI;
    return sandbox;
}

// ---------------------------------------------------------------------------
// Helper to build a minimal mock Response object.
// ---------------------------------------------------------------------------

/**
 * @param {number} status
 * @param {unknown} body - Will be JSON-serialised.
 * @param {Record<string,string>} [extraHeaders]
 * @returns {Response}
 */
function mockJsonResponse(status, body, extraHeaders = {}) {
    const jsonText = JSON.stringify(body);
    return /** @type {Response} */ ({
        ok: status >= 200 && status < 300,
        status,
        headers: {
            get(name) {
                const lower = name.toLowerCase();
                if (lower === 'content-type') return 'application/json';
                return extraHeaders[lower] || null;
            },
        },
        async json() { return JSON.parse(jsonText); },
        async text() { return jsonText; },
    });
}

/**
 * @param {number} status
 * @param {string} text
 * @returns {Response}
 */
function mockTextResponse(status, text) {
    return /** @type {Response} */ ({
        ok: status >= 200 && status < 300,
        status,
        headers: {
            get(name) {
                if (name.toLowerCase() === 'content-type') return 'text/plain';
                return null;
            },
        },
        async json() { throw new Error('not json'); },
        async text() { return text; },
    });
}

/** @returns {Record<string,string>} */
function getAuthHeaders() {
    return { Authorization: 'Bearer test-token' };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CollectionsAPI', () => {
    /** @type {any} */
    let CollectionsAPI;
    /** @type {ReturnType<typeof loadModule>} */
    let sandbox;

    beforeEach(() => {
        sandbox = loadModule();
        CollectionsAPI = sandbox.CollectionsAPI;
    });

    afterEach(() => {
        // Restore any fetch mock on globalThis (loadModule uses sandbox.fetch, not globalThis.fetch directly,
        // but we patch globalThis.fetch before each loadModule call in some tests so clean up here).
        delete globalThis.fetch;
    });

    // -----------------------------------------------------------------------
    // getCollectionVectorsStatus
    // -----------------------------------------------------------------------

    describe('getCollectionVectorsStatus', () => {
        test('returns parsed status object on 200', async () => {
            /** @type {CollectionVectorsStatus} */
            const expected = {
                library_id: 'lib1',
                item_vectors_count: 42,
                collection_vectors_count: 7,
                computed: true,
            };

            sandbox.fetch = async (url, init) => {
                assert.ok(url.includes('/api/collections/vectors/status'));
                assert.ok(url.includes('library_id=lib1'));
                assert.equal(init.method, 'GET');
                return mockJsonResponse(200, expected);
            };

            const result = await CollectionsAPI.getCollectionVectorsStatus(
                'http://localhost:8000',
                'lib1',
                getAuthHeaders
            );

            // Cross-realm object comparison via JSON to avoid deepStrictEqual prototype issues.
            assert.equal(JSON.stringify(result), JSON.stringify(expected));
        });

        test('throws on HTTP error (e.g. 500)', async () => {
            sandbox.fetch = async () => mockTextResponse(500, 'Internal Server Error');

            await assert.rejects(
                () => CollectionsAPI.getCollectionVectorsStatus('http://localhost:8000', 'lib1', getAuthHeaders),
                (err) => {
                    // err is thrown from the vm context — instanceof Error is cross-realm and fails.
                    assert.ok(err && typeof err.message === 'string', 'expected an Error-like object');
                    assert.ok(err.message.includes('500'));
                    return true;
                }
            );
        });
    });

    // -----------------------------------------------------------------------
    // syncCollectionVectors
    // -----------------------------------------------------------------------

    describe('syncCollectionVectors', () => {
        test('sends correct body and returns stats on 200', async () => {
            /** @type {SyncCollectionVectorsResult} */
            const expected = {
                items_computed: 10,
                items_skipped: 2,
                collections_computed: 3,
                collections_skipped: 1,
            };

            /** @type {string|undefined} */
            let capturedUrl;
            /** @type {RequestInit|undefined} */
            let capturedInit;

            sandbox.fetch = async (url, init) => {
                capturedUrl = url;
                capturedInit = init;
                return mockJsonResponse(200, expected);
            };

            /** @type {Object.<string, string[]>} */
            const collectionMap = { ABCD1234: ['col1', 'col2'], EFGH5678: ['col3'] };
            /** @type {Object.<string, string>} */
            const collectionNames = { col1: 'Physics', col2: 'Chemistry', col3: 'Biology' };

            const result = await CollectionsAPI.syncCollectionVectors(
                'http://localhost:8000',
                'lib1',
                collectionMap,
                collectionNames,
                getAuthHeaders
            );

            assert.equal(JSON.stringify(result), JSON.stringify(expected));
            assert.ok(capturedUrl.includes('/api/collections/vectors/sync'));
            assert.equal(capturedInit.method, 'POST');

            const sentBody = JSON.parse(capturedInit.body);
            assert.equal(JSON.stringify(sentBody), JSON.stringify({
                library_id: 'lib1',
                collection_map: collectionMap,
                collection_names: collectionNames,
            }));
        });

        test('forwards AbortSignal to fetch', async () => {
            sandbox.fetch = async (url, init) => {
                assert.ok(init.signal instanceof AbortSignal);
                return mockJsonResponse(200, { items_computed: 0, items_skipped: 0, collections_computed: 0, collections_skipped: 0 });
            };

            const signal = AbortSignal.timeout(30000);
            await CollectionsAPI.syncCollectionVectors(
                'http://localhost:8000',
                'lib1',
                {},
                {},
                getAuthHeaders,
                signal
            );
        });

        test('throws on HTTP error', async () => {
            sandbox.fetch = async () => mockJsonResponse(422, { detail: 'Validation error' });

            await assert.rejects(
                () => CollectionsAPI.syncCollectionVectors(
                    'http://localhost:8000', 'lib1', {}, {}, getAuthHeaders
                ),
                (err) => {
                    assert.ok(err && typeof err.message === 'string', 'expected an Error-like object');
                    assert.ok(err.message.includes('422'));
                    return true;
                }
            );
        });
    });

    // -----------------------------------------------------------------------
    // suggestCollections
    // -----------------------------------------------------------------------

    describe('suggestCollections', () => {
        test('returns suggestions array on 200', async () => {
            /** @type {Array<CollectionSuggestion>} */
            const expected = [
                { collection_id: 'col1', collection_name: 'Physics', library_id: 'lib1', score: 0.92 },
                { collection_id: 'col2', collection_name: 'Chemistry', library_id: 'lib1', score: 0.87 },
            ];

            sandbox.fetch = async (url, init) => {
                assert.ok(url.includes('/api/collections/suggest'));
                assert.ok(url.includes('library_id=lib1'));
                assert.ok(url.includes('item_key=ABCD1234'));
                assert.ok(url.includes('limit=5'));
                return mockJsonResponse(200, expected);
            };

            const result = await CollectionsAPI.suggestCollections(
                'http://localhost:8000',
                'lib1',
                'ABCD1234',
                getAuthHeaders
            );

            assert.equal(JSON.stringify(result), JSON.stringify(expected));
        });

        test('returns [] when no item vector exists (backend returns 200 + empty array)', async () => {
            sandbox.fetch = async () => mockJsonResponse(200, []);

            const result = await CollectionsAPI.suggestCollections(
                'http://localhost:8000',
                'lib1',
                'MISSING_KEY',
                getAuthHeaders
            );

            assert.equal(result.length, 0, 'expected empty array when no vector exists');
        });

        test('throws on 500 error', async () => {
            sandbox.fetch = async () => mockTextResponse(500, 'Internal Server Error');

            await assert.rejects(
                () => CollectionsAPI.suggestCollections(
                    'http://localhost:8000', 'lib1', 'ABCD1234', getAuthHeaders
                ),
                (err) => {
                    assert.ok(err && typeof err.message === 'string', 'expected an Error-like object');
                    assert.ok(err.message.includes('500'));
                    return true;
                }
            );
        });

        test('throws on 401 error', async () => {
            sandbox.fetch = async () => mockJsonResponse(401, { detail: 'Unauthorized' });

            await assert.rejects(
                () => CollectionsAPI.suggestCollections(
                    'http://localhost:8000', 'lib1', 'ABCD1234', getAuthHeaders
                ),
                (err) => {
                    assert.ok(err && typeof err.message === 'string', 'expected an Error-like object');
                    assert.ok(err.message.includes('401'));
                    return true;
                }
            );
        });

        test('uses default limit of 5 when not specified', async () => {
            sandbox.fetch = async (url) => {
                assert.ok(url.includes('limit=5'), `Expected limit=5 in URL, got: ${url}`);
                return mockJsonResponse(200, []);
            };

            await CollectionsAPI.suggestCollections(
                'http://localhost:8000', 'lib1', 'ABCD1234', getAuthHeaders
            );
        });

        test('uses custom limit when specified', async () => {
            sandbox.fetch = async (url) => {
                assert.ok(url.includes('limit=10'), `Expected limit=10 in URL, got: ${url}`);
                return mockJsonResponse(200, []);
            };

            await CollectionsAPI.suggestCollections(
                'http://localhost:8000', 'lib1', 'ABCD1234', getAuthHeaders, 10
            );
        });
    });
});

/**
 * @typedef {Object} CollectionVectorsStatus
 * @property {string} library_id
 * @property {number} item_vectors_count
 * @property {number} collection_vectors_count
 * @property {boolean} computed
 */

/**
 * @typedef {Object} SyncCollectionVectorsResult
 * @property {number} items_computed
 * @property {number} items_skipped
 * @property {number} collections_computed
 * @property {number} collections_skipped
 */

/**
 * @typedef {Object} CollectionSuggestion
 * @property {string} collection_id
 * @property {string} collection_name
 * @property {string} library_id
 * @property {number} score
 */
