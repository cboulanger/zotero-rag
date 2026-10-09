"""Log file rotation: on startup and daily at midnight, with age-based pruning.

Built on the stdlib ``TimedRotatingFileHandler``. Rotated files are named
``<log>.<YYYY-MM-DD_HHMMSS>`` and removed once older than ``retention_days``.
"""

import logging
import os
import re
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import List, Union

_SUFFIX_FORMAT = "%Y-%m-%d_%H%M%S"
_SUFFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")


class RotatingLogHandler(TimedRotatingFileHandler):
    """Daily-rotating file handler that can also rotate once at startup.

    Rotation timestamps are the actual rotation time (not the period start), so
    several restarts on the same day never collide. Pruning is by file age
    rather than file count, since restarts make the count unpredictable.
    """

    def __init__(
        self,
        filename: Union[str, Path],
        retention_days: int = 7,
        rotate_on_startup: bool = True,
        encoding: str = "utf-8",
    ) -> None:
        # backupCount must be > 0 or the base class never calls getFilesToDelete()
        super().__init__(
            str(filename), when="midnight", backupCount=1, encoding=encoding
        )
        self.retention_seconds = retention_days * 86400
        self.suffix = _SUFFIX_FORMAT
        self.extMatch = _SUFFIX_RE
        self.namer = self._timestamp_namer
        if rotate_on_startup and os.path.exists(self.baseFilename) \
                and os.path.getsize(self.baseFilename) > 0:
            self.doRollover()
        else:
            self._prune()

    @staticmethod
    def _timestamp_namer(default_name: str) -> str:
        base = default_name.rsplit(".", 1)[0]
        return f"{base}.{time.strftime(_SUFFIX_FORMAT)}"

    def getFilesToDelete(self) -> List[str]:
        """Rotated files older than the retention window."""
        directory, base = os.path.split(self.baseFilename)
        prefix = base + "."
        cutoff = time.time() - self.retention_seconds
        stale = []
        for name in os.listdir(directory or "."):
            if name.startswith(prefix) and _SUFFIX_RE.match(name[len(prefix):]):
                path = os.path.join(directory, name)
                if os.path.getmtime(path) < cutoff:
                    stale.append(path)
        return stale

    def _prune(self) -> None:
        for path in self.getFilesToDelete():
            try:
                os.remove(path)
            except OSError as e:
                logging.getLogger(__name__).warning("Could not remove old log %s: %s", path, e)
