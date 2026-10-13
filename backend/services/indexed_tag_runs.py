"""Spawn bin/sync_indexed_tags.py for a UI action and let the plugin tail its output.

Follows the precedent of ``autoindex_scheduler.trigger_index_run``: the script is
a detached subprocess (so a backend restart doesn't kill it) and progress goes
through a file the plugin polls over HTTP rather than a held-open pipe. Unlike
``cron_status.json`` (one overwritten snapshot) the output here is an ordered
stream of tag operations, so it is an append-only JSON-lines file read by byte
offset: ``<data>/system/indexed_tag_sync/<fingerprint>/<run_id>.jsonl``.
Runs are per key fingerprint, so one user can neither see nor start another's.
"""

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.config.settings import Settings
from backend.services.cron_indexer import is_process_alive

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL = ("done", "error")
# A spawned script that never wrote its "start" record (crashed on import)
# would otherwise look "running" forever.
_START_GRACE_SECONDS = 120
_KEEP_RUNS = 5
_MAX_RUN_AGE_SECONDS = 24 * 3600

_spawn_lock = asyncio.Lock()


def _runs_dir(settings: Settings, fingerprint: str) -> Path:
    return settings.indexed_tag_runs_path / fingerprint


def is_valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.match(run_id))


def _parse_lines(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return records


def run_state(path: Path) -> str:
    """"running", "done" or "failed" for a run file, with a liveness check."""
    records = _parse_lines(path)
    if any(r.get("type") == "done" for r in records):
        return "done"
    if any(r.get("type") == "error" for r in records):
        return "failed"
    start = next((r for r in records if r.get("type") == "start"), None)
    if start is not None:
        alive = is_process_alive(int(start["pid"]), start.get("pid_create_time"))
        return "running" if alive else "failed"
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return "failed"
    return "running" if age < _START_GRACE_SECONDS else "failed"


def read_run(settings: Settings, fingerprint: str, run_id: str, offset: int) -> Optional[dict]:
    """Return new records after byte ``offset``; None if the run is unknown.

    A run whose process died without a terminal record gets one synthesized and
    persisted, so every consumer sees the same final state.
    """
    path = _runs_dir(settings, fingerprint) / f"{run_id}.jsonl"
    if not path.exists():
        return None
    if run_state(path) == "failed" and not any(
        r.get("type") in _TERMINAL for r in _parse_lines(path)
    ):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "error", "message": "Tag sync process exited unexpectedly."}) + "\n")
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    # Only consume complete lines; a partially flushed trailing line waits for the next poll.
    complete = chunk[: chunk.rfind(b"\n") + 1]
    records = []
    for line in complete.decode("utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {
        "records": records,
        "offset": offset + len(complete),
        "done": any(r.get("type") in _TERMINAL for r in records) or run_state(path) != "running",
    }


def _prune(directory: Path) -> None:
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    for i, p in enumerate(files):
        if i >= _KEEP_RUNS or now - p.stat().st_mtime > _MAX_RUN_AGE_SECONDS:
            p.unlink(missing_ok=True)


async def trigger_tag_sync(settings: Settings, fingerprint: str) -> tuple[str, bool]:
    """Start a tag-sync run for ``fingerprint``, or attach to its active one.

    Returns (run_id, already_running).
    """
    directory = _runs_dir(settings, fingerprint)
    async with _spawn_lock:
        def _find_active() -> Optional[str]:
            if not directory.exists():
                return None
            files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files and run_state(files[0]) == "running":
                return files[0].stem
            return None

        active = await asyncio.to_thread(_find_active)
        if active:
            return active, True

        run_id = uuid.uuid4().hex

        def _prepare() -> Path:
            directory.mkdir(parents=True, exist_ok=True)
            _prune(directory)
            path = directory / f"{run_id}.jsonl"
            # Created before the spawn so the active-run check above sees it
            # immediately, closing the window before the script's own "start".
            path.touch()
            return path

        out_path = await asyncio.to_thread(_prepare)
        log_path = settings.data_path / "logs" / "sync_indexed_tags.log"

        def _open_log():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            return open(log_path, "ab")

        logf = await asyncio.to_thread(_open_log)
        try:
            await asyncio.create_subprocess_exec(
                sys.executable, str(_PROJECT_ROOT / "bin" / "sync_indexed_tags.py"),
                "--fingerprint", fingerprint,
                "--output-file", str(out_path),
                "--run-id", run_id,
                stdout=logf, stderr=logf, cwd=str(_PROJECT_ROOT),
            )
        finally:
            await asyncio.to_thread(logf.close)
        logger.info("Started indexed-tag sync %s at %s", run_id, datetime.now(timezone.utc).isoformat())
        return run_id, False
