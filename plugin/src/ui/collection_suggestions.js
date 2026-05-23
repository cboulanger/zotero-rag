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

const PANE_ID = "zotero-rag-filing-suggestions";
const PLUGIN_ID = "zotero-rag@cboulanger.github.io";

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
        .rag-suggestion-row { display: flex; align-items: center; padding: 2px 4px; }
        .rag-suggestion-row .box { flex: 1; display: flex; align-items: center; gap: 4px; }
        .rag-suggestion-row .rag-score { font-size: 0.85em; opacity: 0.7; margin-left: 4px; }
        .rag-suggestion-row .rag-actions { display: none; gap: 4px; }
        .rag-suggestion-row:hover .rag-actions { display: flex; }
        .rag-suggestion-row .rag-actions button { font-size: 0.8em; padding: 1px 6px; }
    `;
    doc.head.appendChild(style);
}

/**
 * Build a single suggestion row element.
 *
 * @param {import('../api/collections.js').CollectionSuggestion} suggestion
 * @param {any} item - Zotero item
 * @param {Document} doc
 * @returns {HTMLElement}
 */
function _buildSuggestionRow(suggestion, item, doc) {
    const row = /** @type {HTMLElement} */ (doc.createElement("div"));
    row.className = "rag-suggestion-row";
    row.dataset.collectionKey = suggestion.collection_id;

    const box = doc.createElement("div");
    box.className = "box";

    const icon = doc.createElement("span");
    icon.className = "icon icon-css icon-collection";

    const label = doc.createElement("span");
    label.className = "label";
    label.textContent = suggestion.collection_name || suggestion.collection_id;

    const score = doc.createElement("span");
    score.className = "rag-score";
    score.textContent = `${Math.round(suggestion.score * 100)}%`;

    box.append(icon, label, score);

    const actions = doc.createElement("div");
    actions.className = "rag-actions";

    const copyBtn = doc.createElement("button");
    copyBtn.textContent = "Copy";
    copyBtn.onclick = async () => {
        await _copyItemToCollection(item, suggestion.collection_id);
        row.remove();
    };

    const moveBtn = doc.createElement("button");
    moveBtn.textContent = "Move";
    moveBtn.onclick = async () => {
        await _moveItemToCollection(item, suggestion.collection_id);
        row.remove();
    };

    actions.append(copyBtn, moveBtn);
    row.append(box, actions);
    return row;
}

/**
 * Add the item to a collection without removing it from existing collections.
 *
 * @param {any} item - Zotero item
 * @param {string} collectionKey - Zotero collection key (e.g. "ABC12345")
 * @returns {Promise<void>}
 */
async function _copyItemToCollection(item, collectionKey) {
    const col = Zotero.Collections.getByLibraryAndKey(item.libraryID, collectionKey);
    if (!col) return;
    item.addToCollection(col.id);
    await item.saveTx();
}

/**
 * Move the item to a collection, removing it from all existing collections first.
 *
 * @param {any} item - Zotero item
 * @param {string} collectionKey - Zotero collection key (e.g. "ABC12345")
 * @returns {Promise<void>}
 */
async function _moveItemToCollection(item, collectionKey) {
    const col = Zotero.Collections.getByLibraryAndKey(item.libraryID, collectionKey);
    if (!col) return;
    // Capture existing collection IDs before modifying
    const currentCollections = item.getCollections(); // returns array of internal IDs
    item.addToCollection(col.id);
    for (const existingId of currentCollections) {
        item.removeFromCollection(existingId);
    }
    await item.saveTx();
}

/**
 * Register the filing suggestions item pane section.
 * Called from bootstrap.js after ZoteroRAG.main() completes.
 */
function registerFilingSuggestionsPane() {
    Zotero.ItemPaneManager.registerSection({
        paneID: PANE_ID,
        pluginID: PLUGIN_ID,
        header: {
            l10nID: "pane-filing-suggestions",
        },
        sidenav: {
            l10nID: "pane-filing-suggestions",
        },

        /**
         * @param {{ body: HTMLElement, item: any, doc: Document }} renderCtx
         */
        onAsyncRender: async ({ body, item, doc }) => {
            body.innerHTML = "";

            // Only show for regular (non-attachment, non-note) items
            if (!item || item.isAttachment() || item.isNote()) {
                return;
            }

            const rag = Zotero.ZoteroRAG;
            if (!rag?.backendURL) return;

            const libraryId = _getBackendLibraryId(item);

            let suggestions;
            try {
                suggestions = await CollectionsAPI.suggestCollections(
                    rag.backendURL,
                    libraryId,
                    item.key,
                    (...args) => rag.getAuthHeaders(...args),
                    5,
                );
            } catch (err) {
                // Show an inline error rather than crashing the pane
                const errDiv = doc.createElement("div");
                errDiv.className = "rag-suggestions-empty";
                errDiv.textContent = `Error loading suggestions: ${err instanceof Error ? err.message : String(err)}`;
                body.appendChild(errDiv);
                return;
            }

            // Inject styles once per document (idempotent)
            _injectStyles(doc);

            if (!suggestions.length) {
                const empty = doc.createElement("div");
                empty.className = "rag-suggestions-empty";
                empty.setAttribute("data-l10n-id", "pane-filing-suggestions-empty");
                body.appendChild(empty);
                return;
            }

            for (const suggestion of suggestions) {
                body.appendChild(_buildSuggestionRow(suggestion, item, doc));
            }
        },
    });
}

/**
 * Unregister the filing suggestions item pane section.
 * Called from bootstrap.js during shutdown.
 */
function unregisterFilingSuggestionsPane() {
    Zotero.ItemPaneManager.unregisterSection(PANE_ID);
}
