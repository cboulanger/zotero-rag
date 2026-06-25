// @ts-check

/**
 * Item navigation history for Zotero RAG.
 *
 * Injects Back / Forward buttons into the item pane as a persistent footer toolbar.
 * Tracks item selection history so the user can return to a previously viewed item
 * after clicking a collection link in the Filing Suggestions pane (or anywhere else).
 *
 * This module is self-contained and independent of the rest of the plugin.
 * It registers a permanently-hidden section solely to receive the onItemChange callback.
 * The nav bar is injected into each main window via the onInit hook.
 *
 * Manual verification:
 * - "← Back" and "→ Forward" footer buttons appear at the bottom of the item pane
 * - Both buttons are disabled initially (no history)
 * - Selecting a new item enables Back; Forward remains disabled
 * - Clicking Back navigates to the previous item; Forward is now enabled
 * - Clicking a collection path in Filing Suggestions selects that collection;
 *   pressing Back restores the item view with the Filing Suggestions pane intact
 */

var _NAV_PANE_ID = "zotero-rag-item-navigation";
var _NAV_PLUGIN_ID = "zotero-rag@cboulanger.github.io";

/** @type {number[]} Item ID history stack */
var _navHistory = [];
/** @type {number} Cursor into _navHistory; -1 = empty */
var _navCursor = -1;
/**
 * Suppresses history recording for the single onItemChange that fires
 * in response to our own Back/Forward navigation.
 * @type {number|null}
 */
var _navExpectedItemId = null;

// ---------------------------------------------------------------------------
// History management
// ---------------------------------------------------------------------------

/**
 * Record an item-change event.  Called from the hidden section's onItemChange hook.
 *
 * @param {any} item - Zotero item (may be null when pane is empty)
 */
function _navRecordItem(item) {
    if (!item || !item.id) {
        _navUpdateAllButtons();
        return;
    }

    // If this change is the result of our own navigation, skip it.
    if (_navExpectedItemId !== null && _navExpectedItemId === item.id) {
        _navExpectedItemId = null;
        _navUpdateAllButtons();
        return;
    }
    _navExpectedItemId = null;

    // Truncate any forward history and push the new item.
    _navHistory = _navHistory.slice(0, _navCursor + 1);
    if (_navHistory.length === 0 || _navHistory[_navCursor] !== item.id) {
        _navHistory.push(item.id);
        _navCursor = _navHistory.length - 1;
    }

    _navUpdateAllButtons();
}

/**
 * Refresh enabled state of Back/Forward buttons in every open main window.
 */
function _navUpdateAllButtons() {
    const canBack = _navCursor > 0;
    const canFwd = _navCursor < _navHistory.length - 1;
    for (const win of Zotero.getMainWindows()) {
        const back = win.document.getElementById("rag-nav-back");
        const fwd = win.document.getElementById("rag-nav-forward");
        if (back) back.disabled = !canBack;
        if (fwd) fwd.disabled = !canFwd;
    }
}

/**
 * Navigate backwards through item history.
 * @returns {Promise<void>}
 */
async function _navGoBack() {
    if (_navCursor <= 0) return;
    _navCursor--;
    _navExpectedItemId = _navHistory[_navCursor];
    const zp = Zotero.getActiveZoteroPane();
    if (zp) await zp.selectItem(_navHistory[_navCursor], { inLibraryRoot: true });
    _navUpdateAllButtons();
}

/**
 * Navigate forwards through item history.
 * @returns {Promise<void>}
 */
async function _navGoForward() {
    if (_navCursor >= _navHistory.length - 1) return;
    _navCursor++;
    _navExpectedItemId = _navHistory[_navCursor];
    const zp = Zotero.getActiveZoteroPane();
    if (zp) await zp.selectItem(_navHistory[_navCursor], { inLibraryRoot: true });
    _navUpdateAllButtons();
}

// ---------------------------------------------------------------------------
// DOM injection
// ---------------------------------------------------------------------------

