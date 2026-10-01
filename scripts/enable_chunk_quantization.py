"""
Enable int8 scalar quantization on an existing document_chunks collection.

Use case: new collections are created with scalar quantization enabled by
default (see backend/db/vector_store.py's CHUNKS_QUANTIZATION_CONFIG), but
that only applies to collections created from scratch. An already-indexed
collection (e.g. production's 7.24M-point collection, root-caused in the
2026-10 Qdrant-timeout incident) needs to be migrated in place.

What this does
---------------
Calls Qdrant's `update_collection` to:
  - enable int8 scalar quantization (quantile=0.99, always_ram=True) — the
    quantized vectors stay resident in RAM at ~1/4 the size of the original
    float32 vectors;
  - mark the original (full-precision) vectors `on_disk=True` — they move to
    memory-mapped disk storage and are only read back to rescore the top
    candidates of each query, instead of being forced RAM-resident for the
    whole HNSW search.

Qdrant applies this by rebuilding segments in the background (the collection
stays readable/writable throughout, served from old segments until each is
replaced). For a multi-million-point collection on spinning disk this can
take a long time and adds disk I/O load while it runs — schedule it for a
low-traffic window and don't run it during an active cron indexing pass.

It does NOT shrink the point count. Combine with a re-index (which now
benefits from backend.services.chunking.coalesce_chunks merging tiny
per-page chunks) if you also want to reduce the number of points.

Usage
-----
    # Preview current config + estimated memory impact (no changes)
    uv run python scripts/enable_chunk_quantization.py --dry-run

    # Apply against the local embedded/dev Qdrant
    uv run python scripts/enable_chunk_quantization.py

    # Apply against a running Qdrant server
    uv run python scripts/enable_chunk_quantization.py --qdrant-url http://localhost:6333

    # Apply and then poll collection status until segment rebuild finishes
    uv run python scripts/enable_chunk_quantization.py --qdrant-url http://localhost:6333 --wait
"""

import argparse
import sys
import time
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import ScalarQuantization, ScalarQuantizationConfig, ScalarType, VectorParamsDiff

# Must be run from the project root so backend package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.config.settings import get_settings  # noqa: E402
from backend.db.vector_store import VectorStore  # noqa: E402

CHUNKS_COLLECTION = VectorStore.CHUNKS_COLLECTION
BYTES_PER_FLOAT32 = 4
BYTES_PER_INT8 = 1


def _format_gb(num_bytes: float) -> str:
    return f"{num_bytes / (1024 ** 3):.2f} GB"


def _print_report(client: QdrantClient) -> dict:
    info = client.get_collection(CHUNKS_COLLECTION)
    points_count = info.points_count or 0
    vectors_config = info.config.params.vectors
    dim = vectors_config.size if hasattr(vectors_config, "size") else next(iter(vectors_config.values())).size
    on_disk = vectors_config.on_disk if hasattr(vectors_config, "on_disk") else None
    quantization = info.config.quantization_config

    raw_bytes = points_count * dim * BYTES_PER_FLOAT32
    quantized_bytes = points_count * dim * BYTES_PER_INT8

    print(f"Collection      : {CHUNKS_COLLECTION}")
    print(f"Points          : {points_count:,}")
    print(f"Vector dim      : {dim}")
    print(f"Vectors on_disk : {on_disk}")
    print(f"Quantization    : {quantization}")
    print(f"Unquantized vector data (float32): {_format_gb(raw_bytes)}")
    print(f"Quantized vector data (int8):      {_format_gb(quantized_bytes)}")
    print(f"Estimated RAM-resident savings:    {_format_gb(raw_bytes - quantized_bytes)}")
    return {"points_count": points_count, "already_quantized": quantization is not None}


def _wait_for_green(client: QdrantClient, poll_seconds: int = 15) -> None:
    print(f"\nPolling collection status every {poll_seconds}s until optimization finishes (Ctrl-C to stop watching)...")
    while True:
        info = client.get_collection(CHUNKS_COLLECTION)
        print(
            f"  status={info.status} segments={info.segments_count} "
            f"indexed_vectors={info.indexed_vectors_count} points={info.points_count}"
        )
        if str(info.status).lower() == "green" or str(info.status).lower().endswith("green"):
            print("Collection is green — segment rebuild complete.")
            return
        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="Enable int8 scalar quantization on document_chunks.")
    parser.add_argument("--dry-run", action="store_true", help="Report current state and estimated savings only")
    parser.add_argument("--qdrant-url", help="Qdrant server URL (overrides settings, e.g. http://localhost:6333)")
    parser.add_argument("--wait", action="store_true", help="After applying, poll status until segments finish rebuilding")
    parser.add_argument(
        "--quantile",
        type=float,
        default=0.99,
        help="Scalar quantization quantile (default 0.99, matches CHUNKS_QUANTIZATION_CONFIG)",
    )
    args = parser.parse_args()

    settings = get_settings()
    qdrant_url = args.qdrant_url or settings.qdrant_url

    if qdrant_url:
        print(f"Connecting to Qdrant server: {qdrant_url}\n")
        client = QdrantClient(url=qdrant_url, timeout=60)
    else:
        print(f"Connecting to local Qdrant storage: {settings.vector_db_path}\n")
        client = QdrantClient(path=str(settings.vector_db_path))

    collections = [c.name for c in client.get_collections().collections]
    if CHUNKS_COLLECTION not in collections:
        print(f"[ERROR] Collection '{CHUNKS_COLLECTION}' not found.")
        sys.exit(1)

    state = _print_report(client)

    if state["already_quantized"]:
        print(f"\n'{CHUNKS_COLLECTION}' already has quantization_config set — nothing to do.")
        return

    if args.dry_run:
        print("\n[DRY RUN] No changes applied. Re-run without --dry-run to apply.")
        return

    print(f"\nApplying int8 scalar quantization (quantile={args.quantile}, always_ram=True) "
          f"and marking original vectors on_disk=True...")
    client.update_collection(
        collection_name=CHUNKS_COLLECTION,
        vectors_config={"": VectorParamsDiff(on_disk=True)},
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=args.quantile, always_ram=True)
        ),
    )
    print("[DONE] Update submitted. Qdrant rebuilds segments in the background; "
          "the collection remains readable/writable throughout.")

    if args.wait:
        _wait_for_green(client)
    else:
        print("Run again with --dry-run (or inspect `get_collection`) later to confirm the rebuild finished.")


if __name__ == "__main__":
    main()
