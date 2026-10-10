"""Background job that provisions (creates or wakes) a preset's remote endpoints.

A job runs ``Provider.provision()`` for each requested side in turn, in a
worker thread (provider methods do blocking HTTP). Sides are independent: a
failure on one side is recorded and the remaining sides still run, so a retry
can target just the failed side. Whatever a provider returns to be stored in
the shared remote config is applied as soon as its side succeeds.

Single in-memory job state (not persisted): a run takes minutes at most, the
operations are idempotent, and a backend restart merely means the admin clicks
the button again. Core knows nothing about any vendor; see backend.providers.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from backend.providers import Provider, ProvisionContext, ProvisionError, Side
from backend.services.endpoint_cache import endpoint_cache
from backend.services.admin_settings_store import update_remote_config

logger = logging.getLogger(__name__)

#: A whole job (all requested sides) may run this long; cold starts of large
#: models can take many minutes.
DEFAULT_JOB_TIMEOUT_SECONDS = 20 * 60
#: Newest progress lines kept in the state (oldest are dropped).
MAX_PROGRESS_LINES = 100

_lock = threading.Lock()
_job_state: dict = {}


def _fresh_state() -> dict:
    return {
        "status": "idle",
        "message": None,
        "started_at": None,
        "finished_at": None,
        "progress": [],
        "sides": {},
    }


_job_state.update(_fresh_state())


def get_job_state() -> dict:
    """Snapshot of the current job state."""
    with _lock:
        return {**_job_state, "progress": list(_job_state["progress"]),
                "sides": {k: dict(v) for k, v in _job_state["sides"].items()}}


def reset_job_state() -> None:
    """Reset to idle (used by tests)."""
    with _lock:
        _job_state.clear()
        _job_state.update(_fresh_state())


def is_running() -> bool:
    return _job_state["status"] == "running"


def mark_running(sides: Optional[list] = None) -> None:
    """Enter the running state; the caller must have checked ``is_running()``."""
    with _lock:
        _job_state.clear()
        _job_state.update(_fresh_state())
        _job_state.update(status="running", started_at=time.time())
        for side in sides or []:
            _job_state["sides"][side] = {"status": "pending", "message": None}


def _finish(status: str, message: Optional[str]) -> None:
    with _lock:
        _job_state.update(status=status, message=message, finished_at=time.time())


def _set_side(side: str, status: str, message: Optional[str] = None) -> None:
    with _lock:
        _job_state["sides"][side] = {"status": status, "message": message}


def _add_progress(side: str, message: str) -> None:
    with _lock:
        lines = _job_state["progress"]
        lines.append(f"{side}: {message}")
        del lines[:-MAX_PROGRESS_LINES]


@dataclass
class SideJob:
    """One side to provision: its provider and the context to run it with."""

    side: Side
    provider: Provider
    ctx: ProvisionContext


async def run_job(
    jobs: list[SideJob],
    data_path: Optional[Path] = None,
    timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    action: Optional[Callable[[SideJob, Callable[[str], None]], dict]] = None,
) -> None:
    """Run every side's job and record the outcome; never leaves the state 'running'.

    Args:
        jobs: Sides to run, in order (embedding before LLM).
        data_path: Data directory for the shared remote config.
        timeout_seconds: Budget for the whole job; each provider honours it
            through ``ctx.deadline``.
        action: What to run per side; defaults to ``provider.provision``. (Pause
            reuses this runner with ``provider.suspend``.)
    """
    deadline = time.monotonic() + timeout_seconds
    failures: list[str] = []
    try:
        for job in jobs:
            job.ctx.deadline = deadline
            _set_side(job.side, "running")

            def progress(message: str, _side: str = job.side) -> None:
                _add_progress(_side, message)

            try:
                if action is None:
                    values = await asyncio.to_thread(job.provider.provision, job.ctx, progress)
                else:
                    values = await asyncio.to_thread(action, job, progress)
                if values:
                    update_remote_config(values, data_path=data_path)
                # The side's endpoint may be new or recreated: forget any cached lookup.
                endpoint_cache.invalidate(job.provider.id, job.side, job.ctx.credential)
                _set_side(job.side, "succeeded")
            except Exception as exc:
                logger.warning("Provisioning %s failed: %s", job.side, exc, exc_info=not isinstance(exc, ProvisionError))
                message = str(exc) or type(exc).__name__
                _set_side(job.side, "failed", message)
                failures.append(f"{job.side}: {message}")
        if failures:
            _finish("failed", "; ".join(failures))
        else:
            _finish("succeeded", None)
    except Exception as exc:  # defensive: the state must never stay 'running'
        logger.exception("Provisioning job crashed")
        _finish("failed", str(exc) or type(exc).__name__)
