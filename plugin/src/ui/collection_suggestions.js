// @ts-check

/**
 * Filing suggestions item pane section for Zotero RAG.
 *
 * Registers a custom item pane section via Zotero.ItemPaneManager.registerSection()
 * that queries the backend for collection filing suggestions for the selected item.
 *
 * Manual verification:
 * - Select a regular item in Zotero that has been indexed by zotero-rag
 * - The "Filing Suggestions" pane should appear in the item details panel
 * - Each suggestion row shows collection name, similarity score, Copy and Move buttons
 * - Hovering over a row reveals the action buttons
 * - Copy adds the item to the suggested collection (item stays in current collections)
 * - Move adds the item to the suggested collection and removes it from all existing collections
 * - If no suggestions are available, an empty-state message is shown
 */

var PANE_ID = "zotero-rag-filing-suggestions";
var PLUGIN_ID = "zotero-rag@cboulanger.github.io";

/**
 * Derive the backend library ID for a given Zotero item.
 * Group libraries use the numeric group ID; the user library uses "u<userId>".
 *
 * @param {any} item - Zotero item
 * @returns {string} Backend library identifier
 */
function _getBackendLibraryId(item) {
    const lib = Zotero.Libraries.get(item.libraryID);
    if (lib && lib.libraryType === 'group') {
        const group = Zotero.Groups.getByLibraryID(item.libraryID);
        return group ? String(group.id) : String(item.libraryID);
    }
    const userId = Zotero.Users.getCurrentUserID();
    return userId ? `u${userId}` : String(item.libraryID);
}

/**
 * Inject the suggestion-row stylesheet once per document.
 *
 * @param {Document} doc
 */
function _injectStyles(doc) {
    if (doc.getElementById("rag-suggestions-style")) return;
    const style = doc.createElement("style");
    style.id = "rag-suggestions-style";
    style.textContent = `
        .rag-suggestions-headline { font-size: 0.9em; font-weight: bold; margin: 4px 4px 2px; opacity: 0.8; }
        .rag-suggestions-table { width: 100%; border-collapse: collapse; }
        .rag-suggestion-row td { padding: 2px 4px; vertical-align: middle; }
        .rag-path-cell { width: 100%; }
        .rag-path-inner { display: flex; align-items: center; gap: 4px; }
        .rag-path-label { cursor: pointer; flex: 1; }
        .rag-path-label:hover { text-decoration: underline; }
        .rag-score { font-size: 0.85em; opacity: 0.7; white-space: nowrap; }
        .rag-actions-cell { white-space: nowrap; }
        .rag-actions { visibility: hidden; display: flex; gap: 4px; align-items: center; }
        .rag-suggestion-row:hover .rag-actions { visibility: visible; }
        .rag-actions button { font-size: 0.8em; padding: 1px 6px; }
    `;
    (doc.head || doc.documentElement).appendChild(style);
}

/**
 * Convert a backend library ID string (e.g. "u12345" or "6297749") to Zotero's
 * internal numeric libraryID.  Returns null when the library can't be resolved.
 *
 * @param {string} backendLibraryId
 * @returns {number|null}
 */
function _resolveZoteroLibraryID(backendLibraryId) {
    if (backendLibraryId.startsWith('u')) {
        return Zotero.Libraries.userLibraryID;
    }
    const groupId = parseInt(backendLibraryId, 10);
    if (!isNaN(groupId)) {
        const group = Zotero.Groups.get(groupId);
        return group ? group.libraryID : null;
    }
    return null;
}

/**
 * Build the full breadcrumb path for a collection: "Library / Parent / Collection".
 * Falls back to `fallbackName` if the collection can't be resolved in the local Zotero DB.
 *
 * @param {string} backendLibraryId - Backend library ID from the suggestion
 * @param {string} collectionKey - Zotero collection key
 * @param {string} fallbackName - Name to use when the collection isn't found locally
 * @returns {string}
 */
function _buildCollectionPath(backendLibraryId, collectionKey, fallbackName) {
    const zoteroLibraryID = _resolveZoteroLibraryID(backendLibraryId);
    if (zoteroLibraryID === null) return fallbackName || collectionKey;
    const parts = [];
    let col = Zotero.Collections.getByLibraryAndKey(zoteroLibraryID, collectionKey);
    if (!col) return fallbackName || collectionKey;
    while (col) {
        parts.unshift(col.name);
        if (!col.parentKey) break;
        col = Zotero.Collections.getByLibraryAndKey(zoteroLibraryID, col.parentKey);
    }
    const lib = Zotero.Libraries.get(zoteroLibraryID);
    if (lib) parts.unshift(lib.name);
    return parts.join(' / ');
}

