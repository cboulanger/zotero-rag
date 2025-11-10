# Zotero Local API

Zotero provides a reasonably complete local implementation of the [Zotero API (api.zotero.org)](https://www.zotero.org/support/dev/web_api/v3/start).

Endpoints are accessible on the local server (localhost:23119 by default) under /api/.

Limitations compared to api.zotero.org:

- Only API version 3 (https://www.zotero.org/support/dev/web_api/v3/basics) is supported, and only
  one API version will ever be supported at a time. If a new API version is released and your
  client needs to maintain support for older versions, first query /api/ and read the
  Zotero-API-Version response header, then make requests conditionally.
- Write access is not yet supported.
- No authentication.
- No access to user data for users other than the local logged-in user. Use user ID 0 or the user's
  actual API user ID (https://www.zotero.org/settings/keys).
- Minimal access to metadata about groups.
- Atom is not supported.
- Item type/field endpoints (https://www.zotero.org/support/dev/web_api/v3/types_and_fields) will
  return localized names in the user's locale. The locale query parameter is not supported. The
  single exception is /api/creatorFields, which follows the web API's behavior in always returning
  results in English, *not* the user's locale.
- If your code relies on any undefined behavior or especially unusual corner cases in the web API,
  it'll probably work differently when using the local API. This implementation is primarily
  concerned with matching the web API's spec and secondarily with matching its observed behavior,
  but it does not make any attempt to replicate implementation details that your code might rely on.
  Sort orders might differ, quicksearch results will probably differ, and JSON you get from the
  local API is never going to be exactly identical to what you would get from the web API.

That said, there are benefits:

- Pagination is often unnecessary because the API doesn't mind sending you many megabytes of data
  at a time - nothing ever touches the network. For that reason, returned results are not limited
  by default (unlike in the web API, which has a default limit of 25 and will not return more than
  100 results at a time).
- For the same reason, no rate limits, and it's really fast.
- <userOrGroupPrefix>/searches/:searchKey/items returns the set of items matching a saved search
  (unlike in the web API, which doesn't support actually executing searches).

## Implementation Details and Gotchas (Learned from Practice)

### File Attachments: `file://` Redirects

**Critical Discovery:** When requesting attachment files via `/api/.../items/{key}/file`, the local API does **not** serve the file content directly. Instead, it returns an **HTTP 302 redirect** to a `file://` URL pointing to the local filesystem.

**Example:**
```
GET /api/groups/6297749/items/U6J8XV72/file
Response: 302 Found
Location: file:///C:/Users/username/Zotero/storage/U6J8XV72/document.pdf
```

**Implications:**
- Standard HTTP clients (like `aiohttp`, `requests`) **cannot follow `file://` URLs**
- You must detect the redirect and read the file from the filesystem yourself
- The `Location` header contains a properly URL-encoded path

**Implementation Pattern:**
```python
async with session.get(url, allow_redirects=False) as response:
    if response.status in (301, 302, 303, 307, 308):
        file_url = response.headers.get("Location")
        if file_url and file_url.startswith("file://"):
            from urllib.parse import unquote, urlparse
            parsed = urlparse(file_url)
            file_path = unquote(parsed.path)

            # Windows: Remove leading slash from /C:/... paths
            if file_path.startswith("/") and len(file_path) > 2 and file_path[2] == ":":
                file_path = file_path[1:]

            # Read directly from filesystem
            return Path(file_path).read_bytes()
```

**Why this matters:** This is undocumented and differs from web API behavior, where files would be streamed directly.

### Item Hierarchy: Parent-Child Relationships

**Discovery:** The local API returns **all items in a flat list**, including both parent items and their child attachments. The relationship is tracked via the `parentItem` field on child items, **not** via a `numChildren` field on parents.

**Behavior:**
- Calling `/api/groups/{id}/items` returns parents AND children in one list
- Parent items do NOT reliably have `numChildren` field populated
- Child items (attachments, notes) have a `parentItem` field with the parent's key
- Attachments without `parentItem` are standalone (rare)

**Example Response:**
```json
[
  {
    "data": {
      "key": "ABC123",
      "itemType": "journalArticle",
      "title": "Example Paper",
      "numChildren": 0  // May be 0 even when children exist!
    }
  },
  {
    "data": {
      "key": "DEF456",
      "itemType": "attachment",
      "contentType": "application/pdf",
      "parentItem": "ABC123"  // Links to parent
    }
  }
]
```

**Correct Pattern:**
```python
# DON'T rely on numChildren
if item["data"].get("numChildren", 0) > 0:  # ❌ Unreliable!
    children = await get_item_children(...)

# DO fetch children for all regular items
if item["data"]["itemType"] not in ["attachment", "note"]:
    # Always fetch children - API call is fast
    children = await client.get_item_children(
        library_id=library_id,
        item_key=item["data"]["key"],
        library_type=library_type
    )
```

**Why this matters:** Relying on `numChildren` will cause you to miss PDF attachments. Always fetch children for every regular item.

### Group Library Access

**Endpoint Structure:**
- User libraries: `/api/users/0/items` (0 = current user)
- Group libraries: `/api/groups/{groupID}/items` (use actual group ID)
- Getting group list: `/api/users/0/groups` returns all groups user belongs to

**Important:** The group library endpoint requires the **group ID** (numeric), not a user ID. Groups and users are separate namespaces.

**Example:**
```python
# Get all accessible libraries
user_libs = await get("/api/users/0/items?limit=1")  # User's personal library
groups = await get("/api/users/0/groups")            # All group libraries

# Access group library
group_id = "6297749"
items = await get(f"/api/groups/{group_id}/items")
```

### Performance Characteristics

**No Pagination Limits:**
- Default web API: 25 items, max 100
- Local API: **No default limit**, can return thousands of items
- Query params still work: `?limit=100&start=0`

**Speed:**
- Local API responses: Typically 10-500ms for large libraries
- No network latency, no rate limiting
- Safe to make hundreds of requests per second

**Recommendation:** For initial development, fetch all items without pagination. Add pagination only if memory becomes an issue.

### Child Items Endpoint

**Endpoint:** `/api/groups/{groupID}/items/{itemKey}/children`

**Returns:** Direct children only (not recursive)
- Attachments (PDFs, snapshots, etc.)
- Notes
- Does NOT include nested children

**Response Format:** Same as regular items list, each with `parentItem` field

### Common Pitfalls

1. **Missing `/api/` Prefix**
   - ❌ `/groups/{id}/items`
   - ✓ `/api/groups/{id}/items`

2. **Forgetting Windows Path Fix**
   - `file:///C:/path` → Need to strip leading `/`
   - Works on macOS/Linux without modification

3. **Assuming `numChildren` is Accurate**
   - Don't check this field to decide whether to fetch children
   - Always fetch children for non-attachment items

4. **Not Handling URL Encoding**
   - Filesystem paths in `file://` URLs are percent-encoded
   - Must use `urllib.parse.unquote()` before reading

### Testing Recommendations

**Environment Validation:**
```python
# Always check connectivity before running tests
response = requests.get("http://localhost:23119/connector/ping")
assert response.status_code == 200, "Zotero not running"

# Verify test library is synced
libraries = await client.list_libraries()
library_ids = [lib["id"] for lib in libraries]
assert "6297749" in library_ids, "Test library not synced"
```

**Test Data:**
- Use a dedicated test group (e.g., https://www.zotero.org/groups/6297749/test-rag-plugin)
- Include items with various attachment types
- Include items with multiple attachments
- Include standalone attachments (no parent)

### Discovered in Session: 2025-11-10

All findings validated through integration testing with real Zotero instance and test library containing 16 PDFs, successfully indexed into 397 text chunks.