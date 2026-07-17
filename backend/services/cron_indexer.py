"""
Cron-driven indexing service for Zotero libraries via the web API.

Designed to run as a standalone background process (via bin/index_libraries.py)
without requiring the Zotero desktop app or the plugin to be running.

Key behaviours:
- PID-based lock file prevents concurrent runs; a stale lock (dead PID) is
  automatically taken over.
- A JSON status file tracks per-slug progress and is surfaced by the
  authenticated GET /api/autoindex/status endpoint (see read_live_status).
- Atomic status writes use os.replace() for Windows compatibility.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from filelock import FileLock, Timeout

from backend.api.public_query import slug_to_backend_id
from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.document_processor import DocumentProcessor
from backend.services.embeddings import (
    EmbeddingAuthenticationError,
    EmbeddingRateLimitExhaustedError,
    create_embedding_service,
)
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


class SlugSkipRequested(Exception):
    """Raised to unwind out of indexing the current slug when an admin requests a skip."""


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


def abort_process(pid: int) -> bool:
    """Send a termination signal to a running cron-indexer process.

    Returns False if the process was already gone. Uses the same POSIX/Windows
    branching as is_process_alive(): SIGTERM on POSIX (kernel releases the
    process's flock automatically, exactly as on a crash), TerminateProcess
    via os.kill(pid, signal.SIGTERM) on Windows (Python maps this to
    TerminateProcess for non-Python-created handles).
    """
    if not is_process_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False  # process exited in the window between the check and the signal
    except PermissionError:
        return True  # process is alive but we can't signal it — same "alive" semantics is_process_alive uses
    return True


def read_live_status(data_path: Path) -> dict:
    """Return the last cron run's live status from ``system/cron_status.json``.

    Applies a liveness check: if the status file claims ``running=True`` but the
    recorded PID is no longer alive, the run is reported as crashed. Returns an
    empty dict when no status file exists yet (no cron run has happened).
    """
    status_path = data_path / "system" / "cron_status.json"
    if not status_path.exists():
        return {}
    cron_data = json.loads(status_path.read_text(encoding="utf-8"))
    if cron_data.get("running") and cron_data.get("pid"):
        if not is_process_alive(int(cron_data["pid"])):
            cron_data["running"] = False
            cron_data["crashed"] = True
    return cron_data


def read_control_state(data_path: Path) -> dict:
    """Return the current admin skip-slug control request, or {} if none."""
    control_path = data_path / "system" / "autoindex_control.json"
    try:
        return json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_control_state(data_path: Path, state: dict) -> None:
    """Atomically write the control-state JSON file (same pattern as CronIndexer._write_status)."""
    control_path = data_path / "system" / "autoindex_control.json"
    control_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=control_path.parent, suffix=".tmp", prefix="autoindex_control_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, control_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def clear_control_state(data_path: Path, matched_slug: str) -> None:
    """Clear the skip request only if it still targets matched_slug — avoids
    clobbering a newer, unrelated request that may have arrived in between."""
    current = read_control_state(data_path)
    if current.get("skip_slug") == matched_slug:
        write_control_state(data_path, {"skip_slug": None, "requested_at": None})


class CronIndexer:
    """Orchestrates web-API-based indexing for one or more Zotero library slugs."""

    def __init__(
        self,
        targets: dict[str, dict],
        vector_store: VectorStore,
        lock_file: Path,
        status_file: Path,
        log: logging.Logger,
        mode: Literal["auto", "incremental", "full"] = "auto",
        max_items: Optional[int] = None,
        progress_update_interval: int = 10,
        key_store: Optional[AutoIndexKeyStore] = None,
    ):
        # targets maps each slug ("users/12345" or "groups/678") to a dict
        # {"zotero_key", "embedding_key", "embedding_key_name", "fingerprint"} —
        # the fingerprint identifies which stored auto-index entry owns this
        # slug, so a per-user embedding key failure can be recorded back onto
        # that entry via key_store.
        self.targets = targets
        self.slugs = list(targets.keys())
        self.vector_store = vector_store
        self.lock_file = lock_file
        self.status_file = status_file
        self.log = log
        self.mode = mode
        self.max_items = max_items
        self.progress_update_interval = progress_update_interval
        self.key_store = key_store
        # Dedicated flock target, separate from self.lock_file (the latter is
        # just a human-readable PID marker and is safe to delete on release —
        # deleting the flock's own target while another process holds a lock
        # on it would reopen the exact TOCTOU race this class is meant to close).
        self._flock_path = Path(str(self.lock_file) + ".flock")
        # Atomic OS-level lock acquired in _acquire_lock(); None until then.
        self._file_lock: Optional[FileLock] = None
        # Pruned-key issues from re-validation; set by the caller before run().
        self.key_issues: list[dict] = []
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
        """Atomically acquire the indexer's exclusive run lock via a non-blocking
        OS-level file lock (flock on POSIX, msvcrt on Windows) on a dedicated,
        never-deleted lock target (self._flock_path) — atomic by construction,
        and automatically released by the kernel if the holding process
        crashes, so no PID/liveness bookkeeping is needed for exclusion itself.
        self.lock_file is written with the current PID purely for operator
        visibility (e.g. inspecting it during an incident) and is safe to
        delete on release since it is not the lock's own target.

        Returns True if a stale lock file (left behind by a process that
        crashed mid-run, without a live holder) was found and taken over.
        """
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        stale = self.lock_file.exists()  # check BEFORE writing the fresh marker
        self._file_lock = FileLock(str(self._flock_path))
        try:
            self._file_lock.acquire(timeout=0)
        except Timeout:
            raise AlreadyRunningError("Cron indexer already running (lock held by another process)")
        if stale:
            self.log.warning("Stale lock file found; previous run was interrupted.")
        self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
        self.log.debug("Lock acquired (PID %s)", os.getpid())
        return stale

    def _release_lock(self) -> None:
        try:
            if self._file_lock is not None:
                self._file_lock.release()
        except OSError as exc:
            self.log.warning("Could not release file lock: %s", exc)
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
        existing = self._read_status()

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
            "key_issues": getattr(self, "key_issues", []),
        }

        total_stats: dict = {
            "items_processed": 0,
            "chunks_added": 0,
            "libraries": [],
        }

        try:
            self._write_status(status)  # inside try so a write failure releases the lock

            for slug_info in slug_infos:
                control = read_control_state(get_settings().data_path)
                if control.get("skip_slug") == slug_info.slug:
                    status["slugs"][slug_info.slug] = {
                        "status": "skipped",
                        "skip_reason": "Skipped by admin request",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self._write_status(status)
                    clear_control_state(get_settings().data_path, matched_slug=slug_info.slug)
                    continue

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

                rate_limit_headers = slug_stats.pop("rate_limit_headers", None)
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
            try:
                write_control_state(get_settings().data_path, {"skip_slug": None, "requested_at": None})
            except Exception as exc:
                self.log.warning("Failed to clear control state at end of run: %s", exc)
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
        target = self.targets[slug_info.slug]
        web_api = ZoteroWebAPI(api_key=target["zotero_key"])
        counter = {"n": 0}  # mutable counter for closure
        last_write = {"t": time.monotonic()}
        # Full-sync mode calls progress_callback once per subprocess batch (coarse:
        # each call can represent minutes of work), while incremental mode calls it
        # once per item (fine-grained: many calls per second). progress_update_interval
        # alone throttles the latter well but starves the former — a batch-sized library
        # can go the entire run without a single status write, leaving the plugin's
        # status dialog stuck at "0/0" even though real progress is happening. The time
        # floor guarantees a write shows up within a few seconds regardless of call
        # granularity; the first call also always writes so items_total is visible
        # immediately instead of after the first throttle window.
        _MIN_WRITE_INTERVAL_SECONDS = 3.0

        def progress_callback(current: int, total: int, chunks_added: int) -> None:
            counter["n"] += 1
            entry = status["slugs"][slug_info.slug]
            entry["items_processed"] = current
            entry["items_total"] = total
            entry["chunks_added"] = chunks_added
            now = time.monotonic()
            due = (
                counter["n"] == 1
                or counter["n"] % self.progress_update_interval == 0
                or now - last_write["t"] >= _MIN_WRITE_INTERVAL_SECONDS
            )
            if due:
                last_write["t"] = now
                self._write_status(status)
                control = read_control_state(get_settings().data_path)
                if control.get("skip_slug") == slug_info.slug:
                    raise SlugSkipRequested(slug_info.slug)

        preset = get_settings().get_hardware_preset()
        embedding_service = create_embedding_service(preset.embedding, api_key=target["embedding_key"])

        try:
            async with web_api:
                mode = await self._resolve_mode(slug_info, web_api)
                processor = DocumentProcessor(
                    zotero_client=web_api,  # type: ignore[arg-type]  # duck-typed
                    embedding_service=embedding_service,
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
                "rate_limit_headers": await embedding_service.get_rate_limit_info(),
            }
        except EmbeddingAuthenticationError as exc:
            fp = target.get("fingerprint")
            if fp and self.key_store:
                self.key_store.set_embedding_key_status(fp, "invalid")
            self.log.error("Embedding API rejected credentials for %s: %s", slug_info.slug, exc)
            error_message = f"Embedding API authentication failed: {exc}"
            status["slugs"][slug_info.slug]["status"] = "error"
            status["slugs"][slug_info.slug]["error"] = error_message
            self._write_status(status)
            return {"status": "error", "error": error_message}
        except EmbeddingRateLimitExhaustedError as exc:
            fp = target.get("fingerprint")
            if fp and self.key_store:
                self.key_store.set_embedding_key_status(
                    fp, "rate_limited", rate_limit_until=exc.available_at.isoformat()
                )
            self.log.warning(
                "Embedding quota exhausted for %s: available again at %s", slug_info.slug, exc.available_at
            )
            status["slugs"][slug_info.slug]["status"] = "skipped"
            status["slugs"][slug_info.slug]["skip_reason"] = "embedding_rate_limit"
            self._write_status(status)
            return {"status": "skipped", "skip_reason": "embedding_rate_limit"}
        except SlugSkipRequested:
            self.log.info("Skip requested by admin for %s; moving to next slug.", slug_info.slug)
            status["slugs"][slug_info.slug]["status"] = "skipped"
            status["slugs"][slug_info.slug]["skip_reason"] = "Skipped by admin request"
            self._write_status(status)
            clear_control_state(get_settings().data_path, matched_slug=slug_info.slug)
            return {"status": "skipped", "skip_reason": "Skipped by admin request"}
        except Exception as exc:
            self.log.error("Error indexing %s: %s", slug_info.slug, exc, exc_info=True)
            status["slugs"][slug_info.slug]["status"] = "error"
            status["slugs"][slug_info.slug]["error"] = str(exc)
            self._write_status(status)
            return {"status": "error", "error": str(exc)}
