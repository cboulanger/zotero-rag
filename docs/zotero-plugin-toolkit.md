# Zotero Plugin Toolkit — Developer Guide

**Source**: `/Users/cboulanger/Code/zotero-plugin-toolkit`
**Version**: 5.1.2 (supports Zotero 6, 7, 8)
**npm**: `zotero-plugin-toolkit`

---

## Overview

The toolkit provides UI helpers, managers, and utilities that abstract cross-version Zotero compatibility and plugin lifecycle management. It is modular: import only what you need.

## Using the Toolkit in This Project

The toolkit is pre-bundled at [plugin/src/toolkit.bundle.js](../plugin/src/toolkit.bundle.js), which is built from [plugin/src/toolkit.js](../plugin/src/toolkit.js). That wrapper exports a `createToolkit(config)` factory that returns a pre-initialized `basicTool`, `uiTool`, `progressHelper`, `showAlert()`, `showError()`, and `showNotification()`.

### Adding a New Toolkit API

If you need a class or helper not currently in the bundle (e.g. `VirtualizedTableHelper`):

1. **Edit `plugin/src/toolkit.js`** — import the class from `'zotero-plugin-toolkit'` and either re-export it or include it in the `createToolkit` return value:

   ```js
   import { BasicTool, UITool, ProgressWindowHelper, VirtualizedTableHelper } from 'zotero-plugin-toolkit';

   export { VirtualizedTableHelper };  // re-export so it appears on the global

   export function createToolkit(config) { /* ... */ }
   ```

2. **Rebuild the bundle**:

   ```bash
   node scripts/build_toolkit.js
   ```

   This regenerates `plugin/src/toolkit.bundle.js` (globalName `ZoteroPluginToolkit`). After the rebuild, `ZoteroPluginToolkit.VirtualizedTableHelper` is available in any window that loads the bundle.

3. **Load the bundle in the target window** — dialog windows (e.g. `fix-unavailable.xhtml`) do not inherit the main window's globals. Load the bundle explicitly via `loadSubScript`:

   ```js
   Services.scriptloader.loadSubScript(
     "chrome://zotero-rag/content/toolkit.bundle.js",
     window
   );
   ```

   After this, `ZoteroPluginToolkit.VirtualizedTableHelper` is available in that window's scope.

4. **Commit both** `plugin/src/toolkit.js` and the rebuilt `plugin/src/toolkit.bundle.js`.

**Two usage styles elsewhere**:

```js
// Full (convenient, larger bundle)
import { ZoteroToolkit } from "zotero-plugin-toolkit";
const ztoolkit = new ZoteroToolkit();
ztoolkit.UI.createElement(...)

// Selective (recommended)
import { UITool, KeyboardManager, DialogHelper } from "zotero-plugin-toolkit";
```

---

## Core Classes

### BasicTool

Base class for all toolkit classes. Provides logging and safe access to Zotero globals.

```js
import { BasicTool } from "zotero-plugin-toolkit";
const tool = new BasicTool();

// Type-safe global access (Zotero, ZoteroPane, window, document, ...)
const Zotero = tool.getGlobal("Zotero");

// Logs to both console.log and Zotero.debug with stack trace
tool.log("Something happened", someValue);

// Create XUL elements with Zotero 6/7/8 compatibility
const menuitem = tool.createXULElement(document, "menuitem");
```

**Constructor options**:

```js
new BasicTool({
  log: { prefix: "[MyPlugin]", disableConsole: false, disableZLog: false }
})
```

### ManagerTool

Extends `BasicTool`. All manager classes inherit from it and auto-unregister on plugin unload when the `pluginID` is set in options.

---

## UITool — Element Creation

```js
import { UITool } from "zotero-plugin-toolkit";
const ui = new UITool();
```

### createElement

Creates HTML, XUL, SVG, or DocumentFragment elements declaratively:

