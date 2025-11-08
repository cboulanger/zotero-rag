# Phase 3: Zotero Plugin - Implementation Progress

## Overview

This document tracks the implementation progress of Phase 3 (Zotero Plugin) from the master implementation plan.

## Completed Steps

### 12. Plugin Scaffold ✅

**Files Created:**

- [plugin/manifest.json](../plugin/manifest.json) - Zotero 7+ compatible plugin manifest
- [plugin/src/bootstrap.js](../plugin/src/bootstrap.js) - Plugin lifecycle hooks
- [scripts/build-plugin.js](../scripts/build-plugin.js) - XPI build script

**Directory Structure:**

```
plugin/
├── src/              # Plugin source files
├── locale/           # Localization files
│   └── en-US/
├── build/            # Temporary build directory (generated)
├── dist/             # XPI output directory (generated)
└── README.md         # Plugin documentation
```

**Key Features:**

- Manifest configured for Zotero 7+ (no max version restriction)
- Bootstrap.js implements all lifecycle hooks: install, startup, shutdown, uninstall
- Preferences pane registration
- Main window load/unload handlers

**Build Process:**

- npm script: `npm run plugin:build`
- Creates XPI archive from plugin source
- Output: `plugin/dist/zotero-rag-{version}.xpi`
- Automated file copying and ZIP creation

**Tests:** Build script tested successfully ✅

---

### 13. UI Implementation ✅

**Files Created:**

- [plugin/src/dialog.xhtml](../plugin/src/dialog.xhtml) - Query dialog UI
- [plugin/src/dialog.js](../plugin/src/dialog.js) - Dialog logic
- [plugin/locale/en-US/zotero-rag.ftl](../plugin/locale/en-US/zotero-rag.ftl) - Localization strings

**UI Components:**

1. **Query Dialog** ([dialog.xhtml](../plugin/src/dialog.xhtml)):
   - Question text area (multi-line input)
   - Library selection list with checkboxes
   - Progress bar (hidden by default)
   - Status messages area
   - Accept/Cancel buttons

2. **Localization** ([zotero-rag.ftl](../plugin/locale/en-US/zotero-rag.ftl)):
   - Fluent localization format
   - English strings (extensible to other languages)

**Dialog Features:**

- Modal dialog with resizable dimensions (600x500px)
- Accessible form controls
- Progress tracking UI (shown during indexing)
- Status message display with color coding
- Validation before submission

---

### 14. Menu Integration ✅

