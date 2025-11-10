"""
Diagnose test library to understand why items are being skipped.
"""
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.zotero.local_api import ZoteroLocalAPI


def safe_print(text):
    """Print text with ASCII fallback for Windows console."""
    try:
        print(text)
    except UnicodeEncodeError:
        # Replace problematic characters with ASCII equivalents
        ascii_text = text.encode('ascii', errors='replace').decode('ascii')
        print(ascii_text)


async def main():
    client = ZoteroLocalAPI()

    # Check connectivity
    safe_print("Checking Zotero connectivity...")
    connected = await client.check_connection()
    safe_print(f"Connected: {connected}\n")

    if not connected:
        safe_print("Cannot connect to Zotero. Is it running?")
        return

    # List libraries
    safe_print("Listing libraries...")
    libraries = await client.list_libraries()
    safe_print(f"Found {len(libraries)} libraries\n")

    # Find test library
    test_library_id = "6297749"
    test_library = None
    for lib in libraries:
        if lib["id"] == test_library_id:
            test_library = lib
            break

    if not test_library:
        safe_print(f"Test library {test_library_id} not found!")
        safe_print("Available libraries:")
        for lib in libraries[:10]:
            safe_print(f"  - {lib['id']}: {lib['name']} ({lib['type']})")
        return

    safe_print(f"Found test library: {test_library['name']}")
    safe_print(f"  ID: {test_library['id']}")
    safe_print(f"  Type: {test_library['type']}\n")

    # Get library items
    safe_print("Fetching library items...")
    items = await client.get_library_items(test_library_id, "group")
    safe_print(f"Found {len(items)} items\n")

    # Analyze items
    pdf_count = 0
    attachment_count = 0
    other_count = 0
    items_with_children = 0

    safe_print("Item breakdown:")
    for item in items:
        item_data = item.get("data", {})
        item_type = item_data.get("itemType", "unknown")
        item_key = item_data.get("key", "no-key")
        title = item_data.get("title", "(no title)")

        if item_type == "attachment":
            attachment_count += 1
            content_type = item_data.get("contentType", "unknown")
            filename = item_data.get("filename", "(no filename)")
            parent_item = item_data.get("parentItem", None)
            parent_info = f" (parent: {parent_item})" if parent_item else " (NO PARENT)"
            safe_print(f"  [ATTACHMENT] {filename} ({content_type}){parent_info}")

            if content_type == "application/pdf":
                pdf_count += 1
        else:
            other_count += 1
            safe_print(f"  [{item_type.upper()}] {title[:60]}")

            # Check for child attachments
            num_children = item_data.get("numChildren", 0)
            if num_children > 0:
                items_with_children += 1
                safe_print(f"    -> has {num_children} children")

    safe_print(f"\nSummary:")
    safe_print(f"  Top-level PDFs: {pdf_count}")
    safe_print(f"  Other attachments: {attachment_count - pdf_count}")
    safe_print(f"  Regular items: {other_count}")
    safe_print(f"  Items with children: {items_with_children}")
    safe_print(f"  Total: {len(items)}")

    # Get items with children
    safe_print("\n\nChecking items with children in detail...")
    for item in items:
        item_data = item.get("data", {})
        item_type = item_data.get("itemType", "unknown")
        num_children = item_data.get("numChildren", 0)

        if num_children > 0 and item_type != "attachment":
            item_key = item_data.get("key")
            title = item_data.get("title", "(no title)")

            safe_print(f"\n{title[:60]}")
            safe_print(f"  Key: {item_key}")
            safe_print(f"  Type: {item_type}")
            safe_print(f"  Children: {num_children}")

            # Try to get children
            try:
                children = await client.get_item_children(test_library_id, item_key, "group")
                safe_print(f"  Fetched {len(children)} child items:")
                for child in children:
                    child_data = child.get("data", {})
                    child_type = child_data.get("itemType", "unknown")
                    child_content = child_data.get("contentType", "unknown")
                    child_filename = child_data.get("filename", "(no filename)")
                    safe_print(f"    - {child_type}: {child_filename} ({child_content})")
            except Exception as e:
                safe_print(f"  Error fetching children: {e}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
