#!/usr/bin/env python3
"""CLI for script 2: OCR attachments lacking a text layer.

Usage:
    uv run python scripts/ocr_attachments.py --library-slug groups/6297749 \
        --api-key <read-only-zotero-key> --input .local/analysis.json \
        --output .local/ocr_results.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_ocr import run as ocr_run
from backend.services.extraction import create_document_extractor
from backend.zotero.web_api import ZoteroWebAPI


def _load_attachment_specs(input_path: str) -> list[dict]:
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    return [
        {"item_key": a["item_key"], "attachment_key": a["attachment_key"]}
        for a in data.get("attachments", [])
        if a.get("needs_ocr")
    ]


async def _main(args: argparse.Namespace) -> int:
    library_type, _numeric_id, library_id = parse_library_slug(args.library_slug)
    client = ZoteroWebAPI(api_key=args.api_key)
    extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
    specs = _load_attachment_specs(args.input)

    bar = tqdm(total=100, unit="%", desc="OCR-ing")

    def on_progress(progress: float, message: str) -> None:
        bar.n = int(progress * 100)
        bar.set_description(message)
        bar.refresh()

    result = await ocr_run(
        zotero_client=client,
        extractor=extractor,
        library_id=library_id,
        library_type=library_type,
        attachment_specs=specs,
        max_items=args.max_items,
        cache_dir=Path(args.cache_dir),
        progress_callback=on_progress,
    )
    bar.close()

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote OCR results to {args.output}")
    else:
        print(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR attachments lacking a text layer.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Read-only Zotero API key")
    parser.add_argument("--input", required=True, help="Script 1's output JSON (needs_ocr items are used)")
    parser.add_argument("--cache-dir", default="data/ocr_cache")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
