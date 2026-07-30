#!/usr/bin/env python3
"""CLI for script 3: retrofit-link existing, separately-catalogued book and
bookSection items. Defaults to dry-run -- pass --commit to actually write.

Usage:
    uv run python scripts/retrofit_chapter_links.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --output .local/retrofit.json --commit

    # Replay a prior dry run's matches instead of re-fetching/re-matching
    # the whole library (much faster for a large library):
    uv run python scripts/retrofit_chapter_links.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --input .local/retrofit.json --commit
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
    parser.add_argument("--commit", action="store_true", help="Actually write to Zotero (default: dry-run preview)")
    parser.add_argument(
        "--input", default=None,
        help="A prior dry run's --output JSON. Replays its would_link matches into the "
             "commit pass instead of re-fetching and re-matching the whole library. Only "
             "valid together with --commit.",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.input and not args.commit:
        parser.error("--input requires --commit (it replays a prior dry run's matches into a commit pass)")

    library_type, numeric_id, _library_id = parse_library_slug(args.library_slug)
    zot = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None
    would_link = json.loads(Path(args.input).read_text(encoding="utf-8")).get("would_link") if args.input else None

    result = retrofit_run(
        zotero_write_client=zot,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
        commit=args.commit,
        would_link=would_link,
    )

    if not args.commit:
        print(f"DRY RUN: would link {len(result['would_link'])} chapter(s). Pass --commit to apply.")
    else:
        print(f"Wrote {len(result['linked'])} link(s).")
    print(f"{len(result['ambiguous'])} ambiguous, {len(result['no_match'])} unmatched.")

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Wrote full result to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
