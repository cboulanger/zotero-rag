"""
CLI script for cron-driven Zotero library indexing via the web API.

Usage:
    uv run python bin/index_libraries.py users/12345 groups/678
    uv run python bin/index_libraries.py --slugs-file my_libraries.txt
    uv run python bin/index_libraries.py users/12345 --mode full --max-items 500

Reads ZOTERO_API_KEY from the environment (or .env at project root).
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
        "slugs",
        nargs="*",
        metavar="SLUG",
        help="Library slugs to index, e.g. users/12345 groups/678",
    )
    parser.add_argument(
        "--slugs-file",
        metavar="FILE",
        help="Text file with one slug per line (or whitespace-separated). "
             "Combined with positional slugs.",
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
    from backend.dependencies import make_embedding_service, make_vector_store
    from backend.services.cron_indexer import AlreadyRunningError, CronIndexer

    settings = get_settings()

    # Resolve log file
    log_file = Path(args.log_file) if args.log_file else settings.data_path / "logs" / "cron_indexer.log"
    log = _setup_logging(log_file, args.log_level)

    # Collect slugs
    slugs = list(args.slugs)
    if args.slugs_file:
        try:
            text = Path(args.slugs_file).read_text(encoding="utf-8")
            slugs.extend(text.split())
        except OSError as exc:
            log.error("Cannot read slugs file %s: %s", args.slugs_file, exc)
            return 1

    slugs = [s.strip() for s in slugs if s.strip()]
    if not slugs:
        log.error("No library slugs provided. Pass slugs as arguments or use --slugs-file.")
        return 1

    # API key
    api_key = settings.zotero_api_key
    if not api_key:
        log.error(
            "ZOTERO_API_KEY is not set. Add it to .env or set the environment variable."
        )
        return 1

    log.info("Starting cron indexer for: %s", ", ".join(slugs))

    lock_file = settings.data_path / "system" / "cron_indexer.lock"
    status_file = settings.data_path / "system" / "cron_status.json"

    if args.force and lock_file.exists():
        log.warning("--force: removing existing lock file.")
        lock_file.unlink(missing_ok=True)

    try:
        vector_store = make_vector_store()
        embedding_service = make_embedding_service()

        indexer = CronIndexer(
            slugs=slugs,
            api_key=api_key,
            vector_store=vector_store,
            embedding_service=embedding_service,
            lock_file=lock_file,
            status_file=status_file,
            log=log,
            mode=args.mode,
            max_items=args.max_items,
        )
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
