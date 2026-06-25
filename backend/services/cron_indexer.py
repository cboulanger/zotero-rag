"""
Cron-driven indexing service for Zotero libraries via the web API.

Designed to run as a standalone background process (via bin/index_libraries.py)
without requiring the Zotero desktop app or the plugin to be running.

Key behaviours:
- PID-based lock file prevents concurrent runs; a stale lock (dead PID) is
  automatically taken over.
- A JSON status file tracks per-slug progress and is consumed by the FastAPI
  root endpoint to surface cron state in /
- Atomic status writes use os.replace() for Windows compatibility.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from backend.api.public_query import slug_to_backend_id
from backend.db.vector_store import VectorStore
from backend.services.document_processor import DocumentProcessor
from backend.services.embeddings import EmbeddingService, EmbeddingRateLimitExhaustedError
from backend.zotero.web_api import ZoteroWebAPI

logger = logging.getLogger(__name__)

# If fewer than this fraction of the library's total Zotero items are indexed,
# the cron indexer forces a full re-index even in auto mode.  The threshold is
# deliberately conservative: most academic libraries have ≥ 25 % of items with
# indexable content, so falling below this almost always means something went wrong.
# The check is skipped when last_full_scan_indexable already proves the library
# legitimately has a low indexable-content ratio (see _index_slug).
_COMPLETENESS_THRESHOLD = 0.25


class AlreadyRunningError(Exception):
    """Raised when another cron indexer process is already running."""


@dataclass
class SlugInfo:
    """Parsed Zotero library slug with all ID representations."""

    slug: str           # "users/12345"
    library_type: str   # "user" | "group"
    library_id: str     # "u12345" (backend format — used for VectorStore)
    numeric_id: str     # "12345" (display and web API)


def is_process_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    if sys.platform == "win32":
        # On Windows, os.kill(pid, 0) opens the process handle; raises
        # PermissionError if alive but inaccessible, OSError if not found.
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True  # process exists, we just can't signal it
        except OSError:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # alive, insufficient permission


class CronIndexer:
    """Orchestrates web-API-based indexing for one or more Zotero library slugs."""

    def __init__(
        self,
        slugs: list[str],
        api_key: str,
        vector_store: VectorStore,
        embedding_service: EmbeddingService,
        lock_file: Path,
        status_file: Path,
        log: logging.Logger,
        mode: Literal["auto", "incremental", "full"] = "auto",
        max_items: Optional[int] = None,
        progress_update_interval: int = 10,
    ):
        self.slugs = slugs
        self.api_key = api_key
        self.vector_store = vector_store
        self.embedding_service = embedding_service
        self.lock_file = lock_file
        self.status_file = status_file
        self.log = log
        self.mode = mode
        self.max_items = max_items
        self.progress_update_interval = progress_update_interval
        # Slugs whose previous run was interrupted (stale lock takeover); set in run().
        self._interrupted_slugs: set[str] = set()

    # ------------------------------------------------------------------
    # Slug parsing
    # ------------------------------------------------------------------

    def parse_slug(self, slug: str) -> SlugInfo:
        """Parse a Zotero slug string into a SlugInfo.

        Accepts "users/{id}" or "groups/{id}". Raises ValueError for anything else.
        """
        parts = slug.strip().split("/")
        if len(parts) != 2 or parts[0] not in ("users", "groups"):
            raise ValueError(
                f"Invalid slug {slug!r}. Expected 'users/{{id}}' or 'groups/{{id}}'."
            )
        kind, numeric_id = parts
        library_type = "user" if kind == "users" else "group"
        library_id = slug_to_backend_id(slug)
        return SlugInfo(
            slug=slug,
            library_type=library_type,
            library_id=library_id,
            numeric_id=numeric_id,
        )

    # ------------------------------------------------------------------
    # Lock file
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> bool:
        """Write the current PID to the lock file, raising AlreadyRunningError
        if another live process holds the lock.

        Returns True if a stale lock was taken over (previous run was interrupted).
        """
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        stale = False
        if self.lock_file.exists():
            try:
                existing_pid = int(self.lock_file.read_text(encoding="utf-8").strip())
                if existing_pid <= 0:
                    raise ValueError(f"Invalid PID {existing_pid} in lock file")
                if is_process_alive(existing_pid):
                    raise AlreadyRunningError(
                        f"Cron indexer already running with PID {existing_pid}"
                    )
                self.log.warning(
                    "Stale lock file found (PID %s dead); previous run was interrupted.",
                    existing_pid,
                )
                stale = True
            except (ValueError, OSError):
                self.log.warning("Lock file unreadable or invalid; taking over.")
                stale = True
        self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
        self.log.debug("Lock acquired (PID %s)", os.getpid())
        return stale

    def _release_lock(self) -> None:
        try:
            self.lock_file.unlink(missing_ok=True)
        except OSError as exc:
            self.log.warning("Could not remove lock file: %s", exc)

    # ------------------------------------------------------------------
    # Status file
    # ------------------------------------------------------------------

    def _write_status(self, status: dict) -> None:
        """Atomically write the status JSON file (Windows-safe via os.replace)."""
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.status_file.parent, suffix=".tmp", prefix="cron_status_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, default=str)
            os.replace(tmp_path, self.status_file)  # atomic on POSIX, near-atomic on Windows
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _read_status(self) -> dict:
        try:
            return json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Index all configured slugs. Returns aggregated stats dict.

        Raises AlreadyRunningError if another instance is alive.
        """
        # Read before acquiring the lock — only the indexer writes this file, so no race risk.
        # Exit early if a previous run stored a future rate-limit expiry.
        existing = self._read_status()
        rate_limit_until_str = existing.get("embedding_rate_limit_until")
        if rate_limit_until_str:
            try:
                rate_limit_until = datetime.fromisoformat(rate_limit_until_str)
                if rate_limit_until.tzinfo is None:
                    rate_limit_until = rate_limit_until.replace(tzinfo=timezone.utc)
                if rate_limit_until > datetime.now(timezone.utc):
                    remaining = (rate_limit_until - datetime.now(timezone.utc)).total_seconds()
                    self.log.warning(
                        "Embedding service rate-limited until %s (%.0f s remaining). "
                        "Skipping this run.",
                        rate_limit_until_str,
                        remaining,
                    )
                    return {
                        "items_processed": 0,
                        "chunks_added": 0,
                        "libraries": [],
                        "skipped": "embedding_rate_limit",
                    }
            except ValueError:
                self.log.warning(
                    "Invalid embedding_rate_limit_until in status: %r", rate_limit_until_str
                )

        slug_infos = [self.parse_slug(s) for s in self.slugs]

        stale = self._acquire_lock()
        if stale and self.mode == "auto":
            # Identify slugs that were mid-index when the previous process died.
            # Force a full re-index for those: an interrupted full run may have
            # deleted existing chunks without finishing, and an interrupted
            # incremental run may have left partially-updated items.
            for slug, slug_status in existing.get("slugs", {}).items():
                if slug_status.get("status") == "indexing":
                    self._interrupted_slugs.add(slug)
                    self.log.warning(
                        "Previous run interrupted while indexing %s; forcing full re-index.",
                        slug,
                    )

        started_at = datetime.now(timezone.utc).isoformat()
        status: dict = {
            "running": True,
            "started_at": started_at,
            "pid": os.getpid(),
            "slugs": {s.slug: {"status": "pending"} for s in slug_infos},
        }

        total_stats: dict = {
            "items_processed": 0,
            "chunks_added": 0,
            "libraries": [],
        }

        try:
            self._write_status(status)  # inside try so a write failure releases the lock

            for slug_info in slug_infos:
                status["slugs"][slug_info.slug] = {
                    "status": "indexing",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "items_processed": 0,
                    "items_total": 0,
                    "chunks_added": 0,
                }
                self._write_status(status)
                self.log.info("Indexing %s (library_id=%s)", slug_info.slug, slug_info.library_id)

                slug_stats = await self._index_slug(slug_info, status)

                rate_limit_headers = await self.embedding_service.get_rate_limit_info()
                if rate_limit_headers:
                    status["last_rate_limit_headers"] = rate_limit_headers
                status["slugs"][slug_info.slug].update({
                    "status": "done",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    **slug_stats,
                })
                self._write_status(status)

                total_stats["items_processed"] += slug_stats.get("items_processed", 0)
                total_stats["chunks_added"] += slug_stats.get("chunks_added", 0)
                total_stats["libraries"].append(slug_info.slug)

        except EmbeddingRateLimitExhaustedError as exc:
            self.log.error(
                "Embedding quota exhausted: %s. Service available again at %s.",
                exc,
                exc.available_at.isoformat(),
            )
            status["embedding_rate_limit_until"] = exc.available_at.isoformat()
            rate_limit_headers = await self.embedding_service.get_rate_limit_info()
            if rate_limit_headers:
                status["last_rate_limit_headers"] = rate_limit_headers
            for si in slug_infos:
                if status["slugs"][si.slug].get("status") in ("pending", "indexing"):
                    status["slugs"][si.slug]["status"] = "skipped"
                    status["slugs"][si.slug]["skip_reason"] = "embedding_rate_limit"
            # Do not re-raise — quota exhaustion is an expected operational condition.

        except Exception as exc:
            self.log.error("Fatal error during cron indexing: %s", exc, exc_info=True)
            # Mark any slug that was in progress as errored
            for slug_info in slug_infos:
                if status["slugs"][slug_info.slug].get("status") in ("pending", "indexing"):
                    status["slugs"][slug_info.slug]["status"] = "error"
                    status["slugs"][slug_info.slug]["error"] = str(exc)
            raise
        finally:
            status["running"] = False
            status["finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                self._write_status(status)
            except Exception as exc:
                self.log.warning("Failed to write final cron status: %s", exc)
            self._release_lock()  # always runs even if _write_status raised

        return total_stats

    async def _resolve_mode(self, slug_info: SlugInfo, web_api: ZoteroWebAPI) -> str:
        """Return the indexing mode to use for this slug.

        Starts from self.mode and upgrades to "full" when either:
        - the previous run was interrupted (stale lock takeover), or
        - the library is under-indexed relative to its live Zotero item count.
        """
        if slug_info.slug in self._interrupted_slugs:
            self.log.info("Using full mode for %s (previous run was interrupted)", slug_info.slug)
            return "full"

        if self.mode != "auto":
            return self.mode

        # Only run the completeness check when the library has been indexed before
        # (last_indexed_version > 0); a brand-new library will get "full" from
        # DocumentProcessor's own auto logic.
        meta = self.vector_store.get_library_metadata(slug_info.library_id)
        if not meta or meta.last_indexed_version == 0:
            return "auto"

        zotero_total = await web_api.get_library_item_count(
            slug_info.numeric_id, slug_info.library_type
        )
        if zotero_total == 0:
            return "auto"

        indexed = meta.total_items_indexed
        ratio = indexed / zotero_total
        if ratio >= _COMPLETENESS_THRESHOLD:
            return "auto"

        # Ratio is below threshold — but only force full if this isn't explained by a
        # previous full scan that already established a low indexable-content ratio.
        scan_floor = meta.last_full_scan_indexable
        if scan_floor > 0 and indexed >= scan_floor * 0.9:
            # Library legitimately has few indexable items; current count is expected.
            return "auto"

        self.log.warning(
            "Library %s under-indexed: %d/%d items (%.0f%% < %.0f%%); forcing full re-index",
            slug_info.slug, indexed, zotero_total,
            ratio * 100, _COMPLETENESS_THRESHOLD * 100,
        )
        return "full"

    async def _index_slug(self, slug_info: SlugInfo, status: dict) -> dict:
        """Index a single library slug. Returns stats dict."""
        web_api = ZoteroWebAPI(api_key=self.api_key)
        counter = {"n": 0}  # mutable counter for closure

        def progress_callback(current: int, total: int, chunks_added: int) -> None:
            counter["n"] += 1
            entry = status["slugs"][slug_info.slug]
            entry["items_processed"] = current
            entry["items_total"] = total
            entry["chunks_added"] = chunks_added
            if counter["n"] % self.progress_update_interval == 0:
                self._write_status(status)

        try:
            async with web_api:
                mode = await self._resolve_mode(slug_info, web_api)
                processor = DocumentProcessor(
                    zotero_client=web_api,  # type: ignore[arg-type]  # duck-typed
                    embedding_service=self.embedding_service,
                    vector_store=self.vector_store,
                )
                stats = await processor.index_library(
                    library_id=slug_info.library_id,
                    library_type=slug_info.library_type,
                    library_name=slug_info.slug,
                    mode=mode,
                    progress_callback=progress_callback,
                    max_items=self.max_items,
                )
            self.log.info(
                "Finished %s: %s items, %s chunks added",
                slug_info.slug,
                stats.get("items_processed", 0),
                stats.get("chunks_added", 0),
            )
            return {
                "items_processed": stats.get("items_processed", 0),
                "chunks_added": stats.get("chunks_added", 0),
                "last_update": datetime.now(timezone.utc).isoformat(),
            }
        except EmbeddingRateLimitExhaustedError:
            raise  # let run() handle quota exhaustion centrally
        except Exception as exc:
            self.log.error("Error indexing %s: %s", slug_info.slug, exc, exc_info=True)
            status["slugs"][slug_info.slug]["status"] = "error"
            status["slugs"][slug_info.slug]["error"] = str(exc)
            self._write_status(status)
            return {"status": "error", "error": str(exc)}
