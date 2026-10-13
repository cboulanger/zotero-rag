"""In-process scheduler and pause-state control plane for auto-indexing.

Provides the shared "trigger an indexing run" logic used by both the
in-process scheduler loop (run_scheduler_loop) and the on-demand
POST /api/autoindex/run and POST /api/autoindex/scheduler/run-now endpoints.

Skip-slug control-state helpers live in backend.services.cron_indexer instead
of here, to avoid a circular import: this module needs read_live_status from
cron_indexer, and cron_indexer needs the skip-slug helpers — putting both
directions in the same pair of modules would create a cycle.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from backend.config.settings import Settings
from backend.services.secret_store import get_key_store
from backend.services.cron_indexer import read_live_status

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_DELAY_SECONDS = 60

# Guards the read-then-spawn sequence in trigger_index_run against concurrent
# calls within this process (the only process — UVICORN_WORKERS=1 in
# production). See _write_claim for why the lock alone isn't sufficient.
_trigger_lock = asyncio.Lock()

# How long a freshly-spawned run's "claim" (see _write_claim) is trusted
# before being ignored, in case the subprocess never got far enough to write
# its own status (e.g. it crashed on import before CronIndexer.run() could
# acquire its lock and report "running"). Generous relative to the ~1-1.5s
# typical subprocess startup observed in production logs.
_CLAIM_TTL_SECONDS = 10.0


async def trigger_index_run(
    settings: Settings, fingerprint: Optional[str] = None, slug: Optional[str] = None
) -> Literal["started", "already_running", "disabled"]:
    """Start a server-side indexing run if one isn't already active.

    fingerprint=None triggers an unscoped run covering every resolvable
    target (used by the scheduler and the admin run-now endpoint); a
    fingerprint scopes the run to that entry's own targets (used by the
    on-demand POST /api/autoindex/run endpoint). slug further restricts to
    a single library (used by the admin per-library run-slug endpoint, so
    an admin can target one library in between scheduled runs or after
    aborting the current one, without waiting for the next tick).

    Two near-simultaneous calls (e.g. a user double-clicking "Index" before
    the first click's subprocess has started up) could otherwise both read
    read_live_status() as "not running" and each spawn their own subprocess —
    observed in production as three concurrent indexing processes from three
    rapid clicks. _trigger_lock serializes the check-and-spawn sequence, and
    _write_claim/_claim_is_fresh close the remaining gap: the spawned
    subprocess itself (not this function) is what eventually writes
    "running": true to cron_status.json, which can take a second or more
    (interpreter startup, imports), so a second call arriving in that window
    would still see the stale "not running" status without the claim check.
    """
    store = get_key_store(settings)
    if not store.enabled:
        return "disabled"
    async with _trigger_lock:
        live_status = await asyncio.to_thread(read_live_status, settings.data_path)
        if live_status.get("running"):
            return "already_running"
        if await asyncio.to_thread(_claim_is_fresh, settings.data_path):
            return "already_running"
        await asyncio.to_thread(_write_claim, settings.data_path)
        await _spawn_index_run(settings, fingerprint, slug)
        return "started"


def _claim_path(data_path: Path) -> Path:
    return data_path / "system" / "autoindex_claim.json"


def _write_claim(data_path: Path) -> None:
    _atomic_write_json(_claim_path(data_path), {"claimed_at": datetime.now(timezone.utc).isoformat()})


def _claim_is_fresh(data_path: Path) -> bool:
    try:
        data = json.loads(_claim_path(data_path).read_text(encoding="utf-8"))
        claimed_at = datetime.fromisoformat(data["claimed_at"])
        age = (datetime.now(timezone.utc) - claimed_at).total_seconds()
        return 0 <= age < _CLAIM_TTL_SECONDS
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return False


async def run_scheduler_loop(settings: Settings) -> None:
    """Runs forever until cancelled. Ticks every AUTOINDEX_INTERVAL_MINUTES,
    triggering an unscoped (all-targets) indexing run via trigger_index_run().

    The tick body is wrapped in try/except Exception (re-raising
    CancelledError) deliberately: a single tick's failure (e.g. a transient
    exception in trigger_index_run itself, not the subprocess it spawns)
    must not kill the scheduler task permanently — the loop must keep
    ticking on the configured interval indefinitely.

    Persists next_tick_at to the scheduler state file after every tick, so
    the status dialog can show "Next run at ..." without guessing — see
    _record_next_tick.
    """
    if not settings.autoindex_interval_minutes:
        logger.error("run_scheduler_loop called without autoindex_interval_minutes set; exiting immediately.")
        return
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            state = await asyncio.to_thread(read_scheduler_state, settings.data_path)
            if not state.get("paused", False):
                result = await trigger_index_run(settings)
                logger.info("Scheduler tick: %s", result)
            else:
                logger.debug("Scheduler tick skipped: paused by admin.")
            next_tick_at = datetime.now(timezone.utc) + timedelta(seconds=settings.autoindex_interval_minutes * 60)
            await asyncio.to_thread(_record_next_tick, settings.data_path, next_tick_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed unexpectedly; will retry next interval.")
        await asyncio.sleep(settings.autoindex_interval_minutes * 60)


def update_scheduler_state(data_path: Path, **fields) -> dict:
    """Merge fields into the scheduler state file instead of overwriting it —
    used by pause_scheduler/resume_scheduler (paused) and the scheduler loop
    (next_tick_at) so neither write stomps on the other's field."""
    state = read_scheduler_state(data_path)
    state.update(fields)
    write_scheduler_state(data_path, state)
    return state


def _record_next_tick(data_path: Path, next_tick_at: datetime) -> None:
    update_scheduler_state(data_path, next_tick_at=next_tick_at.isoformat())


async def _spawn_index_run(settings: Settings, fingerprint: Optional[str], slug: Optional[str] = None) -> None:
    log_path = settings.data_path / "logs" / "cron_indexer.log"
    script_path = _PROJECT_ROOT / "bin" / "index_libraries.py"
    args = [sys.executable, str(script_path)]
    if fingerprint:
        args += ["--fingerprint", fingerprint]
    if slug:
        args += ["--slug", slug]

    def _open_log():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(log_path, "ab")

    logf = await asyncio.to_thread(_open_log)
    try:
        await asyncio.create_subprocess_exec(*args, stdout=logf, stderr=logf, cwd=str(_PROJECT_ROOT))
    finally:
        await asyncio.to_thread(logf.close)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a small JSON state file (Windows-safe via os.replace).

    Mirrors CronIndexer._write_status's pattern (backend/services/cron_indexer.py).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_scheduler_state(data_path: Path) -> dict:
    """Missing file (no admin has ever paused/resumed) reads as {} — the
    caller treats that as paused=False, today's implicit always-runs default."""
    state_path = data_path / "system" / "autoindex_scheduler_state.json"
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_scheduler_state(data_path: Path, state: dict) -> None:
    _atomic_write_json(data_path / "system" / "autoindex_scheduler_state.json", state)
