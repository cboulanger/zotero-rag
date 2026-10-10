"""Persistent record of attachments the backend refuses to (re)process.

An attachment lands here when processing it is known to be futile or harmful:
its OCR cost is out of proportion (``too_costly``), it exceeds the extraction
size cap (``too_large``), or it kept failing in the deferred-upload queue
(``quarantined``). While an attachment is recorded, ``DocumentProcessor`` skips
it, so a single pathological file can neither crash the shared Kreuzberg sidecar
again nor block its library's queue.

Each transition is also appended to the ``IndexEventLog`` (``failed`` /
``unfailed``) so the plugin can mirror it as the Zotero tag
``FAILED_TAG_NAME``. The user retries an attachment by removing that tag; the
plugin reports the removal and ``clear()`` forgets the record. Cross-process
safe (API server, cron subprocess): an OS file lock serializes read-modify-write.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from filelock import FileLock

from backend.services.index_event_log import EVENT_FAILED, EVENT_UNFAILED, IndexEventLog

logger = logging.getLogger(__name__)

REASON_TOO_COSTLY = "too_costly"
REASON_TOO_LARGE = "too_large"
REASON_QUARANTINED = "quarantined"


class FailedAttachmentStore:
    """JSON file ``{library_id: {attachment_key: record}}`` with cross-process locking."""

    def __init__(self, path: Path, event_log: Optional[IndexEventLog] = None) -> None:
        self.path = Path(path)
        self.event_log = event_log
        self._lock = FileLock(str(self.path) + ".lock", timeout=10)

    def _read(self) -> dict[str, dict[str, dict]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp", prefix=self.path.stem + "_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def mark_failed(
        self, library_id: str, attachment_key: str, item_key: str, reason: str, detail: str = ""
    ) -> None:
        """Record a failure (idempotent; a repeat keeps the original ``failed_at``)."""
        with self._lock:
            data = self._read()
            lib = data.setdefault(library_id, {})
            existing = lib.get(attachment_key)
            lib[attachment_key] = {
                "item_key": item_key,
                "reason": reason,
                "detail": detail,
                "failed_at": existing["failed_at"] if existing else datetime.now(timezone.utc).isoformat(),
            }
            self._write(data)
        if existing is None and self.event_log is not None:
            self.event_log.append([{
                "type": EVENT_FAILED, "library_id": library_id,
                "attachment_key": attachment_key, "item_key": item_key, "reason": reason,
            }])

    def clear(self, library_id: str, attachment_key: str) -> Optional[dict]:
        """Forget a failure so the attachment can be processed again.

        Returns the removed record, or None if there was none.
        """
        with self._lock:
            data = self._read()
            record = data.get(library_id, {}).pop(attachment_key, None)
            if record is None:
                return None
            if not data[library_id]:
                del data[library_id]
            self._write(data)
        if self.event_log is not None:
            self.event_log.append([{
                "type": EVENT_UNFAILED, "library_id": library_id,
                "attachment_key": attachment_key, "item_key": record.get("item_key", ""),
            }])
        return record

    def is_failed(self, library_id: str, attachment_key: str) -> bool:
        return attachment_key in self._read().get(library_id, {})

    def list_failed(self, library_id: str) -> list[dict]:
        """Full failure records for a library, each with ``attachment_key`` injected."""
        return [
            {**record, "attachment_key": attachment_key}
            for attachment_key, record in self._read().get(library_id, {}).items()
        ]

    def failed_keys(self, library_id: str, attachment_keys: Optional[Iterable[str]] = None) -> set[str]:
        """Failed attachment keys of a library, optionally restricted to ``attachment_keys``."""
        keys = set(self._read().get(library_id, {}))
        return keys if attachment_keys is None else keys & set(attachment_keys)


def get_failed_store() -> FailedAttachmentStore:
    """Store bound to the process settings (path and event log), like ``get_key_store``."""
    from backend.config.settings import get_settings

    settings = get_settings()
    return FailedAttachmentStore(settings.failed_attachments_path, IndexEventLog(settings.index_events_path))
