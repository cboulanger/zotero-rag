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
oversized items directly, reprocessing just those attachments through the
current (chunk-coalescing) pipeline.

It calls `_index_item(..., force_extraction=True)`, which skips both the
same-library and cross-library dedup lookups (VectorStore.check_duplicate()/
find_cross_library_duplicate()) and always re-extracts, so it never trusts
whatever is already stored for this exact item or a sibling copy elsewhere.
That means it's safe to reprocess *without* deleting the item's existing
chunks first — the old points and the freshly-added ones can coexist
(add_chunks_batch always assigns fresh point IDs). This script captures the
old points' IDs up front and only deletes them *after* confirming
reprocessing actually produced replacement chunks (see
VectorStore.delete_chunks_by_ids).

This matters because reprocessing can legitimately produce zero chunks for
reasons outside this script's control — most importantly, an attachment
whose Zotero-hosted file storage has since expired or been removed returns a
plain 404 from get_attachment_file(), and there's no way to recover its
content: cross-library dedup can't help either, since matching a sibling
copy requires a content_hash computed from the downloaded bytes, which were
never obtained. Deleting first and reprocessing after would permanently
zero out such an item instead of leaving its last-known-good chunks in
place. (Hit in production: a delete-then-reprocess run zeroed out two items
whose attachments now 404 from Zotero's own API — recovered by hand via
Qdrant's surviving deduplication-collection history, which is not
guaranteed to be possible in general.)

Cross-library dedup bypass is also why force_extraction is required at all,
not just an optimization: without it, reprocessing a copy of content that's
duplicated across libraries (same book attached to a personal library and a
shared group library, for example) doesn't re-extract — it just cross-copies
whatever chunks another library's still-unprocessed copy currently has,
which can make the chunk count *worse* if that other copy hasn't been
reprocessed yet. (Separately hit in production: reprocessing one copy of a
shared book went from 70,020 to 90,402 chunks this way.) force_extraction
guarantees a real extraction + coalesce_chunks pass regardless of what any
other library currently has stored for the same content.

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

    # Unattended: keep discovering+reprocessing in batched rounds until none
    # remain >= --min-chunks. Safe to run as a long-lived background job —
    # see _loop()'s docstring for batching/exclusion behavior.
    uv run python bin/reindex_oversized_items.py --loop --min-chunks 1000

Must be run with the same environment as the cron indexer (AUTOINDEX_SECRET,
QDRANT_URL, etc. set) — e.g. inside the production container via
`podman exec`, or locally against a dev data dir.
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter, MatchValue

