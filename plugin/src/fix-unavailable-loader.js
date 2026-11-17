// Loader for fix-unavailable.xhtml: pulls in the window's scripts once the DOM is ready.
// Kept in its own file (not an inline <script>) because Firefox's baseline
// chrome CSP (`script-src chrome: resource: moz-src:`), which Zotero 11 plans to
// enforce, blocks inline scripts in chrome: documents.
document.addEventListener("DOMContentLoaded", () => {
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero/content/include.js",
      window
    );
  } catch (e) {
    console.error("Failed to load include.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/toolkit.bundle.js",
      window
    );
  } catch (e) {
    console.error("Failed to load toolkit.bundle.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/fix-unavailable-debug.js",
      window
    );
  } catch (e) {
    console.error("Failed to load fix-unavailable-debug.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/fix-unavailable.js",
      window
    );
  } catch (e) {
    console.error("Failed to load fix-unavailable.js:", e);
  }
});
