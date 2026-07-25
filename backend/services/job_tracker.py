"""In-memory job registry for on-demand, human-triggered background scripts
(chapter analyze/OCR/retrofit-link/segment-upload).

Mirrors backend/api/document_upload.py's _UploadTask pattern (in-memory dict
+ lock, pruned after an hour) rather than the subprocess/lock-file scheme in
cron_indexer.py — these are on-demand runs, not unattended scheduled jobs, so
job state does not need to survive a server restart. See design spec §4.
"""

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Optional

_PRUNE_AFTER_SECONDS = 3600


@dataclass
class JobStatus:
    job_id: str
    status: str = "processing"  # "processing" | "done" | "error"
    progress: float = 0.0
    message: str = ""
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)


class JobTracker:
    """Thread-safe in-memory job registry keyed by job_id."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobStatus] = {}
        self._lock = Lock()

    def create(self) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = JobStatus(job_id=job_id)
        return job_id

    def update(
        self,
        job_id: str,
        *,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if progress is not None:
                job.progress = progress
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
                job.status = "done"
            if error is not None:
                job.error = error
                job.status = "error"

    def get(self, job_id: str) -> Optional[JobStatus]:
        now = time.monotonic()
        with self._lock:
            stale = [jid for jid, j in self._jobs.items() if now - j.created_at > _PRUNE_AFTER_SECONDS]
            for jid in stale:
                del self._jobs[jid]
            return self._jobs.get(job_id)


def make_progress_callback(tracker: JobTracker, job_id: str) -> Callable[[float, str], None]:
    """Return a progress_callback(progress, message) closure bound to a job,
    for passing into a script's core run() function."""
    def _callback(progress: float, message: str) -> None:
        tracker.update(job_id, progress=progress, message=message)
    return _callback
