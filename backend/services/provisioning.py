"""Background job running a preset's ``provisioning_script``.

Single in-memory job state (not persisted): a run takes minutes at most, the
scripts are idempotent, and a backend restart merely means the admin clicks
the button again. See
docs/superpowers/specs/2026-10-09-endpoint-health-provisioning-design.md for
the script's ``PROVISION_RESULT:`` output contract.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from backend.services.admin_settings_store import update_remote_config

logger = logging.getLogger(__name__)

RESULT_PREFIX = "PROVISION_RESULT:"
# Env var through which a one-time provisioning key reaches the script. Passed
# via the subprocess environment (never argv, which `ps` would expose) and
# never persisted by the backend.
API_KEY_ENV = "PROVISIONING_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[2]

_job_state: dict = {"status": "idle", "message": None, "started_at": None, "finished_at": None}


def get_job_state() -> dict:
    """Snapshot of the current job state."""
    return dict(_job_state)


def reset_job_state() -> None:
    """Reset to idle (used by tests)."""
    _job_state.update(status="idle", message=None, started_at=None, finished_at=None)


def is_running() -> bool:
    return _job_state["status"] == "running"


def mark_running() -> None:
    _job_state.update(status="running", message=None, started_at=time.time(), finished_at=None)


def _finish(status: str, message: Optional[str]) -> None:
    _job_state.update(status=status, message=message, finished_at=time.time())


def parse_result(stdout: str) -> Optional[dict]:
    """The dict from the last ``PROVISION_RESULT:`` line, or None if absent/malformed."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                parsed = json.loads(line[len(RESULT_PREFIX):].strip())
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict) and all(isinstance(v, str) for v in parsed.values()):
                return parsed
            return None
    return None


async def start_job(script: str, api_key: Optional[str] = None) -> asyncio.subprocess.Process:
    """Start the provisioning subprocess; the caller must have called mark_running().

    ``api_key``, if given, is exposed to the script as ``PROVISIONING_API_KEY``.
    """
    env = dict(os.environ)
    if api_key:
        env[API_KEY_ENV] = api_key
    return await asyncio.create_subprocess_exec(
        "uv", "run", "python", str(REPO_ROOT / script), "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )


async def await_job(
    proc: asyncio.subprocess.Process, data_path: Path, extra_config: Optional[dict] = None,
) -> None:
    """Wait for the subprocess and record the outcome; never leaves state 'running'.

    On success, stores the script's reported values plus ``extra_config``
    (e.g. a first-time API key) in the shared remote config.
    """
    try:
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            _finish("failed", stderr.strip()[-500:] or f"Script exited with code {proc.returncode}")
            return
        values = parse_result(stdout)
        if values is None:
            _finish("failed", "Provisioning script did not report a result.")
            return
        update_remote_config(data_path, {**values, **(extra_config or {})})
        _finish("succeeded", None)
    except Exception as exc:
        logger.exception("Provisioning job failed")
        _finish("failed", str(exc) or type(exc).__name__)
