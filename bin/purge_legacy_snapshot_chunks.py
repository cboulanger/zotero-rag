"""
TEMPORARY cleanup script — delete this file once a `--list` run reports zero
candidates against every Qdrant store that was ever affected (this dev
instance, and production). It exists only to fix a one-time data-quality bug
and has no ongoing purpose once that bug's instances are all cleaned up.

Background (see issue #80/#87): Zotero webpage-snapshot attachments (title
exactly "Snapshot") are supposed to be excluded from indexing by default
(`index_snapshots` admin setting, `backend/services/document_processor.py`'s
`_is_indexable_attachment`). The admin-only `POST /api/admin/settings/purge-
snapshots` endpoint (`backend/api/admin_settings.py`) can retroactively
delete any that got indexed anyway — but only chunks written with schema
version >= 7, which is when the `attachment_title` field (what that endpoint
filters on) was added to chunk payloads. Every chunk written *before* that
has `attachment_title: None` and can never match that filter, regardless of
what attachment it actually came from — see
`docs/superpowers/specs/2026-10-07-configurable-snapshot-indexing-design.md`,
which calls this out explicitly ("old data is inert with respect to this
feature until re-indexed").

In production this is silent and can sit indefinitely: incremental indexing
only reprocesses an item when its Zotero version changes, so a legacy
Snapshot chunk with garbage extracted content (e.g. a saved webpage whose
"text" is literal base64-encoded binary data) can keep outranking the item's
real PDF attachment in retrieval forever, with nothing short of this script
(or the item happening to get edited in Zotero) ever fixing it. Confirmed
once in the test-rag-plugin dev library (hysemrag2025 / item E6ZZZE85):
`bin/reindex_oversized_items.py --item 6297749:E6ZZZE85 --apply` fixed that
one item by hand once it was found; this script automates *finding* every
other item in the same situation and applies the same fix.

Approach: a chunk's own payload can't tell you whether its source attachment
was a Snapshot — `attachment_title` is exactly the field that would say so,
and it's missing on precisely the chunks this script cares about. So instead
this discovers every (library_id, item_key) with at least one legacy
(schema_version < 7) chunk, collects the distinct attachment_keys those
chunks came from, and asks the Zotero Web API directly what each
attachment's *current* title is. Items with at least one such attachment
currently titled "Snapshot" are reprocessed through the current pipeline
(force_extraction=True, same reindex-then-swap safety as
bin/reindex_oversized_items.py: old chunks are only deleted after confirming
reprocessing actually produced replacement content), which — since
index_snapshots is false — will stop indexing that Snapshot attachment and,
for free, pick up any other un-indexed real attachment on the same item.

Usage
-----
    # Discovery only (no changes) — reports every affected item.
    uv run python bin/purge_legacy_snapshot_chunks.py

    # Reprocess every affected item for real.
    uv run python bin/purge_legacy_snapshot_chunks.py --apply

    # Restrict to one library.
    uv run python bin/purge_legacy_snapshot_chunks.py --library 6297749 --apply

Must be run with the same environment as the cron indexer (AUTOINDEX_SECRET,
QDRANT_URL, etc.) — e.g. inside the production container via `podman exec`,
or locally against a dev data dir.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter, MatchValue, Range

# Must be run from the project root so backend package is importable.
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.config.settings import get_settings  # noqa: E402
from backend.dependencies import make_vector_store  # noqa: E402
from backend.providers import get_provider_or_none  # noqa: E402
from backend.services.effective_preset import preset_for_target  # noqa: E402
from backend.db.vector_store import VectorStore  # noqa: E402
from backend.services.embeddings import create_embedding_service  # noqa: E402
from backend.services.secret_store import get_key_store  # noqa: E402
from backend.services.autoindex_resolver import resolve_targets  # noqa: E402
from backend.services.document_processor import DocumentProcessor, merge_download_failures  # noqa: E402
from backend.zotero.web_api import ZoteroWebAPI  # noqa: E402

# Fixed forever at 7 — the schema version that introduced attachment_title —
# regardless of later CURRENT_SCHEMA_VERSION bumps in backend/models/document.py.
_ATTACHMENT_TITLE_SCHEMA_VERSION = 7


def _library_type(library_id: str) -> str:
    return "user" if library_id.startswith("u") else "group"


def _slug_for(library_id: str) -> str:
    kind = "users" if _library_type(library_id) == "user" else "groups"
    numeric = library_id[1:] if library_id.startswith("u") else library_id
    return f"{kind}/{numeric}"


def _discover_legacy_attachments(
    vector_store: VectorStore, library_id: str | None
) -> dict[tuple[str, str], set[str]]:
    """Scroll the chunks collection for legacy (schema_version < _ATTACHMENT_TITLE_SCHEMA_VERSION)
    points and return {(library_id, item_key): {attachment_key, ...}} — the distinct
    legacy attachment_keys each item's current chunks came from. Not yet filtered by
    title; that requires a Zotero API lookup, done per-library by the caller."""
    must = [FieldCondition(key="schema_version", range=Range(lt=_ATTACHMENT_TITLE_SCHEMA_VERSION))]
    if library_id:
        must.append(FieldCondition(key="library_id", match=MatchValue(value=library_id)))

    candidates: dict[tuple[str, str], set[str]] = {}
    offset = None
    while True:
        points, offset = vector_store.client.scroll(
            collection_name=VectorStore.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=must),
            limit=1000,
            offset=offset,
            with_payload=["library_id", "item_key", "attachment_key"],
            with_vectors=False,
        )
        for point in points:
            lib = point.payload.get("library_id")
            item_key = point.payload.get("item_key")
            attachment_key = point.payload.get("attachment_key")
            if lib and item_key and attachment_key:
                candidates.setdefault((lib, item_key), set()).add(attachment_key)
        if offset is None:
            break
    return candidates


async def _filter_to_snapshot_items(
    candidates: dict[tuple[str, str], set[str]],
    store,
) -> dict[tuple[str, str], list[str]]:
    """Of the discovered (library_id, item_key) candidates, keep only those where at
    least one legacy attachment_key currently has Zotero title "Snapshot". Returns
    {(library_id, item_key): [matching attachment_key, ...]} for reporting."""
    by_library: dict[str, dict[str, str]] = {}  # library_id -> {attachment_key: item_key}
    for (library_id, item_key), attachment_keys in candidates.items():
        for ak in attachment_keys:
            by_library.setdefault(library_id, {})[ak] = item_key

    auto_targets, key_issues = await resolve_targets(store)
    for issue in key_issues:
        print(f"[WARN] Key pruned for user {issue.get('user')}: {issue.get('reason')}")

    confirmed: dict[tuple[str, str], list[str]] = {}
    for library_id, attachment_to_item in by_library.items():
        slug = _slug_for(library_id)
        target = auto_targets.get(slug)
        if not target:
            print(f"[SKIP] No auto-index key found for {slug} (library_id={library_id}) — cannot check attachment titles")
            continue

        library_type = _library_type(library_id)
        web_api = ZoteroWebAPI(api_key=target["zotero_key"])
        async with web_api:
            attachments = await web_api.get_items_by_keys(
                library_id, list(attachment_to_item), library_type
            )
        for attachment in attachments:
            data = attachment.get("data", {})
            if data.get("title") == "Snapshot":
                item_key = attachment_to_item[data.get("key")]
                confirmed.setdefault((library_id, item_key), []).append(data.get("key"))

    return confirmed


async def _reprocess(
    confirmed: dict[tuple[str, str], list[str]], vector_store: VectorStore
) -> None:
    settings = get_settings()
    store = get_key_store()
    auto_targets, _ = await resolve_targets(store)
    preset = settings.get_hardware_preset()

    by_library: dict[str, list[str]] = {}
    for library_id, item_key in confirmed:
        by_library.setdefault(library_id, []).append(item_key)

    for library_id, item_keys in by_library.items():
        slug = _slug_for(library_id)
        target = auto_targets.get(slug)
        if not target:
            print(f"[SKIP] No auto-index key found for {slug} (library_id={library_id})")
            continue

        library_type = _library_type(library_id)
        web_api = ZoteroWebAPI(api_key=target["zotero_key"])
        target_preset = preset_for_target(settings, target)
        embedding_service = create_embedding_service(
            target_preset.embedding, api_key=target["embedding_key"], data_path=settings.data_path,
            provider=get_provider_or_none(target_preset, "embedding"),
        )
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
                    continue

                title = item["data"].get("title", "Untitled")
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
                    continue

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
                        continue

                vector_store.delete_chunks_by_ids(old_ids)
                print(
                    f"[DONE] {library_id}:{item_key} ({title!r}): "
                    f"{old_count} -> {new_count} chunks ({elapsed:.1f}s)"
                )

        metadata = vector_store.get_library_metadata(library_id)
        if metadata is not None:
            metadata.total_chunks = vector_store.count_library_chunks(library_id)
            if processor._download_failures:
                metadata.last_scan_failed_downloads = merge_download_failures(
                    metadata.last_scan_failed_downloads, processor._download_failures
                )
            vector_store.update_library_metadata(metadata)


async def _run(library_id: str | None, apply: bool) -> None:
    vector_store = make_vector_store()
    store = get_key_store()
    if not store.enabled:
        print("[ERROR] AUTOINDEX_SECRET is not set; cannot decrypt stored Zotero keys.")
        sys.exit(1)

    print("Scanning the chunks collection for legacy (pre-schema-v7) chunks...")
    candidates = _discover_legacy_attachments(vector_store, library_id)
    if not candidates:
        print("[PASS] No legacy chunks found — nothing to check.")
        return

    print(f"Found {len(candidates)} item(s) with legacy chunks; checking current Zotero titles...")
    confirmed = await _filter_to_snapshot_items(candidates, store)

    if not confirmed:
        print("[PASS] None of the legacy chunks' attachments are currently titled \"Snapshot\".")
        return

    print(f"[FOUND] {len(confirmed)} item(s) with a legacy Snapshot-attachment chunk:")
    for (library_id_, item_key), attachment_keys in confirmed.items():
        print(f"  {library_id_}:{item_key}  (snapshot attachment(s): {', '.join(attachment_keys)})")

    if not apply:
        print("\nRe-run with --apply to reprocess these items.")
        return

    await _reprocess(confirmed, vector_store)


def main():
    parser = argparse.ArgumentParser(
        description="Find and fix items whose legacy (pre-schema-v7) chunks came from a "
        "Snapshot attachment that the normal purge-snapshots endpoint can't catch."
    )
    parser.add_argument("--library", default=None, help="Restrict to this library_id (default: all libraries)")
    parser.add_argument("--apply", action="store_true", help="Reprocess affected items (default: report only)")
    args = parser.parse_args()

    asyncio.run(_run(args.library, args.apply))


if __name__ == "__main__":
    main()
