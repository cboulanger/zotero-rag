# Zotero Plugin Development - Technical Notes

## Overview

Zotero 7/8 uses a bootstrapped extension model based on Firefox's legacy add-on system. While XUL is deprecated in Firefox, it's still functional and commonly used in Zotero plugins.

## Essential Files

### manifest.json

```json
{
  "manifest_version": 2,
  "name": "Plugin Name",
  "version": "0.1.0",
  "author": "Author Name",
  "applications": {
    "zotero": {
      "id": "plugin@example.com",
      "update_url": "https://example.com/updates.json",
      "strict_min_version": "7.0",
      "strict_max_version": "8.*"
    }
  }
}
```

**Required fields for Zotero 8 beta:**

- `author`
- `update_url`
- `strict_max_version`

### bootstrap.js

Entry point for plugin lifecycle:

```javascript
var chromeHandle;

async function startup({ id, version, rootURI }) {
  // 1. Register chrome:// protocol (critical for loading resources)
  var aomStartup = Components.classes[
    "@mozilla.org/addons/addon-manager-startup;1"
  ].getService(Components.interfaces.amIAddonManagerStartup);
  var manifestURI = Services.io.newURI(rootURI + "manifest.json");
  chromeHandle = aomStartup.registerChrome(manifestURI, [
    ["content", "plugin-name", rootURI]
  ]);

  // 2. Register preferences pane
  Zotero.PreferencePanes.register({
    pluginID: 'plugin@example.com',
    src: rootURI + 'preferences.xhtml',
    scripts: [rootURI + 'preferences.js']
  });

  // 3. Load main plugin script
  Services.scriptloader.loadSubScript(rootURI + 'main.js');
}

function shutdown() {
  // Clean up chrome protocol
  if (chromeHandle) {
    chromeHandle.destruct();
    chromeHandle = null;
  }
}
```

## Chrome Protocol Registration

**Critical:** Without registering `chrome://plugin-name/content/`, resources cannot be loaded via chrome URLs.

Pattern:

```javascript
chromeHandle = aomStartup.registerChrome(manifestURI, [
  ["content", "plugin-name", rootURI]
]);
```

This maps `chrome://plugin-name/content/` → `rootURI`

## Dialogs

### XHTML Structure

Use HTML document with XHTML namespace (not pure XUL):

```xml
<?xml version="1.0"?>
<?xml-stylesheet href="chrome://global/skin/" type="text/css"?>
<?xml-stylesheet href="chrome://zotero/skin/zotero.css" type="text/css"?>
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml"
      xmlns:xul="http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul">
<head>
  <title>Dialog Title</title>
  <meta charset="utf-8"/>
  <script>
    document.addEventListener("DOMContentLoaded", (ev) => {
      Services.scriptloader.loadSubScript(
        "chrome://zotero/content/include.js",
        this
      );
      Services.scriptloader.loadSubScript(
        "chrome://plugin-name/content/dialog.js",
        window
      );
    });
  </script>
  <link rel="stylesheet" href="dialog.css"/>
  <style>
    /* Inline styles or external CSS via link tag */
  </style>
</head>
<body>
  <!-- Standard HTML elements -->
  <input type="text" id="query-input"/>
  <button id="submit-btn">Submit</button>
  <textarea id="results" readonly></textarea>
</body>
</html>
```

**HTML Form Elements:** Use standard HTML elements (`<input>`, `<button>`, `<textarea>`, `<label>`, `<select>`) instead of XUL equivalents. Style with CSS using flexbox or grid for layout.

### Loading Scripts

Use `Services.scriptloader.loadSubScript()` instead of `<script src="">`:

```javascript
Services.scriptloader.loadSubScript(
  "chrome://plugin-name/content/script.js",
  window  // target scope
);
```

### Opening Dialogs

Method 1: Using chrome:// protocol (requires chrome registration):

```javascript
const dialogURL = 'chrome://plugin-name/content/dialog.xhtml';
const dialogFeatures = 'chrome,centerscreen,modal,resizable=yes,width=600,height=500';

window.openDialog(
  dialogURL,
  'dialog-id',
  dialogFeatures,
  { plugin: this }  // Passed as window.arguments[0]
);
```

Method 2: Using rootURI directly (no chrome registration needed):

