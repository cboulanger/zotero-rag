"""
Export one Zotero library's indexed RAG data to a single portable snapshot
file, and import such a snapshot into another zotero-rag backend.

Use this to build the gold-corpus index once in the full local environment
(real Kreuzberg + Qdrant sidecar) and restore it in a session that cannot run
the extraction sidecar, e.g. a cloud session whose backend uses embedded Qdrant.
It reuses the `/api/migration/*` endpoints and HTTP machinery of
bin/migrate_library.py, so the same admin-key rules and retry behavior apply,
but stores the points in a file instead of streaming them between two live
instances.

Usage:
    uv run python bin/library_snapshot.py export <slug> <source-url> <out.jsonl.gz>
        [--source-key KEY] [--batch-size 200] [--max-outage-minutes 240]
    uv run python bin/library_snapshot.py import <snapshot.jsonl.gz> <dest-url>
        [--dest-key KEY] [--batch-size 200] [--max-outage-minutes 240]
    uv run python bin/library_snapshot.py info <snapshot.jsonl.gz>

<slug> is a Zotero.org library slug, e.g. groups/6297749. Import fully
overwrites the destination's data for that library and refuses to run when the
destination's embedding model/dimension differs from the one recorded in the
snapshot. Re-index instead in that case.

Snapshot format (gzip-compressed JSON Lines): the first line is a header
({"format", "slug", "library_id", "embedding_model_name", "embedding_dim",
"counts", "metadata", "created_at"}); every following line is
{"collection": "chunks" | "dedup", "point": {id, vector, payload}}. The file is
written to a temporary name and renamed on success, so a partial export never
looks like a valid snapshot.
"""

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx  # noqa: E402

from backend.api.public_query import slug_to_backend_id  # noqa: E402
from backend.utils.migration_state import MIGRATION_COLLECTIONS  # noqa: E402
from bin import migrate_library  # noqa: E402
from bin.migrate_library import MigrationError, _get, _post  # noqa: E402

SNAPSHOT_FORMAT = "zotero-rag-library-snapshot/1"


def export_snapshot(
    client, slug: str, source_url: str, out_path: Path,
    source_key: Optional[str] = None, batch_size: int = 200,
) -> dict:
    """Write the library's chunks, dedup records and index metadata to `out_path`.

    Returns the snapshot header. The file only appears at `out_path` once the
    export completed and the exported counts match the source's counts.
    """
    library_id = slug_to_backend_id(slug)
    info = _get(client, source_url, "/api/migration/embedding-info", source_key)
    metadata = _get(client, source_url, "/api/migration/export/metadata", source_key, library_id=library_id)
    counts = {
        collection: _get(
            client, source_url, "/api/migration/export/count", source_key,
            library_id=library_id, collection=collection,
        )["count"]
        for collection in MIGRATION_COLLECTIONS
    }
    if not counts["chunks"]:
        raise MigrationError(f"{slug} ({library_id}) has no indexed chunks on {source_url}; nothing to export.")

    header = {
        "format": SNAPSHOT_FORMAT,
        "slug": slug,
        "library_id": library_id,
        "embedding_model_name": info["embedding_model_name"],
        "embedding_dim": info["embedding_dim"],
        "counts": counts,
        "metadata": metadata,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".partial")
    written = {c: 0 for c in MIGRATION_COLLECTIONS}
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            for collection in MIGRATION_COLLECTIONS:
                offset = None
                while True:
                    page = _get(
                        client, source_url, "/api/migration/export", source_key,
                        library_id=library_id, collection=collection, offset=offset, limit=batch_size,
                    )
                    for point in page["points"]:
                        fh.write(json.dumps({"collection": collection, "point": point}) + "\n")
                        written[collection] += 1
                    offset = page["next_offset"]
                    print(
                        f"[..] {collection}: {written[collection]}/{counts[collection]} exported",
                        end="\r", file=sys.stderr,
                    )
                    if offset is None:
                        break
                print(f"[OK] {collection}: {written[collection]}/{counts[collection]} exported", file=sys.stderr)
        # The index may change while exporting (e.g. a running indexer); a snapshot
        # whose counts disagree with its header would be silently inconsistent.
        if written != counts:
            raise MigrationError(
                f"Exported {written} but the source reported {counts}; the library changed "
                "during export. Stop indexing and re-run."
            )
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return header


