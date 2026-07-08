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
from pathlib import Path
from typing import Literal, Optional

from backend.config.settings import Settings
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.cron_indexer import read_live_status

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_DELAY_SECONDS = 60


async def trigger_index_run(settings: Settings, fingerprint: Optional[str] = None) -> Literal["started", "already_running", "disabled"]:
    """Start a server-side indexing run if one isn't already active.

    fingerprint=None triggers an unscoped run covering every resolvable
    target (used by the scheduler and the admin run-now endpoint); a
    fingerprint scopes the run to that entry's own targets (used by the
    on-demand POST /api/autoindex/run endpoint).
    """
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        return "disabled"
    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if live_status.get("running"):
        return "already_running"
    await _spawn_index_run(settings, fingerprint)
    return "started"


async def run_scheduler_loop(settings: Settings) -> None:
    """Runs forever until cancelled. Ticks every AUTOINDEX_INTERVAL_MINUTES,
    triggering an unscoped (all-targets) indexing run via trigger_index_run().

    The tick body is wrapped in try/except Exception (re-raising
    CancelledError) deliberately: a single tick's failure (e.g. a transient
    exception in trigger_index_run itself, not the subprocess it spawns)
    must not kill the scheduler task permanently — the loop must keep
    ticking on the configured interval indefinitely.
    """
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if not read_scheduler_state(settings.data_path).get("paused", False):
                result = await trigger_index_run(settings)
                logger.info("Scheduler tick: %s", result)
            else:
                logger.debug("Scheduler tick skipped: paused by admin.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler tick failed unexpectedly; will retry next interval.")
        await asyncio.sleep(settings.autoindex_interval_minutes * 60)


async def _spawn_index_run(settings: Settings, fingerprint: Optional[str]) -> None:
    log_path = settings.data_path / "logs" / "cron_indexer.log"
    script_path = _PROJECT_ROOT / "bin" / "index_libraries.py"
    args = [sys.executable, str(script_path)]
    if fingerprint:
        args += ["--fingerprint", fingerprint]

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