```javascript
openQueryDialog(window) {
  const dialogURL = this.rootURI + 'dialog.xhtml';
  const dialogFeatures = 'chrome,centerscreen,modal,resizable=yes,width=600,height=500';

  window.openDialog(
    dialogURL,
    'zotero-rag-dialog',
    dialogFeatures,
    { plugin: this }
  );
}
```

**Important:** Method 1 requires chrome protocol registration in bootstrap.js. Method 2 works without registration but uses file:// URLs internally.

### Dialog Script Pattern

Basic pattern:

```javascript
var MyDialog = {
  plugin: null,

  init() {
    // Get plugin reference from window.arguments
    if (window.arguments && window.arguments[0]) {
      this.plugin = window.arguments[0].plugin;
    }

    // Set up event listeners
    document.getElementById('submit').addEventListener('click', () => {
      this.submit();
    });
  }
};

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    MyDialog.init();
  });
} else {
  MyDialog.init();
}
```

With async operations and SSE:

```javascript
var MyDialog = {
  plugin: null,
  eventSource: null,

  init() {
    if (window.arguments && window.arguments[0]) {
      this.plugin = window.arguments[0].plugin;
    }

    document.getElementById('submit').addEventListener('click', async () => {
      await this.submit();
    });
  },

  async submit() {
    const query = document.getElementById('query-input').value;

    // SSE for streaming responses
    const url = `http://localhost:8000/stream?query=${encodeURIComponent(query)}`;
    this.eventSource = new EventSource(url);

    this.eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);
      this.updateProgress(data);
    };

    this.eventSource.onerror = () => {
      this.eventSource.close();
      this.eventSource = null;
    };
  },

  cleanup() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
};
```

## Menu Items

Menu items must use XUL elements (can't avoid XUL here):

```javascript
addToWindow(window) {
  let doc = window.document;

  // Create XUL menuitem
  let menuitem = doc.createXULElement('menuitem');
  menuitem.id = 'plugin-menu-item';
  menuitem.setAttribute('label', 'Menu Label');
  menuitem.addEventListener('command', () => {
    this.handleCommand(window);
  });

  // Add to Tools menu
  let toolsMenu = doc.getElementById('menu_ToolsPopup');
  if (toolsMenu) {
    toolsMenu.appendChild(menuitem);
  }
}
```

## Preferences

Preferences are HTML fragments (not full documents):

```xml
<linkset>
  <html:link rel="stylesheet" href="chrome://global/skin/" xmlns:html="http://www.w3.org/1999/xhtml"/>
</linkset>

<script src="preferences.js" xmlns="http://www.w3.org/1999/xhtml"/>

<html:div xmlns:html="http://www.w3.org/1999/xhtml">
  <html:fieldset>
    <html:legend>Settings</html:legend>
    <html:input id="setting-input" type="text"/>
  </html:fieldset>
</html:div>
```

## Zotero API Access

### Getting Libraries

```javascript
// User library
const userLibraryID = Zotero.Libraries.userLibraryID;
const userLibrary = Zotero.Libraries.get(userLibraryID);

