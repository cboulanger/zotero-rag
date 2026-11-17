// Loader for setup-wizard.xhtml: pulls in the window's scripts once the DOM is ready.
// Kept in its own file (not an inline <script>) because Firefox's baseline
// chrome CSP (`script-src chrome: resource: moz-src:`), which Zotero 11 plans to
// enforce, blocks inline scripts in chrome: documents.
document.addEventListener("DOMContentLoaded", () => {
  try {
    Services.scriptloader.loadSubScript("chrome://zotero/content/include.js", window);
  } catch (e) {
    console.error("Failed to load include.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript("chrome://zotero-rag/content/provider-sections.js", window);
  } catch (e) {
    console.error("Failed to load provider-sections.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript("chrome://zotero-rag/content/setup-wizard.js", window);
  } catch (e) {
    console.error("Failed to load setup-wizard.js:", e);
  }
});
