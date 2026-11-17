// Loader for dialog.xhtml: pulls in the window's scripts once the DOM is ready.
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

  // Load remote indexer (must load before dialog.js)
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/remote_indexer.js",
      window
    );
  } catch (e) {
    console.error("Failed to load remote_indexer.js:", e);
  }

  // Load shared rate-limit bars (must load before dialog.js)
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/rate-limit-widget.js",
      window
    );
  } catch (e) {
    console.error("Failed to load rate-limit-widget.js:", e);
  }

  // Load mention/citation search (must load before dialog.js)
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/mentions.js",
      window
    );
  } catch (e) {
    console.error("Failed to load mentions.js:", e);
  }

  // Load dialog script
  try {
    Services.scriptloader.loadSubScript(
      "chrome://zotero-rag/content/dialog.js",
      window
    );
  } catch (e) {
    console.error("Failed to load dialog.js:", e);
  }
});
