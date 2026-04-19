"""
Quick script to check Zotero connectivity and list available libraries.

This helps verify Zotero is running and shows what libraries are synced.
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.zotero.local_api import ZoteroLocalAPI


async def main():
    """Check Zotero connectivity and list libraries."""
    print("=" * 70)
    print("Zotero Connectivity Check")
    print("=" * 70)

    client = ZoteroLocalAPI()

    # Check connection
    print("\n1. Checking Zotero connection...")
    try:
        is_connected = await client.check_connection()
        if is_connected:
            print("   [PASS] Zotero is running and accessible")
        else:
            print("   [FAIL] Zotero is not responding")
            return
    except Exception as e:
        print(f"   [FAIL] Error connecting to Zotero: {e}")
        return

    # List libraries
    print("\n2. Listing available libraries...")
    try:
        libraries = await client.list_libraries()

        if not libraries:
            print("   [WARN] No libraries found")
            print("   -> Make sure you have synced at least one library in Zotero")
            return

        print(f"   Found {len(libraries)} library/libraries:\n")

        for lib in libraries:
            lib_id = lib.get("id", "unknown")
            lib_name = lib.get("name", "Unnamed")
            lib_type = lib.get("type", "unknown")

            print(f"   - {lib_name}")
            print(f"     ID: {lib_id}")
            print(f"     Type: {lib_type}")
            print()

        # Check for test library
        print("3. Checking for test library...")
        test_lib_id = "6297749"
        lib_ids = [lib["id"] for lib in libraries]

        if test_lib_id in lib_ids:
            print(f"   [PASS] Test library {test_lib_id} is synced!")
            print("   -> Integration tests can now run")
        else:
            print(f"   [FAIL] Test library {test_lib_id} not found")
            print("   -> Please sync the test group in Zotero:")
            print("      1. Visit: https://www.zotero.org/groups/6297749/test-rag-plugin")
            print("      2. Join the group (if not already a member)")
            print("      3. Open Zotero desktop")
            print("      4. Click the green sync button (or press Ctrl+S / Cmd+S)")
            print("      5. Wait for the group library to appear")
            print("      6. Run this script again to verify")

    except Exception as e:
        print(f"   [FAIL] Error listing libraries: {e}")
        return

    print("\n" + "=" * 70)
    print("Check complete!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
