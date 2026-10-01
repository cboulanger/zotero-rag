"""
Reprocess specific already-indexed items to shrink their chunk count.

Use case: some items (typically long books) were indexed one chunk per page
by extractors that treat pages as hard chunk boundaries, producing far more
Qdrant points than necessary (see backend.services.chunking.coalesce_chunks,
added to fix this for future indexing). Re-running a full library reindex
would also pick up the fix, but costs roughly one extraction pass over every
chunk currently in the library (see docs/architecture.md's "Capacity at
scale" note) — for a library with one 70,000-chunk outlier among thousands of
small items, that's mostly wasted work. This script targets only the
oversized items directly, deleting their existing chunks and reprocessing
just those attachments through the current (chunk-coalescing) pipeline.

It reuses the exact same reprocessing idiom DocumentProcessor's own
incremental/full sync already uses for a changed item: delete the item's
existing chunks, then call `_index_item()` again. Deleting the chunks first
is what makes this safe to re-run on unchanged content — VectorStore.
check_duplicate()'s same-library-dedup path only skips an item when
get_item_version() still finds a version for it; with no chunks left, that
lookup returns None and the duplicate check falls through to fresh
extraction instead of skipping (see DocumentProcessor._handle_same_library_
duplicate's docstring).

Usage
-----
    # Discover candidates (read-only): items with >= 1000 chunks, top 25
    uv run python bin/reindex_oversized_items.py --list --min-chunks 1000 --top 25

    # Reprocess specific items (dry run first — reports what would happen)
    uv run python bin/reindex_oversized_items.py --item u39226:U9CD3MQI

    # Apply for real
    uv run python bin/reindex_oversized_items.py --item u39226:U9CD3MQI --apply

    # Auto-discover the top N oversized items and reprocess them in one go
    uv run python bin/reindex_oversized_items.py --min-chunks 1000 --top 25 --apply

Must be run with the same environment as the cron indexer (AUTOINDEX_SECRET,
QDRANT_URL, etc. set) — e.g. inside the production container via
`podman exec`, or locally against a dev data dir.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter, MatchValue

# Must be run from the project root so backend package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.config.settings import get_settings  # noqa: E402
from backend.dependencies import make_vector_store  # noqa: E402
from backend.db.vector_store import VectorStore  # noqa: E402
from backend.services.embeddings import create_embedding_service  # noqa: E402
from backend.services.autoindex_key_store import AutoIndexKeyStore  # noqa: E402
from backend.services.autoindex_resolver import resolve_targets  # noqa: E402
from backend.services.document_processor import DocumentProcessor  # noqa: E402
from backend.zotero.web_api import ZoteroWebAPI  # noqa: E402


def _library_type(library_id: str) -> str:
    return "user" if library_id.startswith("u") else "group"


def _slug_for(library_id: str) -> str:
    kind = "users" if _library_type(library_id) == "user" else "groups"
    numeric = library_id[1:] if library_id.startswith("u") else library_id
    return f"{kind}/{numeric}"


def discover_oversized_items(vector_store: VectorStore, min_chunks: int, top: int) -> list[dict]:
    """Return up to *top* items with >= min_chunks chunks, sorted by chunk count desc.

    Each entry: {"library_id", "item_key", "title", "chunk_count"}.
    """
    client = vector_store.client
    facet = client.facet(
        collection_name=VectorStore.CHUNKS_COLLECTION,
        key="item_key",
        limit=max(top * 4, 200),  # over-fetch since low-count hits are filtered below
        exact=True,
    )
    hits = [h for h in facet.hits if h.count >= min_chunks]
    hits.sort(key=lambda h: h.count, reverse=True)
    hits = hits[:top]

    results = []
    for hit in hits:
        item_key = str(hit.value)
        points, _ = client.scroll(
            collection_name=VectorStore.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="item_key", match=MatchValue(value=item_key))]),
            limit=1,
            with_payload=["library_id", "title"],
        )
        if not points:
            continue
        payload = points[0].payload or {}
        results.append({
            "library_id": payload.get("library_id"),
            "item_key": item_key,
            "title": payload.get("title", "?"),
            "chunk_count": hit.count,
        })
    return results


async def _reprocess_items(
    targets_by_library: dict[str, list[str]],
    vector_store: VectorStore,
    apply: bool,
) -> None:
    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        print("[ERROR] AUTOINDEX_SECRET is not set; cannot decrypt stored Zotero keys.")
        sys.exit(1)
    auto_targets, key_issues = await resolve_targets(store)
    for issue in key_issues:
        print(f"[WARN] Key pruned for user {issue.get('user')}: {issue.get('reason')}")

    preset = settings.get_hardware_preset()

    for library_id, item_keys in targets_by_library.items():
        slug = _slug_for(library_id)
        target = auto_targets.get(slug)
        if not target:
            print(f"[SKIP] No auto-index key found for {slug} (library_id={library_id})")
            continue

        library_type = _library_type(library_id)
        web_api = ZoteroWebAPI(api_key=target["zotero_key"])
        embedding_service = create_embedding_service(preset.embedding, api_key=target["embedding_key"])
        processor = DocumentProcessor(
            zotero_client=web_api,
            embedding_service=embedding_service,
            vector_store=vector_store,
        )

        async with web_api:
            items = await web_api.get_items_by_keys(library_id, item_keys, library_type)
            items_by_key = {item["data"]["key"]: item for item in items if "data" in item}

            for item_key in item_keys:
                old_count = len(vector_store.get_item_chunks(library_id, item_key))
                item = items_by_key.get(item_key)

                if item is None:
                    print(f"[SKIP] {library_id}:{item_key} — not found in Zotero (deleted upstream?)")
                    continue

                title = item["data"].get("title", "Untitled")
                if not apply:
                    print(f"[DRY RUN] {library_id}:{item_key} ({title!r}): {old_count} chunks -> would reprocess")
                    continue

                t0 = time.monotonic()
                vector_store.delete_item_chunks(library_id, item_key)
                new_count = await processor._index_item(item, library_id, library_type)
                elapsed = time.monotonic() - t0
                print(
                    f"[DONE] {library_id}:{item_key} ({title!r}): "
                    f"{old_count} -> {new_count} chunks ({elapsed:.1f}s)"
                )

        # Refresh library-level chunk total so the plugin's index-status view stays accurate.
        metadata = vector_store.get_library_metadata(library_id)
        if metadata is not None:
            metadata.total_chunks = vector_store.count_library_chunks(library_id)
            vector_store.update_library_metadata(metadata)


def main():
    parser = argparse.ArgumentParser(
        description="Reprocess oversized already-indexed items through the current chunking pipeline."
    )
    parser.add_argument("--list", action="store_true", help="Only report candidates; do not reprocess anything")
    parser.add_argument("--min-chunks", type=int, default=1000, help="Minimum chunk count to consider (default 1000)")
    parser.add_argument("--top", type=int, default=25, help="Max number of candidates to consider (default 25)")
    parser.add_argument(
        "--item", action="append", metavar="LIBRARY_ID:ITEM_KEY",
        help="Reprocess this specific item instead of auto-discovering candidates (repeatable)",
    )
    parser.add_argument("--apply", action="store_true", help="Actually delete+reprocess (default: dry run only)")
    args = parser.parse_args()

    vector_store = make_vector_store()

    if args.item:
        targets_by_library: dict[str, list[str]] = {}
        for spec in args.item:
            if ":" not in spec:
                print(f"[ERROR] --item must be LIBRARY_ID:ITEM_KEY, got: {spec!r}")
                sys.exit(1)
            library_id, item_key = spec.split(":", 1)
            targets_by_library.setdefault(library_id, []).append(item_key)
    else:
        candidates = discover_oversized_items(vector_store, args.min_chunks, args.top)
        if not candidates:
            print(f"No items found with >= {args.min_chunks} chunks.")
            return
        total_chunks = sum(c["chunk_count"] for c in candidates)
        print(f"Found {len(candidates)} candidate item(s), {total_chunks} chunks total:\n")
        for c in candidates:
            print(f"  {c['library_id']:>12}:{c['item_key']}  {c['chunk_count']:>7} chunks  {c['title']!r}")
        print()
        if args.list:
            return
        targets_by_library = {}
        for c in candidates:
            targets_by_library.setdefault(c["library_id"], []).append(c["item_key"])

    if not args.apply:
        print("[DRY RUN] No changes will be made. Re-run with --apply to reprocess these items.\n")

    asyncio.run(_reprocess_items(targets_by_library, vector_store, args.apply))


if __name__ == "__main__":
    main()