/**
 * Navigate the Zotero UI to the specified collection in the collections tree.
 *
 * @param {string} backendLibraryId
 * @param {string} collectionKey
 * @returns {Promise<void>}
 */
async function _selectCollection(backendLibraryId, collectionKey) {
    const zoteroLibraryID = _resolveZoteroLibraryID(backendLibraryId);
    if (zoteroLibraryID === null) return;
    const col = Zotero.Collections.getByLibraryAndKey(zoteroLibraryID, collectionKey);
    if (!col) return;
    const zp = Zotero.getActiveZoteroPane();
    const cv = zp && zp.collectionsView;
    if (cv && cv.selectCollection) {
        await cv.selectCollection(col.id);
    }
}

/**
 * Build a single suggestion row as a table row element.
 *
 * @param {import('../api/collections.js').CollectionSuggestion} suggestion
 * @param {any} item - Zotero item
 * @param {Document} doc
 * @returns {HTMLTableRowElement}
 */
function _buildSuggestionRow(suggestion, item, doc) {
    const row = /** @type {HTMLTableRowElement} */ (doc.createElement("tr"));
    row.className = "rag-suggestion-row";
    row.dataset.collectionKey = suggestion.collection_id;

    // --- Path cell ---
    const pathCell = doc.createElement("td");
    pathCell.className = "rag-path-cell";

    const pathInner = doc.createElement("div");
    pathInner.className = "rag-path-inner";

    const icon = doc.createElement("span");
    icon.className = "icon icon-css icon-collection";

    const label = doc.createElement("span");
    label.className = "rag-path-label";
    label.textContent = _buildCollectionPath(suggestion.library_id, suggestion.collection_id, suggestion.collection_name);
    label.title = label.textContent;
    label.addEventListener("click", () => _selectCollection(suggestion.library_id, suggestion.collection_id));

    const score = doc.createElement("span");
    score.className = "rag-score";
    score.textContent = `${Math.round(suggestion.score * 100)}%`;

    pathInner.append(icon, label, score);
    pathCell.appendChild(pathInner);

    // --- Actions cell ---
    const actionsCell = doc.createElement("td");
    actionsCell.className = "rag-actions-cell";

    const actions = doc.createElement("div");
    actions.className = "rag-actions";

    const isSameLibrary = _resolveZoteroLibraryID(suggestion.library_id) === item.libraryID;

    const copyBtn = doc.createElement("button");
    copyBtn.textContent = "Copy";
    copyBtn.onclick = async () => {
        await _copyItemToCollection(item, suggestion.library_id, suggestion.collection_id);
        row.remove();
    };

    const moveBtn = doc.createElement("button");
    moveBtn.textContent = "Move";
    moveBtn.onclick = async () => {
        await _moveItemToCollection(item, suggestion.library_id, suggestion.collection_id);
        row.remove();
    };
    // Move across libraries is not possible (foreign-key constraint);
    // use visibility:hidden so the cell always reserves the same width
    if (!isSameLibrary) {
        moveBtn.style.visibility = "hidden";
        moveBtn.style.pointerEvents = "none";
    }

    actions.append(copyBtn, moveBtn);
    actionsCell.appendChild(actions);
    row.append(pathCell, actionsCell);
    return row;
}

/**
 * Add the item to a collection without removing it from existing collections.
 *
 * @param {any} item - Zotero item
 * @param {string} backendLibraryId - Backend library ID of the target collection
 * @param {string} collectionKey - Zotero collection key (e.g. "ABC12345")
 * @returns {Promise<void>}
 */
async function _copyItemToCollection(item, backendLibraryId, collectionKey) {
    const zoteroLibraryID = _resolveZoteroLibraryID(backendLibraryId);
    if (zoteroLibraryID === null) return;
    const col = Zotero.Collections.getByLibraryAndKey(zoteroLibraryID, collectionKey);
    if (!col) return;
    if (item.libraryID === zoteroLibraryID) {
        item.addToCollection(col.id);
        await item.saveTx();
    } else {
        // Cross-library: clone the item's metadata into the target library
        const newItem = item.clone(zoteroLibraryID);
        newItem.addToCollection(col.id);
        await newItem.saveTx();
    }
}

/**
 * Move the item to a collection, removing it from all existing collections first.
 *
 * @param {any} item - Zotero item
 * @param {string} backendLibraryId - Backend library ID of the target collection
 * @param {string} collectionKey - Zotero collection key (e.g. "ABC12345")
 * @returns {Promise<void>}
 */
async function _moveItemToCollection(item, backendLibraryId, collectionKey) {
    const zoteroLibraryID = _resolveZoteroLibraryID(backendLibraryId);
    if (zoteroLibraryID === null) return;
    const col = Zotero.Collections.getByLibraryAndKey(zoteroLibraryID, collectionKey);
    if (!col) return;
    // Capture existing collection IDs before modifying
    const currentCollections = item.getCollections(); // returns array of internal IDs
    item.addToCollection(col.id);
    for (const existingId of currentCollections) {
        if (existingId !== col.id) {  // don't remove the collection we just added
            item.removeFromCollection(existingId);
        }
    }
    await item.saveTx();
}