// Group libraries
const groups = Zotero.Groups.getAll();
for (let group of groups) {
  const libraryID = group.libraryID;
  const name = group.name;
}
```

### Current Selection

```javascript
const zoteroPane = Zotero.getActiveZoteroPane();
const libraryID = zoteroPane.getSelectedLibraryID();
```

## Localization

Create `.ftl` files in `locale/{lang}/`:

```
# locale/en-US/plugin.ftl
menu-item-label = Menu Label
```

For menu items, use direct `setAttribute('label', ...)` if localization doesn't load:

```javascript
menuitem.setAttribute('label', 'Fallback Label');
```

## Logging

Zotero plugins run in a privileged chrome context where standard `console.log()` is **silent** — messages do not appear anywhere visible. Use `Services.console` instead.

### Where to view messages

Open the **Browser Console** via **Tools > Developer > Browser Console** (or `Cmd+Shift+J` on macOS). This is the "Parent process Browser Console" that shows chrome-context output.

### bootstrap.js (loadSubScript scope)

`console` is not defined here. Use `Services.console.logStringMessage()` directly:

```javascript
function log(msg) {
  Services.console.logStringMessage("My Plugin: " + msg);
}
```

### Window/dialog scripts

`console` exists in window scope but its methods are silent. Patch them at the top of the script to route through `Services.console`:

- `log`/`info` → `logStringMessage` (neutral, no severity colour)
- `warn`/`error` → `nsIScriptError` with the appropriate severity flag

```javascript
;(function() {
  const nsFlags = { warn: 0x1, error: 0x0 };
  const makeLogger = (level) => (...args) => {
    const msg = "[My Plugin] " + args.join(" ");
    if (level === "log" || level === "info") {
      Services.console.logStringMessage(msg);
    } else {
      const e = Cc["@mozilla.org/scripterror;1"].createInstance(Ci.nsIScriptError);
      e.init(msg, "", null, 0, 0, nsFlags[level], "chrome javascript");
      Services.console.logMessage(e);
    }
  };
  ["log", "info", "warn", "error"].forEach(level => { console[level] = makeLogger(level); });
})();
```

For scripts loaded via `loadSubScript` (e.g. a main plugin script that also needs `console`), check whether `console` exists first and create it on `globalThis` if not:

```javascript
;(function() {
  const nsFlags = { warn: 0x1, error: 0x0 };
  const makeLogger = (level) => (...args) => {
    const msg = "[My Plugin] " + args.join(" ");
    if (level === "log" || level === "info") {
      Services.console.logStringMessage(msg);
    } else {
      const e = Cc["@mozilla.org/scripterror;1"].createInstance(Ci.nsIScriptError);
      e.init(msg, "", null, 0, 0, nsFlags[level], "chrome javascript");
      Services.console.logMessage(e);
    }
  };
  if (typeof console === "undefined") {
    globalThis.console = { log: makeLogger("log"), info: makeLogger("info"), warn: makeLogger("warn"), error: makeLogger("error") };
  } else {
    ["log", "info", "warn", "error"].forEach(level => { console[level] = makeLogger(level); });
  }
})();
```

Wrap in an IIFE (as shown) to avoid `const` redeclaration errors on hot-reload, since `loadSubScript` re-runs the file in the same global scope.

### nsIScriptError severity flags

| Flag | Value | Display |
| --- | --- | --- |
| `errorFlag` | `0x0` | Red (error) |
| `warningFlag` | `0x1` | Yellow (warning) |
| `infoFlag` | `0x8` | Note: renders as error in practice; use `logStringMessage` for neutral output |

## Common Pitfalls

1. **Empty dialog window**: Missing chrome protocol registration or incorrect script loading
2. **Resources not loading**: Using `rootURI` instead of `chrome://` URLs after registration
3. **Menu label missing**: Localization files not loading; use fallback labels
4. **Installation fails**: Missing required manifest fields (`author`, `update_url`, `strict_max_version`)

## Build Process

Typical structure:

```
plugin/
├── src/           # Source files
├── build/         # Copied files before packaging
├── dist/          # Final .xpi file
└── scripts/
    └── build-plugin.js  # Build script
```

Build script copies src → build, then creates XPI from build directory.

## Hot-Reload Development