```js
const panel = ui.createElement(document, "vbox", {
  id: "my-panel",
  namespace: "xul",          // "html" (default) | "xul" | "svg"
  classList: ["panel"],
  styles: { padding: "8px", display: "flex" },
  attributes: { hidden: "false" },
  properties: { innerText: "Hello" },  // set via elem.prop =
  listeners: [
    { type: "click", listener: (e) => doSomething(e) }
  ],
  children: [
    {
      tag: "html:input",
      id: "my-input",
      attributes: { type: "text", placeholder: "Search..." }
    },
    {
      tag: "html:button",
      properties: { textContent: "Go" },
      listeners: [{ type: "click", listener: () => search() }]
    }
  ]
});
```

**Deduplication props**:

- `ignoreIfExists: true` — skip creation if element with same `id` exists
- `removeIfExists: true` — remove existing element first, then recreate
- `customCheck: (doc, props) => boolean` — only create if returns true

### Inserting Elements

```js
ui.appendElement(props, containerElement);
ui.insertElementBefore(props, referenceNode);
ui.replaceElement(props, oldNode);
```

### XHTML Parsing

```js
const fragment = ui.parseXHTMLToFragment(
  `<vbox><label value="Hello"/></vbox>`,
  [],       // entity definitions
  true      // defaultXUL namespace
);
```

### Cleanup

All created elements are tracked automatically:

```js
ui.unregisterAll();  // removes all tracked elements from DOM
```

---

## KeyboardManager — Global Shortcuts

```js
import { KeyboardManager } from "zotero-plugin-toolkit";
const keyboard = new KeyboardManager();

keyboard.register((event, options) => {
  // options.type: "keydown" | "keyup"
  // options.keyboard: KeyModifier instance
  if (options.keyboard?.equals("accel,s")) {
    // Cmd+S on Mac, Ctrl+S on Windows
  }
  if (options.keyboard?.equals("accel,shift,p")) {
    openCommandPalette();
  }
});

keyboard.unregisterAll();
```

### KeyModifier

```js
import { KeyModifier } from "zotero-plugin-toolkit";

const mod = new KeyModifier("accel,shift,s");
mod.accel    // true
mod.shift    // true
mod.key      // "s"

mod.getLocalized()  // "⌘⇧S" (Mac) or "Ctrl+Shift+S" (Windows)
mod.getRaw()        // "accel,shift,s"
mod.equals("accel,shift,s")  // true
```

---

## PromptManager — Command Palette

Adds a fuzzy-searchable command palette (triggered by Shift+P by default).

```js
import { PromptManager } from "zotero-plugin-toolkit";
const prompt = new PromptManager();

prompt.register([
  {
    name: "Export selected items",
    label: "MyPlugin",
    id: "myplugin-export",
    when: () => ZoteroPane.getSelectedItems().length > 0,
    callback: async (promptInstance) => {
      await exportItems();
    }
  },
  {
    name: "Settings",
    label: "MyPlugin",
    id: "myplugin-settings",
    callback: () => openSettings()
  }
]);

prompt.unregister("myplugin-export");
prompt.unregisterAll();
```

**Dynamic sub-menus**: return an array of `Command` from the callback to show a nested list.

---

## DialogHelper — Modal Dialogs

Builds dialogs with a grid layout (rows × columns).

```js
import { DialogHelper } from "zotero-plugin-toolkit";

const dialog = new DialogHelper(3, 2);  // 3 rows, 2 columns

dialog
  .addCell(0, 0, { tag: "html:label", properties: { innerText: "Name:" } })
  .addCell(0, 1, { tag: "html:input", id: "name-input", attributes: { type: "text" } })
  .addCell(1, 0, { tag: "html:label", properties: { innerText: "Format:" } })
  .addCell(1, 1, {
    tag: "html:select", id: "format-select",
    children: [
      { tag: "html:option", properties: { value: "csv", innerText: "CSV" } },
      { tag: "html:option", properties: { value: "json", innerText: "JSON" } }
    ]
  })
  .addButton("Cancel", "cancel")
  .addButton("OK", "ok", {
    callback: (ev) => {
      const name = dialog.window.document.getElementById("name-input").value;
      processInput(name);
    }
  })
  .setDialogData({
    loadCallback: () => {
      // runs after dialog opens, set initial values here
    },
    unloadCallback: () => {
      // runs after dialog closes, read values here
    }
  })
  .open("Export Settings", {
    width: 400, height: 300,
    centerscreen: true, resizable: true
  });
```

