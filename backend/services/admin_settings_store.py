"""Persisted store for global, admin-controlled runtime settings.

Mirrors backend.services.autoindex_scheduler's state-file pattern: a small
JSON file under data_path/system/, atomically written, with a safe default
when the file doesn't exist yet. Unlike backend.config.settings.Settings
(env-var-backed, fixed at process start), this store is meant to be toggled
live by a server admin without a restart or redeploy.
"""

import json
import os
import tempfile
from pathlib import Path

DEFAULT_ADMIN_SETTINGS = {"index_snapshots": False}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a small JSON state file (Windows-safe via os.replace).

    Mirrors CronIndexer._write_status's pattern (backend/services/cron_indexer.py)
    and autoindex_scheduler._atomic_write_json.
    """
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


def read_admin_settings(data_path: Path) -> dict:
    """Missing, corrupt, or non-dict-shaped file reads as the safe default:
    {"index_snapshots": False}."""
    settings_path = data_path / "system" / "admin_settings.json"
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_ADMIN_SETTINGS)
    if not isinstance(loaded, dict):
        return dict(DEFAULT_ADMIN_SETTINGS)
    merged = dict(DEFAULT_ADMIN_SETTINGS)
    merged.update(loaded)
    return merged


def write_admin_settings(data_path: Path, settings: dict) -> None:
    """Replaces the entire settings file — callers must pass the complete
    current settings dict, not a partial update (same full-replace contract
    as autoindex_scheduler.write_scheduler_state)."""
    _atomic_write_json(data_path / "system" / "admin_settings.json", settings)
