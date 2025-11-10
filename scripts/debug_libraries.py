"""
Debug script to show the actual library data structure from Zotero local API.
"""

import asyncio
import sys
import json
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.zotero.local_api import ZoteroLocalAPI


async def main():
    """Show detailed library information."""
    print("=" * 70)
    print("Zotero Library Data Structure Debug")
    print("=" * 70)

    client = ZoteroLocalAPI()

    try:
        libraries = await client.list_libraries()

        if not libraries:
            print("\nNo libraries found!")
            return

        print(f"\nFound {len(libraries)} library/libraries:\n")

        for i, lib in enumerate(libraries, 1):
            print(f"Library {i}:")
            print(json.dumps(lib, indent=2))
            print("-" * 70)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