This project uses [`zotero-plugin-scaffold`](https://zotero-plugin-dev.github.io/zotero-plugin-scaffold/) which provides a [development server with hot-reloading](https://zotero-plugin-dev.github.io/zotero-plugin-scaffold/guide/features). When active, any change to files in `plugin/src/` is automatically rebuilt and reloaded in Zotero — no manual rebuild or reinstallation needed.

### One-Time Setup

Add the following to your `.env` file (see `.env.dist` for a template):

```bash
# Path to the Zotero binary
# macOS: /Applications/Zotero.app/Contents/MacOS/zotero
# Windows: C:/Program Files/Zotero/zotero.exe
ZOTERO_PLUGIN_ZOTERO_BIN_PATH=/Applications/Zotero.app/Contents/MacOS/zotero

# Path to a dedicated Zotero profile for development
# Create one via: /path/to/zotero -p
ZOTERO_PLUGIN_PROFILE_PATH=/path/to/dev-profile
```

Using a separate development profile keeps your personal Zotero library untouched during development.

### Usage

```bash
# Start plugin development server (opens Zotero with hot-reload)
npm run dev:plugin:start

# Stop plugin development server and Zotero
npm run dev:plugin:stop
```

**Workflow:**

1. Close any running Zotero instances.
2. Run `npm run dev:plugin:start` — this starts Zotero with the plugin automatically installed.
3. Edit files in `plugin/src/`.
4. Changes are detected, rebuilt, and reloaded in Zotero automatically.

**Note:** The backend server must be running separately for the plugin to function. Start it with `npm run server:start`.

### Production Build

For final distribution, build the XPI file:

```bash
npm run plugin:build
# Output: plugin/dist/zotero-rag-{version}.xpi
```

## Useful Zotero APIs

APIs discovered through development and source-code research. These are often undocumented or hard to find — add new entries here whenever you discover an API that isn't covered by the official docs.

### Items

```javascript
// Check whether the attachment's local file exists
await item.fileExists()                         // Promise<boolean>

// Get the full local file path (null if missing)
await item.getFilePathAsync()                   // Promise<string|null>

// Attachment properties
item.attachmentFilename                         // filename only (no directory), e.g. "paper.pdf"
item.attachmentSyncedHash                       // MD5 hex string; only set by Zotero File Storage, NOT WebDAV
item.attachmentContentType                      // MIME type string, e.g. "application/pdf"
item.parentItemID                               // numeric ID of parent item, or false/null for top-level
item.libraryID                                  // numeric library ID
item.key                                        // 8-char alphanumeric Zotero key
item.id                                         // numeric database ID
item.deleted                                    // boolean — true if item is in the trash

// Creators — returns raw DB objects, not typed strings
item.getCreators()
// → Array<{creatorTypeID: number, firstName: string, lastName: string, name: string, fieldMode: number}>
// NOTE: no .creatorType string — use creatorTypeID or don't filter by type

// Relations
item.getRelations()                             // → Record<predicate, string|string[]>
item.getRelationsByPredicate(predicate)         // → string[] of URIs

// Attachment IDs on a regular item
item.getAttachments()                           // → number[] of attachment itemIDs
```

### Relations

```javascript
Zotero.Relations.linkedObjectPredicate          // 'owl:sameAs'
Zotero.Relations.replacedItemPredicate          // 'dc:replaces' URI

// Resolve a Zotero URI to an item (cross-library)
await Zotero.URI.getURIItem(uri)                // → Zotero.Item | null
```

### Attachments

```javascript
// Storage directory for an attachment (nsIFile)
const dir = Zotero.Attachments.getStorageDirectory(attachmentItem)
// dir.path → absolute path string

// "Find Available PDF/File" pipeline — bypasses canFindFileForItem() check
const resolvers = Zotero.Attachments.getFileResolvers(parentItem, methods, automatic)
// methods defaults to ['doi', 'url', 'oa', 'custom']
// returns array of resolver objects understood by downloadFirstAvailableFile()

const { title, mimeType, url, props } =
    await Zotero.Attachments.downloadFirstAvailableFile(resolvers, tmpFilePath, options)
// Downloads the first successfully resolved file to tmpFilePath
// options: { enforceFileType, onAccessMethodStart, onBeforeRequest, onRequestError }
// Throws on failure; returns object with url=null if nothing found

// High-level: find & attach a file to an item (creates a new attachment item)
// NOTE: fails silently if item already has a PDF/EPUB attachment
const newAtt = await Zotero.Attachments.addAvailableFile(item, { methods })
// → Zotero.Item (new attachment) or false

// canFindFileForItem: returns false if item already has a PDF/EPUB — often need to bypass
Zotero.Attachments.canFindFileForItem(item)     // → boolean

// Supported MIME types for the resolver pipeline
Zotero.Attachments.FIND_AVAILABLE_FILE_TYPES    // ['application/pdf', 'application/epub+zip']

// Temp directory scoped to an attachment storage pattern
const { path: tmpDir } = await Zotero.Attachments.createTemporaryStorageDirectory()
```

### Sync

```javascript
// Check whether file sync is configured for a library
Zotero.Sync.Storage.Local.getEnabledForLibrary(libraryID)   // → boolean
// Returns false for libraries that only use Zotero sync metadata (no WebDAV / Zotero Storage)

// Trigger a file download from WebDAV or Zotero Storage
await Zotero.Sync.Runner.downloadFile(attachmentItem)
// Does not return a meaningful value; check fileExists() afterwards
// Throws on network error
```

### HTTP

```javascript
// Preferred over fetch() — honours Zotero proxy settings, cookies, and authentication
const req = await Zotero.HTTP.request('GET', url, {
    responseType: 'blob',       // 'blob' | 'arraybuffer' | 'text' | 'json'
    followRedirects: false,     // optional
    errorDelayMax: 0,           // optional
})
// req.status, req.response, req.getResponseHeader(name)
// Throws Zotero.HTTP.UnexpectedStatusException on non-2xx when errorDelayMax is not 0
```

### Database

```javascript
// Direct SQL query — returns array of first-column values
const ids = await Zotero.DB.columnQueryAsync(sql, [param1, param2])

// Relevant itemAttachments columns:
//   itemID        — FK to items.itemID
//   linkMode      — 0=imported_file, 1=imported_url, 2=linked_file, 3=linked_url
//   path          — "storage:<filename>" for imported attachments (linkMode 0/1)
//   storageHash   — MD5 hex; only populated by Zotero File Storage, NULL for WebDAV
//   contentType   — MIME type string
```

### Notifications (Notifier)

```javascript
// Watch for events (collection selection, item changes, etc.)
const id = Zotero.Notifier.registerObserver(
    { notify(event, type, ids, extraData) { /* ... */ } },
    ['collection']   // array of type strings: 'item', 'collection', 'library', ...
)
Zotero.Notifier.unregisterObserver(id)

// Permanent item deletion: event='delete', type='item'
// extraData[id] = { libraryID: number, key: string } — populated even after item is gone from DB
// NOTE: trashing an item does NOT fire 'delete'; only permanent erasure (empty trash / erase()) does.
// Source: Zotero.DataObject.prototype._finalizeErase → Notifier.queue('delete', objectType, ...)
```

### Pane & Window

```javascript
const pane = Zotero.getActiveZoteroPane()
pane.getSelectedLibraryID()     // → number
await pane.selectItem(itemID)   // focuses item in item list, expanding parent if needed

// Focus an already-open dialog window (if windowtype is registered)
Services.wm.getMostRecentWindow('windowtype-string')   // → Window | null
// NOTE: windowtype must be set on the document element; openDialog's name arg is NOT the windowtype

// Window opened via openDialog
window.arguments[0]             // args object passed as last arg to openDialog()
window.opener                   // the window that called openDialog()
window.closed                   // boolean — true after window.close()
```

### Libraries

```javascript
// Current sync version of a library (highest item version seen by Zotero sync).
// Increases whenever items are added or modified.  Use to detect whether the library
// has changed since a previous operation.
Zotero.Libraries.get(libraryID).libraryVersion   // → number (0 if never synced)

// Numeric ID of the user's personal library (always present)
Zotero.Libraries.userLibraryID                   // → number

// Convert group → numeric library ID
Zotero.Groups.get(groupId).libraryID             // → number
```

### Utilities & Globals

```javascript
// Temporary directory (nsIFile)
Zotero.getTempDirectory().path  // absolute path string

// Cross-platform path helpers (Firefox/Zotero globals)
PathUtils.join(a, b, ...)       // → string
PathUtils.filename(path)        // → basename string

// File I/O (Firefox/Zotero globals)
await IOUtils.copy(srcPath, destPath)
await IOUtils.makeDirectory(path, { createAncestors: true, ignoreExisting: true })
await IOUtils.write(path, Uint8Array)
await IOUtils.remove(path, { recursive: true })
```

## References

- Working example: `zotero-addons/addon/` directory shows HTML-based dialog with chrome protocol
- Zotero 7 docs: <https://www.zotero.org/support/dev/zotero_7_for_developers>
- Zotero 8 docs: <https://www.zotero.org/support/dev/zotero_8_for_developers>
  - note, in particular: "A new API allows plugins to create custom menu items in Zotero's menu popups. Plugins should use this official API if possible rather than manually injecting content."
- Key insight: Pragmatic approach - prefer HTML but accept XUL where necessary (menus, dialogs)
- Zotero's synchronization technical documentation: https://www.zotero.org/support/dev/web_api/v3/syncing