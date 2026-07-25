#!/usr/bin/env python3
"""CLI for script 3: retrofit-link existing, separately-catalogued book and
bookSection items.

Usage:
    uv run python scripts/retrofit_chapter_links.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --output .local/retrofit.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyzotero import zotero

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_retrofit import run as retrofit_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrofit-link existing book/bookSection pairs.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Write-scoped Zotero API key")
    parser.add_argument("--item-keys", default=None, help="Comma-separated bookSection item keys to restrict to")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    library_type, numeric_id, _library_id = parse_library_slug(args.library_slug)
    zot = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None
    result = retrofit_run(
        zotero_write_client=zot,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
    )

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote {len(result['linked'])} link(s), {len(result['ambiguous'])} ambiguous, {len(result['no_match'])} unmatched to {args.output}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
