// @ts-check

/**
 * API client for collection vector endpoints.
 * @module api/collections
 */

/**
 * Perform an HTTP request, throwing a descriptive Error on non-2xx responses.
 *
 * @param {string} method - HTTP method (GET, POST, etc.)
 * @param {string} url - Full URL to fetch
 * @param {RequestInit & {statusAllowList?: number[]}} [init] - Fetch init options, plus
 *   an optional `statusAllowList` of HTTP status codes that should be returned as-is
 *   rather than triggering an error throw.
 * @returns {Promise<Response>}
 */
async function _apiFetch(method, url, init = {}) {
    const { statusAllowList, ...fetchInit } = init;
    const response = await fetch(url, { method, ...fetchInit });
    if (!response.ok) {
        // If caller explicitly whitelisted this status code, return it un-thrown.
        if (statusAllowList && statusAllowList.includes(response.status)) {
            return response;
        }
        let detail = '';
        const ct = response.headers.get('content-type') || '';
        if (ct.includes('application/json')) {
            const body = /** @type {{detail?: string}} */ (
                /** @type {unknown} */ (await response.json().catch(() => ({})))
            );
            detail = body.detail || JSON.stringify(body);
        } else {
            const text = await response.text().catch(() => '');
            detail = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 200);
        }
        const path = (() => { try { return new URL(url).pathname; } catch (_) { return url; } })();
        throw new Error(`${method} ${path}: HTTP ${response.status}${detail ? ` — ${detail}` : ''}`);
    }
    return response;
}

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

const CollectionsAPI = {
    /**
     * Get the status of collection vectors for a library.
     *
     * @param {string} backendURL - Base URL of the backend server
     * @param {string} libraryId - Zotero library identifier
     * @param {function(Record<string,string>=): Record<string,string>} getAuthHeaders - Returns auth headers
     * @returns {Promise<CollectionVectorsStatus>}
     */
    async getCollectionVectorsStatus(backendURL, libraryId, getAuthHeaders) {
        const url = `${backendURL}/api/collections/vectors/status?library_id=${encodeURIComponent(libraryId)}`;
        const response = await _apiFetch('GET', url, {
            headers: getAuthHeaders(),
        });
        return /** @type {CollectionVectorsStatus} */ (await response.json());
    },

    /**
     * Sync collection membership vectors to the backend.
     *
     * @param {string} backendURL - Base URL of the backend server
     * @param {string} libraryId - Zotero library identifier
     * @param {Object.<string, string[]>} collectionMap - Maps item_key to list of collection_ids
     * @param {Object.<string, string>} collectionNames - Maps collection_id to collection name
     * @param {function(Record<string,string>=): Record<string,string>} getAuthHeaders - Returns auth headers
     * @param {AbortSignal} [signal] - Optional AbortSignal for cancellation
     * @returns {Promise<SyncCollectionVectorsResult>}
     */
    async syncCollectionVectors(backendURL, libraryId, collectionMap, collectionNames, getAuthHeaders, signal) {
        const url = `${backendURL}/api/collections/vectors/sync`;
        const body = JSON.stringify({
            library_id: libraryId,
            collection_map: collectionMap,
            collection_names: collectionNames,
        });
        /** @type {RequestInit & {statusAllowList?: number[]}} */
        const init = {
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body,
        };
        if (signal !== undefined) {
            init.signal = signal;
        }
        const response = await _apiFetch('POST', url, init);
        return /** @type {SyncCollectionVectorsResult} */ (await response.json());
    },

    /**
     * Get collection suggestions for a given item.
     *
     * Returns an empty array if the item vector does not exist yet (HTTP 404).
     * Throws on all other HTTP errors.
     *
     * @param {string} backendURL - Base URL of the backend server
     * @param {string} libraryId - Zotero library identifier
     * @param {string} itemKey - Zotero item key
     * @param {function(Record<string,string>=): Record<string,string>} getAuthHeaders - Returns auth headers
     * @param {number} [limit=5] - Maximum number of suggestions to return
     * @returns {Promise<Array<CollectionSuggestion>>}
     */
    async suggestCollections(backendURL, libraryId, itemKey, getAuthHeaders, limit = 5) {
        const url = `${backendURL}/api/collections/suggest?library_id=${encodeURIComponent(libraryId)}&item_key=${encodeURIComponent(itemKey)}&limit=${encodeURIComponent(limit)}`;
        const response = await _apiFetch('GET', url, {
            headers: getAuthHeaders(),
            statusAllowList: [404],
        });
        if (response.status === 404) {
            return [];
        }
        return /** @type {Array<CollectionSuggestion>} */ (await response.json());
    },
};
