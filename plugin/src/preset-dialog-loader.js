// Loader for preset-dialog.xhtml: pulls in the window's scripts once the DOM is ready.
// Kept in its own file (not an inline <script>) because Firefox's baseline
// chrome CSP (`script-src chrome: resource: moz-src:`), which Zotero 11 plans to
// enforce, blocks inline scripts in chrome: documents.
document.addEventListener("DOMContentLoaded", () => {
  try {
    Services.scriptloader.loadSubScript("chrome://zotero/content/include.js", this);
  } catch (e) {
    console.error("Failed to load include.js:", e);
  }
  for (const script of ["preset-dialog-view.js", "preset-dialog.js"]) {
    try {
      Services.scriptloader.loadSubScript(`chrome://zotero-rag/content/${script}`, window);
    } catch (e) {
      console.error(`Failed to load ${script}:`, e);
    }
  }
});