/**
 * Inject the navigation footer into the item pane of the given document.
 * Idempotent: does nothing if the bar is already present.
 *
 * The bar is inserted after #zotero-view-item (the scrollable sections container)
 * inside .zotero-view-item-main, which is a flex column.  The bar therefore
 * appears as a fixed-height footer below the scrollable content.
 *
 * @param {Document} doc
 */
function _navInjectBar(doc) {
    if (doc.getElementById("rag-nav-bar")) return;

    const viewItem = doc.getElementById("zotero-view-item");
    if (!viewItem) return;

    // Styles — injected once per document
    if (!doc.getElementById("rag-nav-style")) {
        const style = doc.createElement("style");
        style.id = "rag-nav-style";
        style.textContent = `
            #rag-nav-bar {
                display: flex;
                gap: 4px;
                padding: 3px 6px;
                border-top: 1px solid var(--material-border, #c8c8c8);
                background: var(--material-sidepane, transparent);
                flex-shrink: 0;
            }
            #rag-nav-bar button {
                font-size: 0.8em;
                padding: 1px 10px;
                min-width: 60px;
            }
        `;
        (doc.head || doc.documentElement).appendChild(style);
    }

    const bar = doc.createElement("div");
    bar.id = "rag-nav-bar";

    const backBtn = doc.createElement("button");
    backBtn.id = "rag-nav-back";
    backBtn.textContent = "← Back";
    backBtn.title = "Go to the previously viewed item";
    backBtn.disabled = true;
    backBtn.onclick = () => _navGoBack();

    const fwdBtn = doc.createElement("button");
    fwdBtn.id = "rag-nav-forward";
    fwdBtn.textContent = "Forward →";
    fwdBtn.title = "Go to the next item in history";
    fwdBtn.disabled = true;
    fwdBtn.onclick = () => _navGoForward();

    bar.append(backBtn, fwdBtn);
    // Insert as footer: after the scrollable content, inside the flex column
    viewItem.insertAdjacentElement("afterend", bar);
}

/**
 * Remove the navigation footer from the given document.
 * @param {Document} doc
 */
function _navRemoveBar(doc) {
    doc.getElementById("rag-nav-bar")?.remove();
    doc.getElementById("rag-nav-style")?.remove();
}

// ---------------------------------------------------------------------------
// Section registration
// ---------------------------------------------------------------------------

/**
 * Register the item navigation helper.
 * Registers a permanently-hidden section to receive onItemChange events,
 * and injects the nav bar into each window via onInit.
 *
 * Called from bootstrap.js after startup completes.
 */
function registerItemNavigation() {
    Zotero.ItemPaneManager.registerSection({
        paneID: _NAV_PANE_ID,
        pluginID: _NAV_PLUGIN_ID,
        header: {
            // Section is always hidden; the l10n key and icon are required by the API
            // but never displayed.
            l10nID: "pane-item-navigation",
            icon: "chrome://zotero/skin/16/universal/restore.svg",
        },
        sidenav: {
            l10nID: "pane-item-navigation",
            icon: "chrome://zotero/skin/20/universal/sync.svg",
        },

        onInit: ({ doc }) => {
            _navInjectBar(doc);
        },

        onDestroy: ({ doc }) => {
            _navRemoveBar(doc);
        },

        /**
         * @param {{ item: any, setEnabled: (enabled: boolean) => void }} param0
         */
        onItemChange: ({ item, setEnabled }) => {
            setEnabled(false); // Keep the section itself hidden — we only need the callback
            _navRecordItem(item);
        },

        // onRender is required by the API; no-op since we have no visible body.
        onRender: () => {},
    });
}

/**
 * Unregister the item navigation helper.
 * Nav bars are cleaned up via the onDestroy callback.
 *
 * Called from bootstrap.js during shutdown.
 */
function unregisterItemNavigation() {
    Zotero.ItemPaneManager.unregisterSection(_NAV_PANE_ID);
}