**Checking which button was clicked**:

```js
const data = {};
dialog.setDialogData(data);
dialog.open("Title");
await data.unloadLock?.promise;
if (data._lastButtonId === "ok") { /* ... */ }
```

---

## SettingsDialogHelper — Preference Dialogs

Extends `DialogHelper` with automatic pref binding.

```js
import { SettingsDialogHelper } from "zotero-plugin-toolkit";

const settings = new SettingsDialogHelper(10, 2);  // 10 setting rows, 2 columns

settings
  .setSettingHandlers(
    (key) => Zotero.Prefs.get(`myplugin.${key}`, true),
    (key, value) => Zotero.Prefs.set(`myplugin.${key}`, value, true)
  )
  .addSetting("Enable feature", "featureEnabled", {
    tag: "html:input",
    attributes: { type: "checkbox" }
  })
  .addSetting("API URL", "apiUrl", {
    tag: "html:input",
    attributes: { type: "text", placeholder: "https://..." }
  })
  .addSetting("Max results", "maxResults", {
    tag: "html:input",
    attributes: { type: "number", min: "1", max: "100" }
  }, { valueType: "number" })
  .addButton("Save", "save")
  .open("Plugin Settings", { width: 500, height: 400 });
```

---

## ProgressWindowHelper — Progress Popups

```js
import { ProgressWindowHelper } from "zotero-plugin-toolkit";

const win = new ProgressWindowHelper("Importing items", {
  closeOnClick: true,
  closeTime: 3000
});

const line = win.createLine({ type: "default", text: "Starting...", progress: 0 });
win.show();

// Update progress
win.changeLine({ text: "Processing 5/10...", progress: 50 });

// Mark done
win.changeLine({ type: "success", text: "Done! Imported 10 items.", progress: 100 });
```

**Type values**: `"success"` (green check), `"fail"` (red X), or a custom icon key.

---

## FilePickerHelper — Native File Dialogs

```js
import { FilePickerHelper } from "zotero-plugin-toolkit";

// Open single file
const path = await new FilePickerHelper(
  "Select PDF",
  "open",
  [["PDF Files", "*.pdf"], ["All Files", "*.*"]]
).open();

if (path !== false) {
  processFile(path);
}

// Save file
const savePath = await new FilePickerHelper(
  "Export as CSV",
  "save",
  [["CSV", "*.csv"]],
  "export.csv"  // suggested filename
).open();

// Select folder
const folder = await new FilePickerHelper("Select folder", "folder").open();

// Multiple files — returns string[]
const paths = await new FilePickerHelper("Select files", "multiple").open();
```

---

## ClipboardHelper — Clipboard Operations

```js
import { ClipboardHelper } from "zotero-plugin-toolkit";

new ClipboardHelper()
  .addText("Plain text", "text/unicode")
  .addText("<b>Rich text</b>", "text/html")
  .copy();

// Copy an image (base64 PNG)
new ClipboardHelper()
  .addImage("data:image/png;base64,iVBORw0KGgo...")
  .copy();

// Copy a file path
new ClipboardHelper()
  .addFile("/path/to/file.pdf")
  .copy();
```

---

## ReaderTool — PDF/EPUB Reader

```js
import { ReaderTool } from "zotero-plugin-toolkit";
const reader = new ReaderTool();

// Get current reader (waits up to 5 seconds for it to load)
const instance = await reader.getReader(5000);

// Get selected annotation
const annotation = reader.getSelectedAnnotationData(instance);
// annotation: { text, color, pageLabel, position, sortIndex, type }

// Get selected text
const text = reader.getSelectedText(instance);

// Get all open reader windows
const windows = reader.getWindowReader();
```

---

## ExtraFieldTool — Item Extra Fields

