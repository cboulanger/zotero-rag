"""Append-only log of per-attachment "indexed / un-indexed" transitions.

The backend has no push channel to the plugin (the old pull-based indexing SSE
endpoints were removed; the plugin already polls ``/api/autoindex/status`` for
cron progress), so real-time indexed-status tags are fed by a cheap polled log:
``VectorStore`` appends an event whenever an attachment gains its first chunks
or loses them, and the plugin reads new events with ``GET /api/indexed-tags/events``.

The log is a JSON-lines file because chunks are written by more than one
process (the API server for plugin uploads, the ``bin/index_libraries.py``
subprocess for cron runs); an OS file lock serializes appends across them.
Sequence numbers are monotonic across rotation, so a reader can tell when it
fell behind the retained window (``gap``) and fall back to a full Refresh.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from filelock import FileLock

logger = logging.getLogger(__name__)

# Tag the plugin attaches to indexed attachments. Defined here (not in the
# plugin) so the backend, the sync script and the plugin cannot drift apart:
# the plugin receives it with every events/refresh response.
INDEXED_TAG_NAME = "\u2705 rag-indexed"

EVENT_INDEXED = "indexed"
EVENT_UNINDEXED = "unindexed"
EVENT_LIBRARY_UNINDEXED = "library_unindexed"

DEFAULT_MAX_EVENTS = 5000


class IndexEventLog:
    """JSON-lines event log with cross-process locking and size-bounded rotation."""

    def __init__(self, path: Path, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self.path = Path(path)
        self.max_events = max_events
        self._lock = FileLock(str(self.path) + ".lock", timeout=10)

    def _read_all(self) -> list[dict]:
        events: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        # A torn trailing line from a crashed writer must not
                        # poison every later read.
                        continue
        except FileNotFoundError:
            pass
        return events

    def append(self, events: Iterable[dict]) -> int:
        """Append events (each a dict without ``seq``/``ts``). Returns the last seq.

        Never raises: tagging is a display enhancement and must not be able to
        fail an indexing write. Failures are logged and swallowed.
        """
        batch = list(events)
        if not batch:
            return self.last_seq()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                existing = self._read_all()
                seq = existing[-1]["seq"] if existing else 0
                now = datetime.now(timezone.utc).isoformat()
                for event in batch:
                    seq += 1
                    existing.append({"seq": seq, "ts": now, **event})
                if len(existing) > self.max_events:
                    existing = existing[-self.max_events:]
                    self._rewrite(existing)
                else:
                    with open(self.path, "a", encoding="utf-8") as f:
                        for event in existing[-len(batch):]:
                            f.write(json.dumps(event) + "\n")
                return seq
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("Failed to append to index event log %s: %s", self.path, exc)
            return 0

    def _rewrite(self, events: list[dict]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp", prefix=self.path.stem + "_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def last_seq(self) -> int:
        """Highest sequence number written so far (0 if the log is empty)."""
        try:
            with self._lock:
                events = self._read_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read index event log %s: %s", self.path, exc)
            return 0
        return events[-1]["seq"] if events else 0

    def read_since(self, since: int, limit: int = 1000) -> dict:
        """Return events with ``seq > since``.

        ``gap`` is True when events between ``since`` and the oldest retained
        event were rotated away, i.e. the reader missed some and should run a
        full Refresh. ``last_seq`` is the log head (for a reader with nothing
        new to advance its cursor).
        """
        with self._lock:
            events = self._read_all()
        last_seq = events[-1]["seq"] if events else 0
        oldest: Optional[int] = events[0]["seq"] if events else None
        gap = oldest is not None and since < oldest - 1
        fresh = [e for e in events if e["seq"] > since][:limit]
        return {"events": fresh, "last_seq": last_seq, "gap": gap}
