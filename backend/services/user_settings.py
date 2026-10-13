"""Per-user server-side settings (currently the user's preferred preset).

A small JSON file ``<data_path>/system/user_settings.json`` keyed by Zotero user
id (stable across the user rotating their Zotero API key). It holds choices, not
secrets; provider keys live in the encrypted auto-index key store. Writes are
atomic and serialised with a file lock, like the other files under ``system/``.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from filelock import FileLock

logger = logging.getLogger(__name__)

_FILENAME = "user_settings.json"


def _path(data_path: Path) -> Path:
    return Path(data_path) / "system" / _FILENAME


def _read(path: Path) -> dict[str, Any]:
    """The whole file; missing, corrupt or wrongly shaped reads as empty."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) and isinstance(loaded.get("users"), dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
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


def get_preferred_preset(data_path: Path, user_id: int) -> Optional[str]:
    """The preset this user chose, or None (they get the server default)."""
    entry = _read(_path(data_path)).get("users", {}).get(str(user_id), {})
    value = entry.get("preferred_preset") if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value else None


def set_preferred_preset(data_path: Path, user_id: int, preset_name: Optional[str]) -> None:
    """Remember the user's choice; ``None`` clears it."""
    path = _path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        data = _read(path) or {"users": {}}
        entry = data["users"].setdefault(str(user_id), {})
        if preset_name:
            entry["preferred_preset"] = preset_name
        else:
            entry.pop("preferred_preset", None)
            if not entry:
                data["users"].pop(str(user_id), None)
        _write(path, data)
