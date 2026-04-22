"""
Library/user registration service.

Stores a mapping of library IDs to the users who have registered to index them.
Data is persisted as a JSON file under <data_path>/system/registrations.json.

Concurrent writes are protected by a filelock so that multi-process uvicorn
deployments (UVICORN_WORKERS > 1) cannot corrupt the file or silently drop
registrations.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)


class RegistrationService:
    """Read/write library-user registration data from a JSON file."""

    def __init__(self, registrations_path: Path) -> None:
        self._path = registrations_path
        # Lock file sits next to the data file; acquired for every write.
        self._lock = FileLock(str(registrations_path) + ".lock")

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        text = self._path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception as e:
            logger.error(f"Failed to parse registrations file: {e}")
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def register(self, library_id: str, library_name: str, user_id: int, username: str) -> bool:
        """Register a user for a library.

        Returns True if the library entry already existed before this call,
        False if it was created fresh. Safe to call concurrently from multiple
        processes.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            data = self._load()
            existed = library_id in data
            if not existed:
                data[library_id] = {
                    "library_id": library_id,
                    "library_name": library_name,
                    "registered_at": now,
                    "users": [],
                }
            entry = data[library_id]
            entry["library_name"] = library_name
            if not any(u["user_id"] == user_id for u in entry["users"]):
                entry["users"].append({
                    "user_id": user_id,
                    "username": username,
                    "registered_at": now,
                })
                logger.info(f"Registered user {username!r} ({user_id}) for library {library_id!r}")
            self._save(data)
        return existed

    def is_registered(self, library_id: str, user_id: int | None) -> bool:
        """Return True if user_id is in the registered users list for library_id."""
        if user_id is None:
            return False
        data = self._load()
        entry = data.get(library_id)
        if entry is None:
            return False
        return any(u["user_id"] == user_id for u in entry.get("users", []))

    def get_all(self) -> dict:
        """Return the full registrations dict."""
        return self._load()
