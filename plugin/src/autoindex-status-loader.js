// Loader for autoindex-status.xhtml: pulls in the window's scripts once the DOM is ready.
// Kept in its own file (not an inline <script>) because Firefox's baseline
// chrome CSP (`script-src chrome: resource: moz-src:`), which Zotero 11 plans to
// enforce, blocks inline scripts in chrome: documents.
document.addEventListener("DOMContentLoaded", (ev) => {
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero/content/include.js",
      this
    );
  } catch (e) {
    console.error("Failed to load include.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/rate-limit-widget.js",
      window
    );
  } catch (e) {
    console.error("Failed to load rate-limit-widget.js:", e);
  }
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/autoindex-status.js",
      window
    );
  } catch (e) {
    console.error("Failed to load autoindex-status.js:", e);
  }
});
