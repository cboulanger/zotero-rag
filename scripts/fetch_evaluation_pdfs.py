#!/usr/bin/env python3
"""Download the open-access book-segmentation evaluation PDFs on demand.

The PDFs themselves are gitignored (backend/evaluation/book-segmentation/*.pdf)
so they aren't shipped in the repo. This script reads
backend/evaluation/book-segmentation/manifest.json (the single source of
truth for this evaluation set — see that directory's README) and downloads
each entry with "oa": true into that same directory if not already present.

Non-OA books ("oa": false) are perfectly welcome in the manifest — they just
can't be auto-downloaded. If one is missing locally, this script prints its
DOI and the exact path to save it to, so you can fetch it manually through
your institution's legal access and drop it in yourself.

Usage:
    uv run python scripts/fetch_evaluation_pdfs.py
    uv run python scripts/fetch_evaluation_pdfs.py --force   # re-download even if present
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

_EVAL_DIR = Path(__file__).resolve().parent.parent / "backend" / "evaluation" / "book-segmentation"


def fetch_all(eval_dir: Path, force: bool) -> int:
    manifest = json.loads((eval_dir / "manifest.json").read_text(encoding="utf-8"))
    missing_non_oa = []

    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for book in manifest["books"]:
            target = eval_dir / book["filename"]
            if not book["oa"]:
                if not target.exists():
                    missing_non_oa.append(book)
                continue
            if target.exists() and not force:
                print(f"[skip] {book['filename']} already present")
                continue
            print(f"[fetch] {book['filename']} <- {book['download_url']}")
            response = client.get(book["download_url"])
            response.raise_for_status()
            target.write_bytes(response.content)
            print(f"        wrote {len(response.content):,} bytes")

    if missing_non_oa:
        print("\nNot auto-downloaded (OA: No — legally cannot be fetched automatically).")
        print("Download each one manually via your institution's access to the DOI below,")
        print(f"then place it at the path shown, and re-run this script (or just run the tests):\n")
        for book in missing_non_oa:
            doi = book.get("doi")
            doi_url = f"https://doi.org/{doi}" if doi else "(no DOI on file — see manifest.json)"
            print(f"  - {book['filename']}")
            print(f"      DOI: {doi_url}")
            print(f"      Save to: {eval_dir / book['filename']}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eval-dir", default=str(_EVAL_DIR), help="Directory containing manifest.json and the PDFs")
    parser.add_argument("--force", action="store_true", help="Re-download even if the file already exists")
    args = parser.parse_args()
    return fetch_all(Path(args.eval_dir), args.force)


if __name__ == "__main__":
    raise SystemExit(main())