# Must be run from the project root so backend package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.config.settings import get_settings  # noqa: E402
from backend.dependencies import make_vector_store  # noqa: E402
from backend.db.vector_store import VectorStore  # noqa: E402
from backend.services.embeddings import (  # noqa: E402
    EmbeddingAuthenticationError,
    EmbeddingRateLimitExhaustedError,
    create_embedding_service,
)
from backend.services.autoindex_key_store import AutoIndexKeyStore  # noqa: E402
from backend.services.autoindex_resolver import resolve_targets  # noqa: E402
from backend.services.document_processor import DocumentProcessor, merge_download_failures  # noqa: E402
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
) -> dict[tuple[str, str], str]:
    """Reprocess the given items. Returns {(library_id, item_key): status},
    status one of "done", "no_improvement", "failed", "not_found",
    "skipped_no_key" — used by _loop() to permanently exclude items that
    fail for reasons a retry can't fix (e.g. a 404'd attachment), or that
    reprocessed successfully but produced no reduction (already optimally
    chunked — a repeat run would just reproduce the same count), instead of
    retrying them every round forever.
    """
    results: dict[tuple[str, str], str] = {}
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
            for item_key in item_keys:
                results[(library_id, item_key)] = "skipped_no_key"
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
                old_chunks = vector_store.get_item_chunks(library_id, item_key)
                old_count = len(old_chunks)
                item = items_by_key.get(item_key)

                if item is None:
                    print(f"[SKIP] {library_id}:{item_key} — not found in Zotero (deleted upstream?)")
                    results[(library_id, item_key)] = "not_found"
                    continue

                title = item["data"].get("title", "Untitled")
                if not apply:
                    print(f"[DRY RUN] {library_id}:{item_key} ({title!r}): {old_count} chunks -> would reprocess")
                    continue

                # Reindex-then-swap, never delete-then-reindex: capture the
                # existing points' IDs and reprocess *without* deleting first.
                # force_extraction=True means this never trusts what's already
                # there, so leaving the old points in place during reprocessing
                # is harmless (see _process_attachment_bytes). Only delete the
                # old points afterward, and only if reprocessing actually
                # produced replacement content — otherwise an attachment that
                # can no longer be downloaded (e.g. its Zotero file storage
                # expired/was removed; cross-library dedup can't help either,
                # since computing a content_hash to match against requires the
                # bytes) would permanently zero out an item instead of leaving
                # its last-known-good chunks untouched. Hit in production:
                # reprocessing two items whose attachments now 404 from
                # Zotero's own API deleted their chunks upfront with no way to
                # restore them.
                old_ids = [c["id"] for c in old_chunks]
                old_attachment_keys = {c["payload"].get("attachment_key") for c in old_chunks}
                old_was_abstract_only = old_count > 0 and all(
                    (k or "").endswith(":abstract") for k in old_attachment_keys
                )
                t0 = time.monotonic()
                new_count = await processor._index_item(
                    item, library_id, library_type, force_extraction=True
                )
                elapsed = time.monotonic() - t0
                if new_count == 0:
                    print(
                        f"[FAILED] {library_id}:{item_key} ({title!r}): "
                        f"reprocessing produced 0 chunks ({elapsed:.1f}s) — "
                        f"existing {old_count} chunks left untouched"
                    )
                    results[(library_id, item_key)] = "failed"
                    continue

                # Guard against a second, more insidious zero-content case than
                # new_count == 0: DocumentProcessor._index_item falls back to
                # indexing an item's abstractNote (a handful of words, 1 chunk)
                # whenever every real attachment failed to download — which is
                # indistinguishable, from new_count alone, from a genuine small
                # improvement. If this item previously had real attachment-derived
                # content (old chunks not already abstract-only) and everything
                # just written this round IS abstract-only, the actual PDF simply
                # failed to download this run — accepting the swap would delete
                # thousands of real chunks in exchange for a one-chunk blurb. Hit
                # in production: three ~3,500-chunk items collapsed to 1 chunk each
                # after a transient "Could not download attachment" during
                # reprocessing. Detect it before old_ids is deleted, so the
                # last-known-good chunks can still be kept.
                if not old_was_abstract_only:
                    old_id_set = set(old_ids)
                    current_chunks = vector_store.get_item_chunks(library_id, item_key)
                    new_chunks = [c for c in current_chunks if c["id"] not in old_id_set]
                    new_is_abstract_only = bool(new_chunks) and all(
                        (c["payload"].get("attachment_key") or "").endswith(":abstract")
                        for c in new_chunks
                    )
                    if new_is_abstract_only:
                        vector_store.delete_chunks_by_ids([c["id"] for c in new_chunks])
                        print(
                            f"[FAILED] {library_id}:{item_key} ({title!r}): "
                            f"reprocessing fell back to abstract-only content "
                            f"({new_count} chunk(s)) after a download failure — "
                            f"existing {old_count} chunks left untouched"
                        )
                        results[(library_id, item_key)] = "failed"
                        continue

                vector_store.delete_chunks_by_ids(old_ids)
                if new_count >= old_count:
                    # Reprocessing succeeded but didn't shrink the item — it was
                    # already optimally chunked (e.g. a long but densely-packed
                    # book) rather than an old per-page-chunk outlier. A repeat
                    # run would deterministically reproduce the same count, so
                    # treat this like a terminal failure for _loop()'s exclusion
                    # purposes: without this, an item whose "optimal" size is
                    # still >= --min-chunks gets rediscovered and reprocessed
                    # every single round forever, burning the full extraction
                    # time for zero benefit. (Hit in production: a single
                    # ~4,500-chunk item was reprocessed 9 times in a row, ~40
                    # minutes each, before this was caught.)
                    print(
                        f"[DONE] {library_id}:{item_key} ({title!r}): "
                        f"{old_count} -> {new_count} chunks ({elapsed:.1f}s) — "
                        f"no reduction, excluding from future rounds"
                    )
                    results[(library_id, item_key)] = "no_improvement"
                else:
                    print(
                        f"[DONE] {library_id}:{item_key} ({title!r}): "
                        f"{old_count} -> {new_count} chunks ({elapsed:.1f}s)"
                    )
                    results[(library_id, item_key)] = "done"

        # Refresh library-level chunk total so the plugin's index-status view stays accurate.
        metadata = vector_store.get_library_metadata(library_id)
        if metadata is not None:
            metadata.total_chunks = vector_store.count_library_chunks(library_id)
            # Surface this script's own download failures (e.g. an attachment's
            # Zotero-hosted file has 404'd) to the plugin's Fix Unavailable tool
            # the same way a full scan would — otherwise they're only visible in
            # this script's own log, invisible to the UI. Merge rather than
            # overwrite: this script only ever sees the items it was asked to
            # reprocess, not a full scan's complete view, so replacing the list
            # outright could hide genuine failures a real full scan found. The
            # next full scan still fully supersedes this once it runs.
            if processor._download_failures:
                metadata.last_scan_failed_downloads = merge_download_failures(
                    metadata.last_scan_failed_downloads, processor._download_failures
                )
            vector_store.update_library_metadata(metadata)

    return results


