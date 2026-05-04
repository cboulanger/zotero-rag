#!/usr/bin/env python3
"""
Ad-hoc test: verify that split_pdf_bytes actually reduces part sizes.

Usage:
    uv run python scripts/test_pdf_split.py <path-to-pdf> [--target-mb N]
"""

import argparse
import sys
from io import BytesIO
from pathlib import Path

import pikepdf
import pypdf

sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.utils.pdf_splitter import split_pdf_bytes


def fmt_mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def main():
    parser = argparse.ArgumentParser(description="Test PDF splitting")
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--target-mb", type=int, default=30, help="Target part size in MB (default: 30)")
    args = parser.parse_args()

    path = Path(args.pdf)
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)

    pdf_bytes = path.read_bytes()
    total_bytes = len(pdf_bytes)

    # Count pages via pypdf (fast, no decompression)
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    bytes_per_page = total_bytes / total_pages if total_pages else 0

    print(f"Input : {path.name}")
    print(f"Size  : {fmt_mb(total_bytes)}  ({total_bytes:,} bytes)")
    print(f"Pages : {total_pages}")
    print(f"Avg   : {fmt_mb(int(bytes_per_page))} / page")
    print()

    target_bytes = args.target_mb * 1_048_576
    expected_parts = max(1, round(total_bytes / target_bytes))
    print(f"Target part size : {args.target_mb} MB  → expect ~{expected_parts} part(s)")
    print()

    parts = split_pdf_bytes(pdf_bytes, target_bytes)

    print(f"{'Part':<6} {'Offset':>8}  {'Pages':>6}  {'Size':>12}  {'Ratio vs input':>16}")
    print("-" * 58)
    total_part_pages = 0
    for i, (part_bytes, offset) in enumerate(parts):
        part_pages = len(pikepdf.open(BytesIO(part_bytes)).pages)
        ratio = len(part_bytes) / total_bytes
        ok = "[OK]" if len(part_bytes) < total_bytes * 0.8 else "[!!] INFLATED"
        print(f"{i+1:<6} {offset:>8}  {part_pages:>6}  {fmt_mb(len(part_bytes)):>12}  {ratio:>14.1%}  {ok}")
        total_part_pages += part_pages

    print("-" * 58)
    pages_ok = total_part_pages == total_pages
    print(f"{'Total':<6} {'':>8}  {total_part_pages:>6}  {'':>12}  pages {'[OK]' if pages_ok else '[!!] MISMATCH'}")
    print()

    if total_part_pages != total_pages:
        print(f"[FAIL] Page count mismatch: {total_part_pages} split pages vs {total_pages} original")
        sys.exit(1)

    inflated = [p for p, _ in parts if len(p) >= total_bytes * 0.8]
    if inflated:
        print(f"[FAIL] {len(inflated)} part(s) are nearly as large as the original — splitting is not working")
        sys.exit(1)

    print(f"[PASS] Split into {len(parts)} parts, all well below original size")


if __name__ == "__main__":
    main()
