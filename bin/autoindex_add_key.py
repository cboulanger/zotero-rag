"""Onboard a read-only Zotero key for auto-indexing without the plugin.

Usage:
    uv run python bin/autoindex_add_key.py <read-only-key>

Validates the key (must be read-only), resolves its target libraries, and stores
it encrypted in the auto-index key store. Requires AUTOINDEX_SECRET to be set.
"""

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


async def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: uv run python bin/autoindex_add_key.py <read-only-key>")
        return 2
    api_key = argv[0].strip()

    from backend.config.settings import get_settings
    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.zotero.key_validator import validate_key

    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        print("[FAIL] AUTOINDEX_SECRET is not set; cannot store keys.")
        return 1

    validation = await validate_key(api_key)
    if not validation.read_only:
        print(f"[FAIL] {validation.reason}")
        return 1

    fp = store.add(api_key, validation)
    print(f"[PASS] Stored key {fp} for {validation.username} "
          f"({validation.user_id}) -> {', '.join(validation.targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
