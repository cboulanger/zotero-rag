"""
Migrate a library_id value across all Qdrant collections and registrations.json.

Use case: personal libraries were previously stored under id "1" (Zotero's
local DB primary key).  After the user-id-based naming refactor, they are
stored as "u{zoteroUserId}".  This script renames the id in-place without
re-embedding anything.

Usage
-----
    # Preview what would change (no writes)
    uv run python scripts/migrate_library_id.py --old-id 1 --new-id u3866263 --dry-run

    # Apply the migration (server must be stopped first to release the Qdrant lock)
    uv run python scripts/migrate_library_id.py --old-id 1 --new-id u3866263

The script iterates every model-slug subdirectory under VECTOR_DB_PATH so it
works regardless of which embedding preset was used.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

# Must be run from the project root so backend package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.config.settings import get_settings  # noqa: E402

CHUNKS_COLLECTION = "document_chunks"
DEDUP_COLLECTION = "deduplication"
METADATA_COLLECTION = "library_metadata"
METADATA_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _lib_uuid(library_id: str) -> str:
    return str(uuid.uuid5(METADATA_NAMESPACE, library_id))


def _count_matching(client: QdrantClient, collection: str, library_id: str) -> int:
    try:
        return client.count(
            collection_name=collection,
            count_filter=Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=library_id))]),
            exact=True,
        ).count
    except Exception:
        return 0


def migrate_qdrant_client(client: QdrantClient, old_id: str, new_id: str, dry_run: bool) -> dict:
    """Migrate library_id in all collections of an open Qdrant client. Returns counts."""
    counts = {"chunks": 0, "dedup": 0, "metadata": 0}

    collections = [c.name for c in client.get_collections().collections]

    # -- document_chunks --
    if CHUNKS_COLLECTION in collections:
        n = _count_matching(client, CHUNKS_COLLECTION, old_id)
        counts["chunks"] = n
        print(f"  {CHUNKS_COLLECTION}: {n} chunks to update")
        if n > 0 and not dry_run:
            client.set_payload(
                collection_name=CHUNKS_COLLECTION,
                payload={"library_id": new_id},
                points=Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=old_id))]),
            )
            print(f"  {CHUNKS_COLLECTION}: updated {n} chunks -> library_id={new_id!r}")

    # -- deduplication --
    if DEDUP_COLLECTION in collections:
        n = _count_matching(client, DEDUP_COLLECTION, old_id)
        counts["dedup"] = n
        print(f"  {DEDUP_COLLECTION}: {n} records to update")
        if n > 0 and not dry_run:
            client.set_payload(
                collection_name=DEDUP_COLLECTION,
                payload={"library_id": new_id},
                points=Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=old_id))]),
            )
            print(f"  {DEDUP_COLLECTION}: updated {n} records -> library_id={new_id!r}")

    # -- library_metadata --
    if METADATA_COLLECTION in collections:
        old_point_id = _lib_uuid(old_id)
        points = client.retrieve(collection_name=METADATA_COLLECTION, ids=[old_point_id], with_payload=True)
        if points:
            counts["metadata"] = 1
            payload = dict(points[0].payload)
            payload["library_id"] = new_id
            print(f"  {METADATA_COLLECTION}: found entry for {old_id!r} -> will rename to {new_id!r}")
            if not dry_run:
                new_point_id = _lib_uuid(new_id)
                client.upsert(
                    collection_name=METADATA_COLLECTION,
                    points=[PointStruct(id=new_point_id, vector=[0.0], payload=payload)],
                )
                client.delete(collection_name=METADATA_COLLECTION, points_selector=[old_point_id])
                print(f"  {METADATA_COLLECTION}: migrated metadata point")
        else:
            print(f"  {METADATA_COLLECTION}: no entry found for {old_id!r}")

    return counts


def migrate_qdrant_dir(model_dir: Path, old_id: str, new_id: str, dry_run: bool) -> dict:
    """Migrate one local Qdrant model-slug directory."""
    try:
        client = QdrantClient(path=str(model_dir))
    except Exception as e:
        print(f"  [SKIP] Cannot open Qdrant at {model_dir}: {e}")
        return {"chunks": 0, "dedup": 0, "metadata": 0}
    counts = migrate_qdrant_client(client, old_id, new_id, dry_run)
    client.close()
    return counts


def migrate_registrations(registrations_path: Path, old_id: str, new_id: str, dry_run: bool) -> bool:
    """Rename old_id -> new_id in registrations.json. Returns True if a change was made."""
    if not registrations_path.exists():
        print(f"registrations.json not found at {registrations_path} — skipping")
        return False

    text = registrations_path.read_text(encoding="utf-8").strip()
    if not text:
        print("registrations.json is empty — skipping")
        return False

    data = json.loads(text)
    if old_id not in data:
        print(f"registrations.json: key {old_id!r} not found — skipping")
        return False

    entry = data[old_id]
    entry["library_id"] = new_id
    data[new_id] = entry
    del data[old_id]

    print(f"registrations.json: will rename key {old_id!r} -> {new_id!r}")
    if not dry_run:
        registrations_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print("registrations.json: written")
    return True


def _scan_library_ids(client: QdrantClient, label: str) -> None:
    existing = [c.name for c in client.get_collections().collections]
    for collection in (CHUNKS_COLLECTION, DEDUP_COLLECTION, METADATA_COLLECTION):
        if collection not in existing:
            continue
        seen: set = set()
        offset = None
        while True:
            batch, offset = client.scroll(
                collection_name=collection,
                scroll_filter=None,
                limit=1000,
                offset=offset,
                with_payload=["library_id"],
            )
            for p in batch:
                seen.add(p.payload.get("library_id") if p.payload else "<no payload>")
            if offset is None:
                break
        print(f"  {collection}: library_ids = {sorted(str(v) for v in seen)}")


def list_library_ids(vector_db_path: Path, qdrant_url: str | None) -> None:
    """Print all distinct library_id values found in Qdrant (server or local file mode)."""
    if qdrant_url:
        print(f"Server mode: {qdrant_url}\n")
        client = QdrantClient(url=qdrant_url)
        _scan_library_ids(client, "server")
        client.close()
        return

    model_dirs = sorted(d for d in vector_db_path.iterdir() if d.is_dir())
    if not model_dirs:
        print("[WARN] No model subdirectories found.")
        return
    for model_dir in model_dirs:
        print(f"\n--- Model dir: {model_dir.name} ---")
        try:
            client = QdrantClient(path=str(model_dir))
        except Exception as e:
            print(f"  [SKIP] {e}")
            continue
        _scan_library_ids(client, model_dir.name)
        client.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate a library_id across all Qdrant collections.")
    parser.add_argument("--old-id", help="Current library_id (e.g. '1')")
    parser.add_argument("--new-id", help="New library_id (e.g. 'u3866263')")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing anything")
    parser.add_argument("--list", action="store_true", help="List all distinct library_id values (no migration)")
    parser.add_argument("--qdrant-url", help="Qdrant server URL (overrides settings, e.g. http://localhost:6333)")
    args = parser.parse_args()

    settings = get_settings()
    vector_db_path: Path = settings.vector_db_path
    registrations_path: Path = settings.registrations_path
    qdrant_url: str | None = args.qdrant_url or settings.qdrant_url

    print(f"VECTOR_DB_PATH : {vector_db_path}")
    print(f"REGISTRATIONS  : {registrations_path}")
    if qdrant_url:
        print(f"QDRANT_URL     : {qdrant_url} (server mode)")
    print()

    if args.list:
        list_library_ids(vector_db_path, qdrant_url)
        return

    if not args.old_id or not args.new_id:
        print("[ERROR] --old-id and --new-id are required unless --list is used.")
        sys.exit(1)

    old_id, new_id = args.old_id, args.new_id
    dry_run = args.dry_run

    if dry_run:
        print("[DRY RUN] No changes will be written.\n")

    print(f"Migrating library_id: {old_id!r} -> {new_id!r}\n")

    total = {"chunks": 0, "dedup": 0, "metadata": 0}

    if qdrant_url:
        print(f"--- Qdrant server: {qdrant_url} ---")
        client = QdrantClient(url=qdrant_url)
        counts = migrate_qdrant_client(client, old_id, new_id, dry_run)
        client.close()
        for k in total:
            total[k] += counts[k]
    else:
        if not vector_db_path.exists():
            print(f"[ERROR] VECTOR_DB_PATH does not exist: {vector_db_path}")
            sys.exit(1)
        model_dirs = sorted(d for d in vector_db_path.iterdir() if d.is_dir())
        if not model_dirs:
            print("[WARN] No model subdirectories found under VECTOR_DB_PATH — nothing to migrate in Qdrant.")
        else:
            for model_dir in model_dirs:
                print(f"\n--- Model dir: {model_dir.name} ---")
                counts = migrate_qdrant_dir(model_dir, old_id, new_id, dry_run)
                for k in total:
                    total[k] += counts[k]

    print(f"\n--- registrations.json ---")
    migrate_registrations(registrations_path, old_id, new_id, dry_run)

    print(f"\nSummary: {total['chunks']} chunks, {total['dedup']} dedup records, {total['metadata']} metadata entries")
    if dry_run:
        print("\n[DRY RUN] Re-run without --dry-run to apply.")
    else:
        print("\n[DONE] Migration complete.")


if __name__ == "__main__":
    main()
