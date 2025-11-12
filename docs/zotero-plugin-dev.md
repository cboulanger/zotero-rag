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

## References

- Working example: `zotero-addons/addon/` directory shows HTML-based dialog with chrome protocol
- Zotero 7 docs: <https://www.zotero.org/support/dev/zotero_7_for_developers>
- Zotero 8 docs: <https://www.zotero.org/support/dev/zotero_8_for_developers>
  - note, in particular: "A new API allows plugins to create custom menu items in Zotero's menu popups. Plugins should use this official API if possible rather than manually injecting content."
- Key insight: Pragmatic approach - prefer HTML but accept XUL where necessary (menus, dialogs)
- Zotero's synchronization technical documentation: https://www.zotero.org/support/dev/web_api/v3/syncing