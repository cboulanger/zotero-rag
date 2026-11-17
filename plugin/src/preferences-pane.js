// Preference-pane script, registered via `scripts` in Zotero.PreferencePanes.register()
// (bootstrap.js). Zotero loads it into a sandbox whose prototype is the settings
// window, before inserting preferences.xhtml, and then dispatches a non-bubbling
// `load` event on each of the pane's top-level elements.
//
// This replaces an inline `onload="..."` attribute on the pane's root element:
// Firefox's baseline chrome CSP (`script-src chrome: resource: moz-src:`), which
// Zotero 11 plans to enforce, blocks inline event handlers in chrome: documents.
// A capturing listener on the document sees the non-bubbling event on its way down.
// It must be the document, not the window: Gecko never propagates `load` events
// from a document to its window, so a window listener is never called.

/** @param {Event} event */
function zoteroRAGPrefPaneOnLoad(event) {
	const target = /** @type {Element|null} */ (event.target);
	if (!target || target.id !== 'zotero-rag-prefs-container') return;
	document.removeEventListener('load', zoteroRAGPrefPaneOnLoad, true);
	Zotero.ZoteroRAG.initPrefPane(window);
}

document.addEventListener('load', zoteroRAGPrefPaneOnLoad, true);