**Implementation Location:** [plugin/src/zotero-rag.js:40-53](../plugin/src/zotero-rag.js#L40-L53)

**Key Features:**

- Menu item added to Tools menu: "Ask Question..."
- Uses Fluent localization system
- Command handler opens query dialog
- Proper cleanup on plugin unload

**Integration Points:**

- `addToWindow()`: Adds menu item to each Zotero window
- `removeFromWindow()`: Cleans up menu item on unload
- Element ID tracking for proper cleanup

---

### 15. Backend Communication ✅

**Implementation Location:** [plugin/src/zotero-rag.js](../plugin/src/zotero-rag.js)

**Key Features:**

1. **Version Checking** ([zotero-rag.js:97-111](../plugin/src/zotero-rag.js#L97-L111)):
   - Checks backend version on plugin startup
   - Endpoint: `GET /api/version`
   - Logs version for debugging
   - Gracefully handles offline backend

2. **Query Submission** ([zotero-rag.js:186-218](../plugin/src/zotero-rag.js#L186-L218)):
   - Endpoint: `POST /api/query`
   - Backend availability check with 3-second timeout
   - JSON request/response handling
   - Clear error messages for offline backend

3. **Error Handling:**
   - Immediate failure when backend unavailable
   - HTTP error status handling
   - User-friendly error messages
   - Timeout management (3s for health check)

4. **Concurrent Query Management:**
   - Active query tracking using Set
   - Configurable max concurrent queries (default: 5)
   - Query ID generation using timestamps
   - Automatic cleanup on completion/error

**Configuration:**

- Backend URL stored in preferences: `extensions.zotero-rag.backendURL`
- Default: `http://localhost:8119`
- Loaded on plugin initialization

---

### 16. Library Selection Logic ✅

**Implementation Location:** [plugin/src/zotero-rag.js:151-184](../plugin/src/zotero-rag.js#L151-L184)

**Key Features:**

1. **Get All Libraries** ([zotero-rag.js:151-175](../plugin/src/zotero-rag.js#L151-L175)):
   - Retrieves user library
   - Gets all group libraries
   - Returns structured data: `{ id, name, type }`

2. **Get Current Library** ([zotero-rag.js:177-184](../plugin/src/zotero-rag.js#L177-L184)):
   - Detects currently selected library in Zotero pane
   - Returns library ID as string
   - Handles null case (no selection)

3. **Dialog Integration** ([dialog.js:15-39](../plugin/src/dialog.js#L15-L39)):
   - Populates library list with checkboxes
   - Pre-checks current library
   - Tracks selected libraries in Set
   - Validates at least one library selected

---

### 17. Indexing Progress UI ✅

**Implementation Location:** [plugin/src/dialog.js:88-164](../plugin/src/dialog.js#L88-L164)

**Key Features:**

1. **Progress Monitoring via SSE** ([dialog.js:112-164](../plugin/src/dialog.js#L112-L164)):
   - EventSource connection to backend
   - Endpoint: `GET /api/index/library/{library_id}/progress`
   - Real-time progress updates
   - Automatic cleanup on completion/error

2. **Event Handling:**
   - `started`: Initialize progress bar
   - `progress`: Update percentage and document count
   - `completed`: Mark indexing complete
   - `error`: Display error message

3. **Progress Display** ([dialog.js:166-180](../plugin/src/dialog.js#L166-L180)):
   - Progress bar with percentage
   - Current/total document count
   - Status messages
   - Auto-scroll to latest status

4. **Library Status Checking** ([dialog.js:88-110](../plugin/src/dialog.js#L88-L110)):
   - Checks if library is already indexed
   - Triggers indexing for unindexed libraries
   - Monitors multiple libraries sequentially

**Timeout Handling:**

- 5-minute timeout per library
- Graceful fallback if SSE fails
- Connection error handling

---

### 18. Note Creation ✅

**Implementation Location:** [plugin/src/zotero-rag.js:220-284](../plugin/src/zotero-rag.js#L220-L284)

**Key Features:**

1. **Note Creation** ([zotero-rag.js:220-248](../plugin/src/zotero-rag.js#L220-L248)):
   - Creates standalone note in current library
   - Adds note to current collection (if selected)
   - Uses Zotero.Item API
   - Transaction-based saving

2. **HTML Formatting** ([zotero-rag.js:250-280](../plugin/src/zotero-rag.js#L250-L280)):
   - Question as H2 heading
   - Answer in paragraph
   - Source citations as bulleted list
   - Metadata footer (timestamp, libraries)

3. **Citations:**
   - Zotero links: `zotero://select/library/items/{item_id}`
   - Page numbers when available (e.g., "Source, p. 42")
   - Text anchors as fallback (first 5 words)
   - Clickable links to source PDFs

4. **HTML Safety:**
   - HTML escaping for user input ([zotero-rag.js:282-284](../plugin/src/zotero-rag.js#L282-L284))
   - Prevents XSS vulnerabilities
   - Uses DOM-based escaping

**Note Metadata:**

- Timestamp in locale format
- List of searched libraries
- Styled metadata section

---

### 19. Plugin Testing ⏳ (Manual Testing Required)

**Status:** Plugin builds successfully, manual testing in Zotero required

**Build Verification:** ✅

- XPI file created: `plugin/dist/zotero-rag-0.1.0.xpi`
- All files included in archive
- Build script completes without errors

**Testing Checklist:**

- [ ] Install plugin in Zotero 7/8
- [ ] Verify preferences pane appears
- [ ] Configure backend URL
- [ ] Open "Ask Question" dialog from Tools menu
- [ ] Select multiple libraries
- [ ] Submit query with backend running
- [ ] Verify indexing progress updates
- [ ] Check note creation in collection
- [ ] Verify note formatting and citations
- [ ] Test version compatibility checking
- [ ] Test concurrent query limits
- [ ] Test error scenarios:
  - [ ] Backend offline
  - [ ] Empty question
  - [ ] No libraries selected
  - [ ] No results found

**Installation Instructions:**

1. Build plugin: `npm run plugin:build`
2. Open Zotero 7 or 8
3. Go to Tools > Add-ons
4. Click gear icon > Install Add-on From File
5. Select `plugin/dist/zotero-rag-0.1.0.xpi`
6. Restart Zotero

---

## Implementation Notes

### Plugin Architecture

The plugin follows Zotero's bootstrapped extension model:

1. **bootstrap.js**: Minimal lifecycle management
2. **zotero-rag.js**: Main plugin logic (global object)
3. **dialog.js**: Dialog-specific logic (separate context)
4. **preferences.js**: Preferences pane logic

### Communication Flow

```
User → Menu Click → Dialog Open → Library Selection → Query Submit
  ↓
Check Library Status → Index if Needed (with SSE progress) → Submit Query
  ↓
Receive Answer → Create Note → Display Success
```

### Preferences System

- Preferences stored in Zotero's pref system
- Namespace: `extensions.zotero-rag.*`
- Settings:
  - `backendURL`: Backend server URL
  - `maxQueries`: Max concurrent queries

### Backend Integration

- **REST API**: Query submission, library status, configuration
- **SSE**: Real-time indexing progress updates
- **Timeout**: 3s for health checks, 5min for indexing

### Security Considerations

- HTML escaping prevents XSS in note content
- Backend URL validation in preferences
- HTTPS support (if backend configured)
- Local-only deployment assumed (no remote auth)

---

## File Summary

**Total Files Created:** 12

| Category | Files | Status |
|----------|-------|--------|
| Plugin Core | 3 | ✅ Complete |
| UI Components | 4 | ✅ Complete |
| Localization | 1 | ✅ Complete |
| Build System | 1 | ✅ Complete |
| Documentation | 1 | ✅ Complete |
| Package Config | 1 | ✅ Updated |

**Lines of Code:**

- JavaScript: ~600 lines
- XUL/XHTML: ~100 lines
- Build scripts: ~150 lines
- Documentation: ~200 lines

---

## Phase 3 Status: COMPLETE ✅ (Pending Manual Testing)

All Phase 3 implementation steps have been completed:

✅ **Step 12:** Plugin Scaffold - Manifest, bootstrap, build process
✅ **Step 13:** UI Implementation - Dialog, localization
✅ **Step 14:** Menu Integration - Tools menu item
✅ **Step 15:** Backend Communication - HTTP, SSE, error handling
✅ **Step 16:** Library Selection Logic - Library listing and selection
✅ **Step 17:** Indexing Progress UI - SSE streaming, progress bar
✅ **Step 18:** Note Creation - HTML formatting, citations
⏳ **Step 19:** Plugin Testing - Requires Zotero 7/8 installation

**Key Achievements:**

1. Complete Zotero plugin with all planned features
2. XPI build process with npm integration
3. Real-time indexing progress via SSE
4. Smart note creation with citations and page numbers
5. Preferences UI for configuration
6. Comprehensive error handling
7. Concurrent query management

**Installation:**

```bash
npm run plugin:build
# Then install plugin/dist/zotero-rag-0.1.0.xpi in Zotero
```

**Next Steps:**

- **Option A:** Manual testing in Zotero 7/8 (requires Zotero installation)
- **Option B:** Complete stub implementations in backend (Document Processing, LLM, RAG)
- **Option C:** End-to-end integration testing with real Zotero library

**Recommended:** Test the plugin manually in Zotero, then complete the backend stub implementations for full functionality.

---

## HTML Refactoring (Post-Implementation Update)

After initial implementation, the plugin UI was refactored from XUL to modern HTML for better future-proofing and compatibility with Zotero 7/8+.

### Changes Made:

1. **Dialog UI** ([dialog.xhtml](../plugin/src/dialog.xhtml)):
   - Converted from XUL `<dialog>` to HTML `<html>` with proper DOCTYPE
   - Replaced XUL elements: `<vbox>`, `<hbox>`, `<groupbox>` → HTML `<div>`
   - Replaced XUL `<label>` → HTML `<label>`
   - Replaced XUL checkboxes → HTML `<input type="checkbox">`
   - Added custom CSS for layout and styling

2. **Preferences UI** ([preferences.xhtml](../plugin/src/preferences.xhtml)):
   - Full HTML conversion with `<fieldset>` and `<legend>`
   - Standard HTML form inputs
   - CSS-based layout using flexbox

3. **JavaScript Updates**:
   - [dialog.js](../plugin/src/dialog.js): Updated to use `document.createElement()` instead of `createXULElement()`
   - Changed from XUL checkbox events (`command`) to HTML events (`change`)
   - Added button event listeners for submit/cancel

4. **CSS Styling** (New files):
   - [dialog.css](../plugin/src/dialog.css): Complete dialog styling with modern CSS
   - [preferences.css](../plugin/src/preferences.css): Preferences pane styling

5. **Menu Integration**:
   - Menu items still use `createXULElement('menuitem')` as they integrate with Zotero's existing XUL menu system (acceptable exception)

### Benefits:

- **Future-proof**: Compatible with Zotero 8+ which continues to deprecate XUL
- **Standards-compliant**: Uses standard HTML5 and CSS3
- **Easier maintenance**: Standard web technologies vs. legacy XUL
- **Better styling**: Full control with CSS instead of XUL layout attributes

### Build Verification:

✅ Plugin builds successfully with HTML refactoring
✅ All files included in XPI archive
✅ No XUL dependency in dialog/preferences (except menu items)

---

## Known Limitations

1. **No automated tests**: Plugin testing requires manual interaction with Zotero
2. **SSE limitations**: In-memory job tracking (not persistent across restarts)
3. **Single language**: Only English localization currently
4. **No keyboard shortcuts**: Menu-based access only
5. **No plugin updates**: Update mechanism not implemented
6. **Menu items use XUL**: Menu integration still uses `createXULElement` (acceptable for now)

## Future Enhancements

1. Add keyboard shortcut for quick access
2. Implement auto-update mechanism
3. Add more localization languages
4. Create collection-specific indexing
5. Add query history feature
6. Implement result caching
7. Add export functionality for answers
