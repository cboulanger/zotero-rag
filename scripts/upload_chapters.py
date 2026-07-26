#!/usr/bin/env python3
"""CLI for script 4: segment book PDFs into chapters and upload as new
bookSection items. Defaults to dry-run — pass --commit to actually write.

Usage:
    uv run python scripts/upload_chapters.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --input .local/analysis.json --commit
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyzotero import zotero

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_upload import run as upload_run
from backend.zotero.web_api import ZoteroWebAPI


async def _main(args: argparse.Namespace) -> int:
    library_type, numeric_id, _library_id = parse_library_slug(args.library_slug)
    write_client = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=args.api_key)
    read_client = ZoteroWebAPI(api_key=args.api_key)

    analyses = json.loads(Path(args.input).read_text(encoding="utf-8")).get("attachments", [])

    bar = tqdm(total=len(analyses) or 1, unit="book", desc="Uploading" if args.commit else "Previewing")

    result = await upload_run(
        zotero_write_client=write_client,
        zotero_read_client=read_client,
        slug=args.library_slug,
        analyses=analyses,
        commit=args.commit,
        confidence_threshold=args.confidence_threshold,
        target_collection=args.target_collection,
        max_items=args.max_items,
    )
    bar.update(len(analyses))
    bar.close()

    if not args.commit:
        print(f"DRY RUN: would create {len(result['would_create'])} chapter(s). Pass --commit to apply.")
    else:
        print(f"Created {len(result['created'])} chapter(s).")
    if result["skipped_low_confidence"]:
        print(f"Skipped {len(result['skipped_low_confidence'])} low-confidence chapter(s) — see output for detail.")

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Segment book PDFs into chapters and upload to Zotero.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Write-scoped Zotero API key")
    parser.add_argument("--input", required=True, help="Script 1's output JSON")
    parser.add_argument("--commit", action="store_true", help="Actually write to Zotero (default: dry-run preview)")
    # See backend/api/chapter_linking.py's SegmentUploadRequest for how this
    # default was calibrated against the real evaluation set.
    parser.add_argument("--confidence-threshold", type=float, default=0.90)
    parser.add_argument("--target-collection", default="Book Chapters")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