```js
import { ExtraFieldTool } from "zotero-plugin-toolkit";
const extraField = new ExtraFieldTool();

const item = Zotero.Items.get(42);

// Read all fields as Map<string, string[]>
const fields = extraField.getExtraFields(item);
const dois = fields.get("DOI");  // string[] — supports multiple values

// Read single field
const doi = extraField.getExtraField(item, "DOI");  // first value or undefined
const allDois = extraField.getExtraField(item, "DOI", true);  // string[]

// Set a field (replaces existing)
await extraField.setExtraField(item, "myKey", "myValue");

// Append without overwriting
await extraField.setExtraField(item, "tag", "new-tag", { append: true });

// Replace all extra fields at once
const newFields = new Map([["DOI", ["10.1234/foo"]], ["ISBN", ["978-..."]]] );
await extraField.replaceExtraFields(item, newFields);
```

---

## FieldHookManager — Intercept Item Fields

Override how Zotero reads or writes item fields — useful for virtual fields shown in the item info pane.

```js
import { FieldHookManager } from "zotero-plugin-toolkit";
const hooks = new FieldHookManager();

// Override getField for a custom virtual field
hooks.register("getField", "myVirtualField",
  (field, unformatted, includeBaseMapped, item, original) => {
    if (item.itemType === "journalArticle") {
      return computeVirtualValue(item);
    }
    return original.apply(item, [field, unformatted, includeBaseMapped]);
  }
);

// Make Zotero treat "myVirtualField" as if it belongs to base field "title"
hooks.register("isFieldOfBase", "myVirtualField",
  (field, baseField, original) => {
    if (baseField === "title") return true;
    return original(field, baseField);
  }
);

hooks.unregister("getField", "myVirtualField");
hooks.unregisterAll();
```

---

## PatchHelper — Monkey-Patching

```js
import { PatchHelper } from "zotero-plugin-toolkit";

const patch = new PatchHelper();
patch.setData({
  target: Zotero.Items,
  funcSign: "merge",
  patcher: (original) => async function(item, otherItems, ...args) {
    Zotero.log("merge called");
    return original.apply(this, [item, otherItems, ...args]);
  },
  enabled: true
});

patch.enable();
patch.disable();  // restores original
```

---

## LargePrefHelper — Large Preference Storage

Stores large structured data in Zotero prefs by splitting across multiple keys.

```js
import { LargePrefHelper } from "zotero-plugin-toolkit";

const store = new LargePrefHelper(
  "myplugin.dataKeys",     // pref that stores the list of sub-keys
  "myplugin.data.",        // prefix for individual value prefs
  "default"                // hook preset: auto JSON parse/stringify
);

// Use like an object via Proxy
const obj = store.asObject();
obj.myKey = { complex: "value", list: [1, 2, 3] };
console.log(obj.myKey.list);  // [1, 2, 3]

// Or use like a Map
const map = store.asMapLike();
map.set("foo", "bar");
console.log(map.get("foo"));  // "bar"
```

---

## GuideHelper — Step-by-Step Guides

```js
import { GuideHelper } from "zotero-plugin-toolkit";

const guide = new GuideHelper();
guide
  .addStep({
    element: "#zotero-items-pane",
    title: "Items Pane",
    description: "Your library items appear here.",
    position: "after_end",
    showButtons: ["next", "close"]
  })
  .addStep({
    element: () => document.querySelector(".toolbar-button"),
    title: "Add Item",
    description: "Click here to add a new item.",
    showButtons: ["prev", "close"],
    onNextClick: async ({ step, guide }) => {
      await guide.show(document);  // advance
    }
  });

await guide.show(document);
```

---

## VirtualizedTableHelper — Large Tables

For rendering thousands of rows efficiently.

```js
import { VirtualizedTableHelper } from "zotero-plugin-toolkit";

const items = getMyItems();  // large array

const table = new VirtualizedTableHelper(window)
  .setProp("id", "my-table")
  .setProp("getRowCount", () => items.length)
  .setProp("getRowData", (index) => ({
    title: items[index].title,
    year: String(items[index].year)
  }))
  .setProp("columns", [
    { dataKey: "title", label: "Title", flex: 2 },
    { dataKey: "year", label: "Year", width: 60 }
  ])
  .setProp("multiSelect", true)
  .setProp("onSelectionChange", (selection) => {
    const selected = selection.selected;
    console.log("Selected indices:", [...selected]);
  })
  .setContainerId("my-table-container");  // must exist in DOM

table.render();
```

### Implementation Details (lessons from fix-unavailable dialog)

