"""Decrypt stored auto-index API keys for live debugging.

Reads the same encrypted key store the cron indexer uses
(``<data_path>/system/autoindex_keys.json``, decrypted with ``AUTOINDEX_SECRET``)
and prints a key to stdout — useful for authenticating ad-hoc requests (e.g.
via ``curl`` or ``scripts/query_trace.py``) against a locally running backend
without needing to open Zotero or the plugin UI. The store holds two kinds
of key per entry: the read-only Zotero API key itself (used for the
X-Zotero-API-Key identity header), and — if the user has ever submitted one
via the plugin's Preferences — an embedding/LLM provider key (e.g.
KISSKI_API_KEY, used for that preset's X-Kisski-Api-Key header).

Usage:
    uv run python bin/debug_get_zotero_key.py                    # print the first Zotero key
    uv run python bin/debug_get_zotero_key.py --list              # list all entries, no key values
    uv run python bin/debug_get_zotero_key.py --user 3866263      # Zotero key for a specific user id
    uv run python bin/debug_get_zotero_key.py --embedding-key     # the stored provider key instead (e.g. KISSKI_API_KEY)

Requires AUTOINDEX_SECRET to be set (see .env) and at least one key already
stored (added via the plugin's Preferences, or `bin/autoindex_add_key.py`).
"""

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--list", action="store_true",
        help="List stored keys (fingerprint, user id, targets) without printing key values",
    )
    parser.add_argument(
        "--user", metavar="ZOTERO_USER_ID",
        help="Print the key belonging to this Zotero user id (default: the first available key)",
    )
    parser.add_argument(
        "--embedding-key", action="store_true",
        help="Print the stored embedding/LLM provider key (e.g. KISSKI_API_KEY) instead "
             "of the Zotero API key. Prints 'NAME=value' so the key's env var name is visible.",
    )
    args = parser.parse_args(argv)

    from backend.config.settings import get_settings
    from backend.services.autoindex_key_store import AutoIndexKeyStore

    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        print("[FAIL] AUTOINDEX_SECRET is not set; no keys can be decrypted.", file=sys.stderr)
        return 1

    entries = list(store.iter_decrypted())
    if not entries:
        print("[FAIL] No keys stored in the auto-index key store.", file=sys.stderr)
        return 1

    if args.list:
        for fp, _key, entry in entries:
            print(f"{fp}  user_id={entry.get('user_id')}  targets={entry.get('targets')}")
        return 0

    for fp, key, entry in entries:
        if args.user is not None and str(entry.get("user_id")) != str(args.user):
            continue
        if args.embedding_key:
            result = store.get_decrypted_embedding_key(fp)
            if result is None:
                continue
            key_name, key_value = result
            print(f"{key_name}={key_value}")
            return 0
        print(key)
        return 0

    what = "embedding key" if args.embedding_key else "key"
    if args.user is not None:
        print(f"[FAIL] No stored {what} for Zotero user id {args.user!r}.", file=sys.stderr)
    else:
        print(f"[FAIL] No stored {what} found.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
