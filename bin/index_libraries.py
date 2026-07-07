"""
CLI script for cron-driven Zotero library indexing via the web API.

Usage:
    uv run python bin/index_libraries.py
    uv run python bin/index_libraries.py --mode full --max-items 500

Libraries and keys are resolved from the encrypted auto-index key store: every
stored read-only key is re-validated against api.zotero.org each run, revoked
keys are pruned, and the surviving keys are deduplicated into a {slug: key} map.
Requires AUTOINDEX_SECRET to decrypt the store; keys are submitted via the plugin.
All log output goes to data/logs/cron_indexer.log (overridable with --log-file).
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path so backend.* imports work when
# the script is run directly (uv run python bin/index_libraries.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _setup_logging(log_file: Path, log_level: str = "INFO") -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())
    root_logger.addHandler(file_handler)

    return logging.getLogger("cron_indexer")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index Zotero libraries via the web API (cron-friendly)."
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        default=None,
        help="Override log file path (default: data/logs/cron_indexer.log).",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "incremental", "full"],
        default="auto",
        help="Indexing mode (default: auto).",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        metavar="N",
        help="Limit indexing to N items per library (useful for testing).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip lock check (manual override for stuck processes).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Import after path setup
    from backend.config.settings import get_settings
    from backend.dependencies import make_vector_store
    from backend.services.cron_indexer import AlreadyRunningError, CronIndexer

    settings = get_settings()

    # Resolve log file
    log_file = Path(args.log_file) if args.log_file else settings.data_path / "logs" / "cron_indexer.log"
    log = _setup_logging(log_file, args.log_level)

    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.services.autoindex_resolver import resolve_targets

    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        log.error("AUTOINDEX_SECRET is not set; no keys can be decrypted. Nothing to index.")
        return 1

    targets, key_issues = await resolve_targets(store)
    for issue in key_issues:
        log.warning("Key pruned for user %s: %s", issue.get("user"), issue.get("reason"))

    if not targets:
        log.error("No valid auto-index keys found. Submit a read-only key via the plugin.")
        return 1

    log.info("Starting cron indexer for: %s", ", ".join(sorted(targets)))

    lock_file = settings.data_path / "system" / "cron_indexer.lock"
    status_file = settings.data_path / "system" / "cron_status.json"

    if args.force and lock_file.exists():
        log.warning("--force: removing existing lock file.")
        lock_file.unlink(missing_ok=True)

    try:
        vector_store = make_vector_store()

        indexer = CronIndexer(
            targets=targets,
            vector_store=vector_store,
            lock_file=lock_file,
            status_file=status_file,
            log=log,
            mode=args.mode,
            max_items=args.max_items,
            key_store=store,
        )
        indexer.key_issues = key_issues
        stats = await indexer.run()
        log.info(
            "Done. Total items processed: %s, chunks added: %s",
            stats.get("items_processed", 0),
            stats.get("chunks_added", 0),
        )
        return 0

    except AlreadyRunningError as exc:
        log.error("Cron indexer already running: %s", exc)
        return 1
    except Exception as exc:
        log.error("Cron indexer failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
