"""Background job that provisions (creates or wakes) a preset's remote endpoints.

A job runs ``Provider.provision()`` for each requested side in turn, in a
worker thread (provider methods do blocking HTTP). Sides are independent: a
failure on one side is recorded and the remaining sides still run, so a retry
can target just the failed side. Whatever a provider returns to be stored in
the shared remote config is applied as soon as its side succeeds.

In-memory job state per slot (not persisted): a run takes minutes at most, the
operations are idempotent, and a backend restart merely means the user clicks
the button again. A ``user``-scope job runs on the caller's own key in the
caller's slot, so two users can provision at once; a ``managed`` job uses the
admin's key and one global slot. Core knows nothing about any vendor; see backend.providers.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from backend.providers import Provider, ProvisionContext, ProvisionError, Side
from backend.services.endpoint_cache import endpoint_cache, paused_cache
from backend.services.admin_settings_store import update_remote_config

logger = logging.getLogger(__name__)

#: A whole job (all requested sides) may run this long; cold starts of large
#: models can take many minutes.
DEFAULT_JOB_TIMEOUT_SECONDS = 20 * 60
#: Newest progress lines kept in the state (oldest are dropped).
MAX_PROGRESS_LINES = 100

#: Slot of the single job shared by everyone (``managed`` credential scope).
GLOBAL_SLOT = "global"


def user_slot(user_id: Optional[int]) -> str:
    """Slot of one user's own job (``user`` credential scope); loopback has no identity."""
    return f"user:{user_id}" if user_id is not None else "user:local"


_lock = threading.Lock()
_ACTIVE = ("pending", "running")
_job_states: dict[str, dict] = {}


def _fresh_state() -> dict:
    return {
        "status": "idle",
        "message": None,
        "started_at": None,
        "finished_at": None,
        "progress": [],
        "sides": {},
    }


def _state(slot: str) -> dict:
    """The mutable state of one slot (created idle on first use); call with ``_lock`` held."""
    return _job_states.setdefault(slot, _fresh_state())


def get_job_state(slot: str = GLOBAL_SLOT) -> dict:
    """Snapshot of one slot's job state."""
    with _lock:
        state = _state(slot)
        return {**state, "progress": list(state["progress"]),
                "sides": {k: dict(v) for k, v in state["sides"].items()}}


def reset_job_state() -> None:
    """Forget every slot (used by tests)."""
    with _lock:
        _job_states.clear()


def is_running(slot: str = GLOBAL_SLOT, sides: Optional[list] = None) -> bool:
    """Whether a job is active in ``slot``; with ``sides``, whether one of those sides is."""
    with _lock:
        state = _state(slot)
        if sides is None:
            return state["status"] == "running"
        return any(state["sides"].get(side, {}).get("status") in _ACTIVE for side in sides)


def mark_running(sides: Optional[list] = None, slot: str = GLOBAL_SLOT) -> None:
    """Enter the running state for ``sides``; the caller must have checked ``is_running``.

    Sides are independent: another side's job already running in the slot (and the
    result of one that finished) is kept, only the restarted sides are reset.
    """
    with _lock:
        state = _state(slot)
        if state["status"] != "running":  # a new round: forget the previous one's progress
            state.update(_fresh_state())
        state.update(status="running", started_at=state["started_at"] or time.time(), finished_at=None, message=None)
        restarted = tuple(f"{side}: " for side in sides or [])
        state["progress"] = [line for line in state["progress"] if not line.startswith(restarted)] if restarted else state["progress"]
        for side in sides or []:
            state["sides"][side] = {"status": "pending", "message": None}


def _finish(slot: str, status: str, message: Optional[str], sides: Optional[list] = None) -> None:
    """Close one job; the slot stays 'running' while a job for another side is still active."""
    with _lock:
        state = _state(slot)
        if any(v.get("status") in _ACTIVE for v in state["sides"].values()):
            return
        failures = [
            f"{side}: {v['message']}" for side, v in state["sides"].items()
            if v.get("status") == "failed" and (sides is None or side in sides)
        ]
        if failures:
            status, message = "failed", "; ".join(failures)
        elif status == "failed":
            pass  # a crash before any side recorded its own failure
        else:
            status, message = "succeeded", None
        state.update(status=status, message=message, finished_at=time.time())


def _set_side(slot: str, side: str, status: str, message: Optional[str] = None) -> None:
    with _lock:
        _state(slot)["sides"][side] = {"status": status, "message": message}


def _add_progress(slot: str, side: str, message: str) -> None:
    with _lock:
        lines = _state(slot)["progress"]
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
    slot: str = GLOBAL_SLOT,
) -> None:
    """Run every side's job and record the outcome; never leaves the state 'running'.

    Args:
        jobs: Sides to run, in order (embedding before LLM).
        data_path: Data directory for the shared remote config.
        timeout_seconds: Budget for the whole job; each provider honours it
            through ``ctx.deadline``.
        action: What to run per side; defaults to ``provider.provision``. (Pause
            reuses this runner with ``provider.suspend``.)
        slot: Whose job this is (``GLOBAL_SLOT`` or ``user_slot(id)``).
    """
    deadline = time.monotonic() + timeout_seconds
    sides = [job.side for job in jobs]
    failures: list[str] = []
    try:
        for job in jobs:
            job.ctx.deadline = deadline
            _set_side(slot, job.side, "running")

            def progress(message: str, _side: str = job.side) -> None:
                _add_progress(slot, _side, message)

            try:
                if action is None:
                    values = await asyncio.to_thread(job.provider.provision, job.ctx, progress)
                else:
                    values = await asyncio.to_thread(action, job, progress)
                if values:
                    update_remote_config(values, data_path=data_path)
                # The side's endpoint may be new or recreated: forget any cached lookup.
                endpoint_cache.invalidate(job.provider.id, job.side, job.ctx.credential)
                paused_cache.invalidate(job.provider.id, job.side, job.ctx.credential)
                _set_side(slot, job.side, "succeeded")
            except Exception as exc:
                paused_cache.invalidate(job.provider.id, job.side, job.ctx.credential)
                logger.warning("Provisioning %s failed: %s", job.side, exc, exc_info=not isinstance(exc, ProvisionError))
                message = str(exc) or type(exc).__name__
                _set_side(slot, job.side, "failed", message)
                failures.append(f"{job.side}: {message}")
        if failures:
            _finish(slot, "failed", "; ".join(failures), sides)
        else:
            _finish(slot, "succeeded", None, sides)
    except Exception as exc:  # defensive: the state must never stay 'running'
        logger.exception("Provisioning job crashed")
        message = str(exc) or type(exc).__name__
        for side in sides:
            with _lock:
                if _state(slot)["sides"].get(side, {}).get("status") in _ACTIVE:
                    _state(slot)["sides"][side] = {"status": "failed", "message": message}
        _finish(slot, "failed", message, sides)