#### Required stylesheets

The VirtualizedTable React component needs two platform stylesheets that are **not** loaded by default in dialog windows. Without them every cell stacks vertically instead of rendering as a flex row. Add these `<?xml-stylesheet?>` PIs to your `.xhtml` before `<!DOCTYPE html>`:

```xml
<?xml-stylesheet href="chrome://zotero-platform/content/zotero-react-client.css" type="text/css"?>
<?xml-stylesheet href="chrome://zotero-platform/content/zotero.css" type="text/css"?>
```

Also load `include.js` and `toolkit.bundle.js` via `loadSubScript` on `DOMContentLoaded` — dialog windows do not inherit the main window's globals.

#### Container CSS

The container div must have a **definite pixel height** (not `auto`) for the windowed-list to calculate which rows are visible. Use flex layout:

```css
/* Parent flex column */
.dialog-container { display: flex; flex-direction: column; height: 100%; padding: 12px 12px 0; box-sizing: border-box; }

/* Table container: grows to fill remaining space, collapses to 0 when needed */
#table-container { flex: 1; min-height: 0; overflow: auto; border: 1px solid #ccc; border-radius: 4px; }
```

Use `overflow: auto` (not `overflow: hidden`) on the container — the windowed-list renders all rows into the DOM when it cannot determine a constrained height, and the container's own scrollbar then provides scrolling. With `overflow: hidden` the scrollbar is simply clipped away.

To keep the column header fixed while rows scroll, make the header sticky:

```css
#table-container .virtualized-table-header { position: sticky; top: 0; z-index: 1; }
```

#### Column definitions

```js
const columns = [
  { dataKey: "title",  label: "Title",  flex: 2 },           // grows
  { dataKey: "year",   label: "Year",   fixedWidth: true, width: 48 }, // fixed px
  { dataKey: "type",   label: "Type",   fixedWidth: true, width: 50 },
];
```

- Use `flex: N` for proportional columns, `fixedWidth: true, width: N` for fixed-pixel columns.
- `ignoreInColumnPicker: true` hides a column from the user-facing column picker.
- `column.className` is **auto-populated** by VirtualizedTable to a CSS class derived from the `dataKey` (see `virtualized-table.jsx` line 1475). Use it in renderers: `span.className = \`cell ${column.className}\``.

#### Custom cell rendering with `column.renderer`

Prefer `column.renderer` over a global `renderItem` override. It receives `(index, data, column)` and must return a `<span>` (not a `<div>`) — the table expects `span.cell` children:

```js
{
  dataKey: "status",
  label: "Status",
  flex: 2,
  renderer: (index, _data, column) => {
    const span = document.createElement("span");
    span.className = `cell ${column.className}`;
    const status = myStatusMap.get(index);
    if (status) {
      span.classList.add("status-" + status.cssClass);
      span.textContent = status.text;
    }
    return span;
  }
}
```

Column width CSS is injected by the React component and keyed on `column.className`, so cells that carry that class automatically get the right width — even with a custom renderer.

#### Refreshing rows

```js
tableHelper.treeInstance.invalidateRow(index);  // repaint one row
tableHelper.treeInstance.invalidate();           // repaint all rows
```

Call `invalidateRow` after any state change that affects a single row (e.g. a status update). The renderer is called again and returns a fresh element.

#### `onSelectionChange` gotcha

`onSelectionChange` is called by `clearSelection()` itself. **Never call `clearSelection()` (or any other selection mutation) inside `onSelectionChange`** — it causes infinite recursion. Use a no-op or a guard flag instead.

---

### Checkboxes in VirtualizedTable

#### The fundamental constraint

VirtualizedTable has a single **native cursor selection** model (highlighted row). There is no built-in checkbox selection. When the user clicks a row, `onSelectionChange` fires and the row is highlighted. Any checkbox you render via `column.renderer` is a plain DOM `<input type="checkbox">` that is **recreated on every render** of that row — including whenever the native selection changes.

This means: if you tie checkbox state to the native selection, clicking a row to select it immediately re-renders the checkbox to match the new selection state, effectively resetting anything the user just checked.

#### Recommended pattern: independent `selected` Set