def read_header(path: Path) -> dict:
    """Return the snapshot header, validating the format marker."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
    if header.get("format") != SNAPSHOT_FORMAT:
        raise MigrationError(f"{path} is not a {SNAPSHOT_FORMAT} file (format={header.get('format')!r})")
    return header


def _iter_points(path: Path) -> Iterator[tuple[str, dict]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        fh.readline()  # header
        for line in fh:
            row = json.loads(line)
            yield row["collection"], row["point"]


def _count_rows(path: Path) -> dict[str, int]:
    """Count points per collection, so a corrupt/truncated file is caught before the destination is cleared."""
    counts = {c: 0 for c in MIGRATION_COLLECTIONS}
    try:
        for collection, _ in _iter_points(path):
            counts[collection] += 1
    except (EOFError, ValueError, KeyError, OSError) as exc:
        raise MigrationError(f"{path} is truncated or corrupt: {exc}") from exc
    return counts


def import_snapshot(
    client, snapshot_path: Path, dest_url: str,
    dest_key: Optional[str] = None, batch_size: int = 200,
) -> dict:
    """Overwrite the destination's data for the snapshot's library with the snapshot's."""
    header = read_header(snapshot_path)
    library_id = header["library_id"]
    if _count_rows(snapshot_path) != header["counts"]:
        raise MigrationError(f"{snapshot_path} is truncated or corrupt: its points do not match the header counts.")
    dest_info = _get(client, dest_url, "/api/migration/embedding-info", dest_key)
    if (dest_info["embedding_model_name"], dest_info["embedding_dim"]) != (
        header["embedding_model_name"], header["embedding_dim"]
    ):
        raise MigrationError(
            f"Embedding mismatch: snapshot was built with {header['embedding_model_name']} "
            f"({header['embedding_dim']}-dim), destination uses {dest_info['embedding_model_name']} "
            f"({dest_info['embedding_dim']}-dim). Re-index on the destination instead."
        )

    begin = _post(client, dest_url, "/api/migration/import/begin", dest_key, {"library_id": library_id})
    imported = {c: 0 for c in MIGRATION_COLLECTIONS}
    batch: list[dict] = []
    current: Optional[str] = None

    def flush() -> None:
        if batch:
            _post(
                client, dest_url, "/api/migration/import", dest_key,
                {"library_id": library_id, "points": list(batch)}, collection=current,
            )
            imported[current] += len(batch)
            batch.clear()

    for collection, point in _iter_points(snapshot_path):
        if collection != current or len(batch) >= batch_size:
            flush()
            current = collection
        batch.append(point)
    flush()

    if imported != header["counts"]:
        raise MigrationError(f"Snapshot is truncated or corrupt: imported {imported}, header says {header['counts']}.")
    _post(
        client, dest_url, "/api/migration/import/metadata", dest_key,
        {"library_id": library_id, "payload": header["metadata"]},
    )
    return {"library_id": library_id, "begin_result": begin, "imported": imported}


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--batch-size", type=int, default=200, help="Points per request (default: 200)")
    common.add_argument(
        "--max-outage-minutes", type=float, default=migrate_library._DEFAULT_MAX_OUTAGE_MINUTES,
        help="Keep retrying a stuck request for up to this many minutes (see bin/migrate_library.py)",
    )

    exp = sub.add_parser("export", parents=[common], help="Write a snapshot file from a running instance")
    exp.add_argument("slug", help="Zotero library slug, e.g. groups/6297749")
    exp.add_argument("source_url", help="Source instance base URL, e.g. http://localhost:8119")
    exp.add_argument("out_path", type=Path, help="Output file, e.g. gold.jsonl.gz")
    exp.add_argument("--source-key", default=None, help="Admin Zotero API key (omit for a loopback-mode instance)")

    imp = sub.add_parser("import", parents=[common], help="Load a snapshot file into a running instance")
    imp.add_argument("snapshot", type=Path)
    imp.add_argument("dest_url", help="Destination instance base URL")
    imp.add_argument("--dest-key", default=None, help="Admin Zotero API key (omit for a loopback-mode instance)")

    info = sub.add_parser("info", help="Print a snapshot's header without loading it")
    info.add_argument("snapshot", type=Path)
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    start = time.monotonic()
    try:
        if args.command == "info":
            header = read_header(args.snapshot)
            header["metadata"] = {k: header["metadata"].get(k) for k in ("total_chunks", "total_items_indexed")}
            print(json.dumps(header, indent=2))
            return
        migrate_library._max_outage_seconds = args.max_outage_minutes * 60
        with httpx.Client() as client:
            if args.command == "export":
                header = export_snapshot(
                    client, args.slug, args.source_url, args.out_path, args.source_key, args.batch_size,
                )
                print(
                    f"[OK] Wrote {args.out_path} ({header['counts']}, {header['embedding_model_name']}) "
                    f"in {time.monotonic() - start:.1f}s"
                )
            else:
                result = import_snapshot(client, args.snapshot, args.dest_url, args.dest_key, args.batch_size)
                print(
                    f"[OK] Imported {result['imported']} into {args.dest_url} "
                    f"in {time.monotonic() - start:.1f}s"
                )
    except (MigrationError, ValueError, OSError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[FAIL] Interrupted by user.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