async def _loop(
    vector_store: VectorStore,
    min_chunks: int,
    discover_top: int,
    batch_chunk_budget: int,
    max_batch_size: int,
) -> None:
    """Repeat discovery+reprocess rounds until no oversized items remain.

    Each round: discover candidates (largest first), build a batch of up to
    `max_batch_size` items — a single huge item fills a round alone, several
    smaller ones get grouped together up to `batch_chunk_budget` combined old
    chunks — reprocess that batch, then loop. Items that fail for a reason a
    retry can't fix, or that reprocess successfully but don't shrink (see
    _reprocess_items's return) are excluded from every later round in this
    run, so neither a permanently-undownloadable attachment nor an
    already-optimally-chunked item can spin the loop forever; both are
    retried the next time this script is run.

    The embedding API's rate limit/quota is a different kind of failure —
    unlike a 404'd attachment, it isn't specific to any one item, and it
    resolves itself once the provider's window resets. _index_item raises
    EmbeddingRateLimitExhaustedError for this (see
    DocumentProcessor._FATAL_EMBEDDING_ERRORS) with an ``available_at``
    timestamp; this loop catches it here, sleeps until then, and retries the
    exact same round from scratch rather than excluding anything or crashing
    the whole unattended run (matching how CronIndexer already handles the
    same exception for regular sync runs). EmbeddingAuthenticationError (a
    bad/revoked key) is not retried — no amount of waiting fixes that — so
    the loop stops and reports it for a human to fix.
    """
    permanently_excluded: set[tuple[str, str]] = set()
    round_num = 0
    while True:
        round_num += 1
        all_candidates = discover_oversized_items(vector_store, min_chunks, discover_top)
        candidates = [
            c for c in all_candidates
            if (c["library_id"], c["item_key"]) not in permanently_excluded
        ]
        if not candidates:
            if all_candidates:
                print(
                    f"[LOOP] {len(all_candidates)} candidate(s) remain but all failed earlier "
                    f"this run and were excluded — stopping after {round_num - 1} round(s)."
                )
            else:
                print(f"[LOOP] No items with >= {min_chunks} chunks remain. Done after {round_num - 1} round(s).")
            return

        batch = [candidates[0]]
        total = candidates[0]["chunk_count"]
        for c in candidates[1:max_batch_size]:
            if total + c["chunk_count"] > batch_chunk_budget:
                break
            batch.append(c)
            total += c["chunk_count"]

        print(
            f"\n[LOOP] Round {round_num}: {len(candidates)} candidate(s) available, "
            f"processing {len(batch)} ({total} chunks total this round)"
        )
        for c in batch:
            print(f"  {c['library_id']:>12}:{c['item_key']}  {c['chunk_count']:>7} chunks  {c['title']!r}")

        targets_by_library: dict[str, list[str]] = {}
        for c in batch:
            targets_by_library.setdefault(c["library_id"], []).append(c["item_key"])

        try:
            results = await _reprocess_items(targets_by_library, vector_store, apply=True)
        except EmbeddingRateLimitExhaustedError as exc:
            wait_s = max(0.0, (exc.available_at - datetime.now(timezone.utc)).total_seconds()) + 5
            print(
                f"[LOOP] Embedding quota exhausted ({exc}); nothing in this round was "
                f"excluded. Waiting {wait_s:.0f}s until {exc.available_at.isoformat()} "
                f"before retrying this round from scratch."
            )
            await asyncio.sleep(wait_s)
            round_num -= 1  # this attempt didn't count as a real round
            continue
        except EmbeddingAuthenticationError as exc:
            print(
                f"[LOOP] Embedding API rejected credentials ({exc}) — stopping; "
                f"this needs a human to fix the key, waiting won't help."
            )
            return

        for key, status in results.items():
            if status in ("failed", "not_found", "skipped_no_key", "no_improvement"):
                permanently_excluded.add(key)


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
    parser.add_argument(
        "--loop", action="store_true",
        help="Repeat discovery+reprocess rounds until no candidates >= --min-chunks remain "
        "(implies --apply; incompatible with --item/--list)",
    )
    parser.add_argument(
        "--max-batch-size", type=int, default=10,
        help="Max items processed per --loop round (default 10)",
    )
    parser.add_argument(
        "--batch-chunk-budget", type=int, default=50_000,
        help="Max combined old chunk count per --loop round when batching smaller items; "
        "a single item already over budget is still processed alone (default 50000)",
    )
    args = parser.parse_args()

    vector_store = make_vector_store()

    if args.loop:
        if args.item or args.list:
            print("[ERROR] --loop is incompatible with --item/--list")
            sys.exit(1)
        asyncio.run(_loop(
            vector_store,
            args.min_chunks,
            max(args.top, 200),
            args.batch_chunk_budget,
            args.max_batch_size,
        ))
        return

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
