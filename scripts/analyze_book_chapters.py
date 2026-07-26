#!/usr/bin/env python3
"""CLI for script 1: analyze book PDFs for chapter-segmentation candidates.

Usage:
    uv run python scripts/analyze_book_chapters.py --library-slug groups/6297749 \
        --api-key <read-only-zotero-key> --output .local/analysis.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_segmentation import run as analyze_run
from backend.zotero.web_api import ZoteroWebAPI


async def _main(args: argparse.Namespace) -> int:
    library_type, _numeric_id, library_id = parse_library_slug(args.library_slug)
    client = ZoteroWebAPI(api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None

    llm_service = None
    if args.llm_fallback:
        from backend.dependencies import make_llm_service
        llm_service = make_llm_service(auto_select_model=args.auto_select_model)

    bar = tqdm(total=100, unit="%", desc="Analyzing")

    def on_progress(progress: float, message: str) -> None:
        bar.n = int(progress * 100)
        bar.set_description(message)
        bar.refresh()

    result = await analyze_run(
        zotero_client=client,
        library_id=library_id,
        library_type=library_type,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
        relink=args.relink,
        progress_callback=on_progress,
        llm_service=llm_service,
        ocr_cache_dir=Path(args.cache_dir),
    )
    bar.close()

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote analysis to {args.output}")
    else:
        print(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze book PDFs for chapter-segmentation candidates.")
    parser.add_argument("--library-slug", required=True, help="e.g. groups/6297749 or users/12345")
    parser.add_argument("--api-key", required=True, help="Read-only Zotero API key")
    parser.add_argument("--item-keys", default=None, help="Comma-separated list to restrict to specific book items")
    parser.add_argument("--relink", action="store_true", help="Re-analyze books that already have X-Contains")
    parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help="Enable the LLM-based fallback for chapters the heuristic pass finds "
             "nothing or is ambiguous about (slower, calls a configured LLM API)",
    )
    parser.add_argument(
        "--auto-select-model",
        action="store_true",
        help="With --llm-fallback, retry across the active preset's available models "
             "(most-available first) on error or an unusable response, instead of "
             "using a single fixed model. Never a hardcoded model name -- resolved "
             "live from the preset/provider at run time.",
    )
    parser.add_argument("--max-items", type=int, default=None, help="Cap the number of book items processed (testing/debugging)")
    parser.add_argument(
        "--cache-dir",
        default="data/ocr_cache",
        help="Directory to check for already-OCR'd page text (script 2's cache) for "
             "scanned PDFs with no text layer -- same default as ocr_attachments.py's "
             "--cache-dir, so re-running this script after OCR-ing a book picks it up "
             "automatically",
    )
    parser.add_argument("--output", default=None, help="Write JSON output to this path instead of stdout")
    args = parser.parse_args()
    if args.auto_select_model and not args.llm_fallback:
        parser.error("--auto-select-model has no effect without --llm-fallback")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
