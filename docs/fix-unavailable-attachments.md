# Fix Unavailable Attachments

If Zotero sync is incomplete, some attachment files may be missing locally
even though the metadata exists. The plugin detects this and shows a warning
badge (e.g. **⚠ 3**) in the Zotero toolbar, or a message "x unavailable" in
the list of libraries after indexing.

<img src="./images/fix-attachments-tool.png" width="300" alt="Screenshot of the fix attachment tool">

Click on the badge or on that message to open the **Fix Unavailable
Attachments** dialog, which lists all affected items in the current library.

## Recovery strategies

For each missing file the tool tries the following strategies in order:

1. **Zotero sync download** — triggers the normal Zotero file sync for that
   attachment.
2. **Filename match** — searches all other libraries for an attachment with
   the same filename.
3. **MD5 hash match** — searches by the file's stored sync hash
   (`storageHash`).
4. **`owl:sameAs` relations** — follows cross-library item relations to find
   the same file elsewhere.
5. **Direct URL download** — downloads from the attachment's stored URL using
   Zotero's proxy-aware HTTP client.
6. **DOI / Open Access resolver** — uses Zotero's built-in file resolvers
   (Unpaywall, etc.) to locate a freely available copy.

When a file is found it is copied into the correct Zotero storage directory.
Items that cannot be recovered can be deleted permanently from the dialog
using the **Delete Selected** button.
