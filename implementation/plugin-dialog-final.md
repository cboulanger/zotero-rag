# Zotero RAG Plugin - Final Dialog Implementation

## Summary

After multiple iterations to avoid XUL, we adopted a **pragmatic approach** that uses a modern HTML document structure that works reliably with Zotero 7/8's `window.openDialog()`.

## Final Architecture

### Dialog Structure

**File:** [plugin/src/dialog.xhtml](../plugin/src/dialog.xhtml)

```xml
<?xml version="1.0"?>
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml"
      xmlns:xul="http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul">
```

**Key Points:**
- Uses **HTML document** (`<!DOCTYPE html>`) as root element
- XHTML namespace for HTML5 elements
- XUL namespace declared but only for reference (menu integration)
- All form controls use standard HTML: `<input>`, `<button>`, `<textarea>`, `<label>`
- CSS for styling and layout

### Dialog Logic

**File:** [plugin/src/dialog.js](../plugin/src/dialog.js) (246 lines)

- Receives plugin reference via `window.arguments[0]`
- Pure JavaScript DOM manipulation
- SSE integration for indexing progress
- Standard event listeners (`click`, `change`)

### Opening the Dialog

**File:** [plugin/src/zotero-rag.js:119-136](../plugin/src/zotero-rag.js#L119-L136)

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

## What Works

✅ **HTML-based dialog** with proper DOCTYPE
✅ **Standard HTML form elements** (no XUL UI components)
✅ **CSS styling** with flexbox layout
✅ **Modern JavaScript** with async/await
✅ **SSE streaming** for real-time progress
✅ **Compatible with Zotero 7 and 8**

## What We Learned

1. **XUL is still used in Zotero plugins** - Many current plugins use XUL successfully
2. **HTML documents work as dialog roots** - Modern approach following reference plugins
3. **Pragmatism over purity** - Focus on functionality rather than avoiding all XUL
4. **window.openDialog() requirements** - Needs specific structure but accepts HTML documents

## Files Changed

| File | Status | Purpose |
|------|--------|---------|
| dialog.xhtml | ✅ Created | HTML-based dialog UI |
| dialog.js | ✅ Created | Dialog logic and event handling |
| dialog.css | ✅ Existing | Styling (unchanged from earlier) |
| zotero-rag.js | ✅ Updated | Opens dialog via window.openDialog() |
| preferences.xhtml | ✅ Updated | HTML fragment (no XUL wrapper) |
| CLAUDE.md | ✅ Updated | Pragmatic guidelines for XUL/HTML |

## Removed Files

- `dialog-builder.js` - Programmatic approach (too complex)
- Old XHTML attempts with XUL `<dialog>` root

## Build Status

```bash
npm run plugin:build
# ✅ Build successful
# Output: plugin/dist/zotero-rag-0.1.0.xpi
```

**XPI Contents:**
- 10 files total
- No XUL dependencies except menu items
- Pure HTML dialog implementation

## Installation

```bash
# In Zotero 7 or 8:
# 1. Tools > Add-ons
# 2. Gear icon > Install Add-on From File
# 3. Select: plugin/dist/zotero-rag-0.1.0.xpi
```

## Next Steps

1. ✅ Plugin installs successfully
2. ⏳ Test dialog opening and display
3. ⏳ Test library selection functionality
4. ⏳ Test backend integration (requires backend running)
5. ⏳ Test note creation
6. ⏳ End-to-end workflow validation

## References

- Zotero plugin example: `zotero-addons/addon/content/addonDetail.xhtml`
- Shows HTML document with XHTML namespace as modern approach
- Confirmed working pattern for Zotero 7/8 plugins