Keep checkbox state completely separate from the native cursor selection:

```js
// On the dialog object:
selected: new Set(),   // indices of checked rows

// In the checkbox column renderer:
renderer: (index, _data, column) => {
  const span = document.createElement("span");
  span.className = `cell ${column.className}`;
  span.style.cssText = "display:flex;align-items:center;justify-content:center;";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = this.selected.has(index);   // read from our own Set, not native selection
  cb.style.margin = "0";
  cb.addEventListener("change", () => {
    if (cb.checked) this.selected.add(index);
    else            this.selected.delete(index);
    this.updateActionButtons();
    // do NOT call invalidateRow here — the checkbox already reflects the new state
  });
  span.appendChild(cb);
  return span;
}

// Leave onSelectionChange as a no-op:
onSelectionChange: () => {}
```

- Read `this.selected` for all downstream logic (`getSelectedIndices`, button enable/disable).
- Pre-populate `this.selected` on load (e.g. select all: `for (let i=0; i<items.length; i++) selected.add(i)`).

#### Select-all toolbar

Put a `<input type="checkbox" id="select-all-cb">` in a toolbar div **above** the table container (not inside VirtualizedTable — the header is not interactive). Sync it with an indeterminate state:

```js
function updateSelectAll() {
  const cb = document.getElementById("select-all-cb");
  const count = this.selected.size, total = this.items.length;
  cb.checked       = total > 0 && count === total;
  cb.indeterminate = count > 0 && count < total;
}
```

#### Known limitation: first click selects, second click toggles

Because VirtualizedTable intercepts `mousedown` at the capture phase to move the native cursor, the first click on a checkbox row moves the cursor AND fires the checkbox change. Subsequent clicks on the same row only fire the checkbox change. This means **unchecking a pre-checked row requires two clicks if that row is not already the cursor row**. This is an inherent limitation of mixing a cursor-selection table with overlay checkboxes. It is acceptable UX for most use cases; if true single-click toggle is required, a custom `renderItem` that manages row rendering entirely is needed (at the cost of losing automatic column-width management).

---

## Utility: waitUntil / waitUntilAsync

```js
import { waitUntil, waitUntilAsync } from "zotero-plugin-toolkit";

// Polling with callback
waitUntil(
  () => document.getElementById("my-element") !== null,
  () => initializeElement(),
  100,    // check every 100ms
  5000    // give up after 5s
);

// Async version
await waitUntilAsync(
  () => Zotero.Schema.schemaUpdatePromise.resolved,
  200,    // poll interval
  10000   // timeout
);

// Wait for reader to be fully initialized
import { waitForReader } from "zotero-plugin-toolkit";
await waitForReader(readerInstance);
```

---

## Lifecycle & Cleanup

**Managers** (`KeyboardManager`, `PromptManager`, `FieldHookManager`) auto-unregister on plugin unload when constructed with the plugin ID in scope.

**UITool** tracks all elements it creates. Call `ui.unregisterAll()` in your plugin shutdown hook.

**ZoteroToolkit** (full bundle) exposes `ztoolkit.unregisterAll()` to clean up everything at once.

**Recommended shutdown pattern**:

```js
// In your plugin's shutdown() method:
ztoolkit.unregisterAll();
// or for selective tools:
keyboard.unregisterAll();
prompt.unregisterAll();
ui.unregisterAll();
```

---

## Cross-Version Compatibility Notes

The toolkit handles these automatically:

| Feature | Zotero 6 | Zotero 7+ |
| --- | --- | --- |
| XUL element creation | `createElementNS(xulNS, tag)` | `createXULElement(tag)` |
| Module import | `ChromeUtils.import()` | `ChromeUtils.importESModule()` |
| ES modules | Not supported | Supported |

Always use `tool.createXULElement(doc, tag)` instead of calling these directly.

---

## Resources

- **API docs**: <https://windingwind.github.io/zotero-plugin-toolkit/>
- **Plugin template**: <https://github.com/windingwind/zotero-plugin-template/>
- **TypeScript types**: install `zotero-types` package alongside
- **Local source**: `/Users/cboulanger/Code/zotero-plugin-toolkit/src/`