/**
 * Register the filing suggestions item pane section.
 * Called from bootstrap.js after ZoteroRAG.main() completes.
 */
function registerFilingSuggestionsPane() {
    const result = Zotero.ItemPaneManager.registerSection({
        paneID: PANE_ID,
        pluginID: PLUGIN_ID,
        header: {
            l10nID: "pane-filing-suggestions",
            icon: "chrome://zotero/skin/16/universal/copy-collection.svg",
        },
        sidenav: {
            l10nID: "pane-filing-suggestions",
            icon: "chrome://zotero/skin/20/universal/add-collection.svg",
        },

        // onRender is required by Zotero.ItemPaneManager; async work goes in onAsyncRender.
        onRender: () => {},

        /**
         * @param {{ body: HTMLElement, item: any, doc: Document }} renderCtx
         */
        onAsyncRender: async ({ body, item, doc }) => {
            body.innerHTML = "";

            // BEGIN DEBUG
            console.log("[FilingSuggestions] onAsyncRender called " + JSON.stringify({
                hasItem: !!item,
                isAttachment: item?.isAttachment?.(),
                isNote: item?.isNote?.(),
                hasRAG: !!/** @type {any} */ (Zotero).ZoteroRAG,
                backendURL: /** @type {any} */ (Zotero).ZoteroRAG?.backendURL,
                hasCollectionsAPI: typeof CollectionsAPI !== 'undefined',
                itemKey: item?.key,
            }));
            // END DEBUG

            // Only show for regular (non-attachment, non-note) items
            if (!item || item.isAttachment() || item.isNote()) {
                console.log("[FilingSuggestions] skipping: attachment or note"); // DEBUG
                return;
            }

            const rag = Zotero.ZoteroRAG;
            if (!rag?.backendURL) {
                console.log("[FilingSuggestions] skipping: no backendURL"); // DEBUG
                return;
            }

            const libraryId = _getBackendLibraryId(item);
            console.log("[FilingSuggestions] fetching suggestions for", item.key, "library", libraryId); // DEBUG

            let suggestions;
            try {
                suggestions = await CollectionsAPI.suggestCollections(
                    rag.backendURL,
                    libraryId,
                    item.key,
                    (...args) => rag.getAuthHeaders(...args),
                    5,
                );
                console.log("[FilingSuggestions] suggestions:", JSON.stringify(suggestions)); // DEBUG
            } catch (err) {
                console.error("[FilingSuggestions] fetch error:", err); // DEBUG
                // Show an inline error rather than crashing the pane
                const errDiv = doc.createElement("div");
                errDiv.className = "rag-suggestions-empty";
                errDiv.textContent = `Error loading suggestions: ${err instanceof Error ? err.message : String(err)}`;
                body.appendChild(errDiv);
                return;
            }

            // Inject styles once per document (idempotent)
            _injectStyles(doc);

            // Filter out collections the item is already in (same-library only)
            const itemBackendLibraryId = _getBackendLibraryId(item);
            const currentCollectionKeys = new Set(
                item.getCollections()
                    .map((/** @type {number} */ id) => Zotero.Collections.get(id)?.key)
                    .filter(/** @param {string|undefined} k */ k => !!k)
            );
            const filteredSuggestions = suggestions.filter(
                (/** @type {any} */ s) =>
                    s.library_id !== itemBackendLibraryId || !currentCollectionKeys.has(s.collection_id)
            );

            if (!filteredSuggestions.length) {
                const empty = doc.createElement("div");
                empty.className = "rag-suggestions-empty";
                empty.textContent = "No suggestions yet. Index this item first.";
                body.appendChild(empty);
                return;
            }

            const headline = doc.createElement("div");
            headline.className = "rag-suggestions-headline";
            headline.textContent = "Suggested collections";
            body.appendChild(headline);

            const table = doc.createElement("table");
            table.className = "rag-suggestions-table";
            const tbody = doc.createElement("tbody");
            for (const suggestion of filteredSuggestions) {
                tbody.appendChild(_buildSuggestionRow(suggestion, item, doc));
            }
            table.appendChild(tbody);
            body.appendChild(table);
        },
    });
    console.log("[FilingSuggestions] registerSection result:", result); // DEBUG
}

/**
 * Unregister the filing suggestions item pane section.
 * Called from bootstrap.js during shutdown.
 */
function unregisterFilingSuggestionsPane() {
    Zotero.ItemPaneManager.unregisterSection(PANE_ID);
}
