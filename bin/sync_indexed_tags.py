"""
Plan "indexed" tag changes for every Zotero library a key can access.

Compares each attachment's tags (read from the Zotero web API) with the backend's
indexed-attachment ground truth and emits the tag add/remove operations needed to
bring them in line. The stored keys are read-only, so this script cannot write
tags itself: the plugin applies the emitted operations to the local library.

Usage:
    uv run python bin/sync_indexed_tags.py --api-key <read-only-key> [--library-ids users/1 groups/2]

Output is JSON lines (one record per line, flushed per record), on stdout by
default or appended to --output-file. Record types: start, library_start, ops, applied,
progress, library_done, library_error, done, error. The backend runs it with
--fingerprint (key looked up in the encrypted auto-index store, never passed on
the command line) and --output-file, and relays the file to the plugin.
Re-running with nothing changed emits no ``ops`` records.

Manual runs can add --write-api-key <write-scoped-key> to apply the plan to zotero.org
directly (an ``applied`` record follows each ``ops`` record; the plugin then picks the tags
up through normal Zotero sync). --dry-run keeps the plan-only behavior even with a write key.
Without a write key the script only plans, which is what the server/plugin path uses.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan indexed-status tag changes for a key's libraries.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--api-key", metavar="KEY", help="Read-only Zotero API key (visible in `ps`; prefer --fingerprint on servers).")
    source.add_argument("--fingerprint", metavar="FP", help="Use the stored auto-index key with this fingerprint.")
    parser.add_argument("--write-api-key", metavar="KEY", default=None,
                        help="Separate write-scoped Zotero key: apply the planned tag changes to zotero.org directly "
                             "(manual use; the server path never has one). The read key above still enumerates libraries.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan and report only, even if --write-api-key is given.")
    parser.add_argument("--library-ids", nargs="+", metavar="SLUG", help="Restrict to these library slugs (users/<id>, groups/<id>).")
    parser.add_argument("--output-file", metavar="PATH", default=None, help="Append JSON lines here instead of stdout.")
    parser.add_argument("--run-id", default=None, help="Identifier echoed in the start record.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


async def _resolve_slugs(args: argparse.Namespace, settings) -> tuple[list[str], dict[str, str]]:
    """Return (slugs, {slug: zotero_key}) using the same resolution the cron indexer uses."""
    if args.api_key:
        from backend.zotero.key_validator import validate_key

        validation = await validate_key(args.api_key)
        if not validation.read_only:
            raise RuntimeError(validation.reason or "Key is not read-only.")
        return list(validation.targets), {s: args.api_key for s in validation.targets}

    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.services.autoindex_resolver import resolve_targets

    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        raise RuntimeError("AUTOINDEX_SECRET is not set; stored keys cannot be decrypted.")
    targets, _issues = await resolve_targets(
        store, only_fingerprint=args.fingerprint, require_embedding_key=False
    )
    mine = {s: t["zotero_key"] for s, t in targets.items() if t["fingerprint"] == args.fingerprint}
    if not mine:
        raise RuntimeError("No valid stored key for this fingerprint.")
    return sorted(mine), mine


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level, stream=sys.stderr, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    import psutil

    from backend.config.settings import get_settings
    from backend.dependencies import make_vector_store
    from backend.services.index_event_log import INDEXED_TAG_NAME, IndexEventLog
    from backend.services.indexed_tag_sync import IndexedTagSync, JsonLinesWriter
    from backend.zotero.web_api import ZoteroWebAPI

    settings = get_settings()
    emit = JsonLinesWriter(Path(args.output_file) if args.output_file else None, stdout=not args.output_file)
    emit({
        "type": "start",
        "run_id": args.run_id,
        "pid": os.getpid(),
        "pid_create_time": psutil.Process(os.getpid()).create_time(),
        "tag": INDEXED_TAG_NAME,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    vector_store = None
    try:
        slugs, keys = await _resolve_slugs(args, settings)
        if args.library_ids:
            unknown = [s for s in args.library_ids if s not in keys]
            for slug in unknown:
                emit({"type": "library_error", "library": slug, "error": "Not accessible with this key."})
            slugs = [s for s in slugs if s in args.library_ids]
        emit({"type": "libraries", "libraries": slugs})

        vector_store = make_vector_store()
        sync = IndexedTagSync(
            vector_store=vector_store,
            event_log=IndexEventLog(settings.index_events_path),
            emit=emit,
            web_api_factory=lambda slug: ZoteroWebAPI(api_key=keys[slug]),
            writer_factory=(lambda slug: ZoteroWebAPI(api_key=args.write_api_key)) if args.write_api_key else None,
            dry_run=args.dry_run,
        )
        totals = await sync.run(slugs)
        emit({"type": "done", "finished_at": datetime.now(timezone.utc).isoformat(), **totals})
        return 0
    except Exception as exc:  # noqa: BLE001 - reported as a terminal record
        logging.getLogger("sync_indexed_tags").error("Tag sync failed: %s", exc, exc_info=True)
        emit({"type": "error", "message": str(exc), "finished_at": datetime.now(timezone.utc).isoformat()})
        return 1
    finally:
        if vector_store is not None:
            vector_store.close()
        emit.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
