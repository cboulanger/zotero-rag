"""On-disk cache of attachment uploads deferred for later indexing.

A plain-files store, following the same pattern as
``backend/utils/migration_state.py`` and ``backend/services/autoindex_key_store.py``
(atomic JSON writes via ``os.replace``) — no database. Keyed by
``(library_id, attachment_key)``: at most one pending entry per attachment,
a re-upload overwrites the previous one in place.

Drained by CronIndexer._drain_pending_uploads (backend/services/cron_indexer.py)
on every autoindex run for that library, and by the on-demand "process now"
endpoint (POST /api/index/document/cache/{library_id}/{attachment_key}/process-now).
See docs/superpowers/specs/2026-10-06-deferred-server-side-indexing-design.md.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _library_dir(data_path: Path, library_id: str) -> Path:
    return data_path / "system" / "pending_uploads" / library_id


def _bin_path(data_path: Path, library_id: str, attachment_key: str) -> Path:
    return _library_dir(data_path, library_id) / f"{attachment_key}.bin"


def _meta_path(data_path: Path, library_id: str, attachment_key: str) -> Path:
    return _library_dir(data_path, library_id) / f"{attachment_key}.meta.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_bin_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_meta_")
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


def write_entry(data_path: Path, library_id: str, attachment_key: str, file_bytes: bytes, metadata: dict) -> None:
    """Write (or overwrite) a pending entry.

    `metadata` should carry the same fields the upload endpoints already
    accept (item_key, mime_type, item_version, attachment_version,
    item_modified/zotero_modified, title, authors, year, item_type,
    library_type, library_name). Any `enqueued_at`/`attempts`/`last_error`
    already in `metadata` are ignored — this always resets them.
    """
    entry = {
        **metadata,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
        "last_error": None,
    }
    _atomic_write_bytes(_bin_path(data_path, library_id, attachment_key), file_bytes)
    _atomic_write_json(_meta_path(data_path, library_id, attachment_key), entry)


def read_entry(data_path: Path, library_id: str, attachment_key: str) -> Optional[tuple[bytes, dict]]:
    """Return (file_bytes, metadata) for a pending entry, or None if absent."""
    bin_path = _bin_path(data_path, library_id, attachment_key)
    meta_path = _meta_path(data_path, library_id, attachment_key)
    if not bin_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return bin_path.read_bytes(), meta


def has_entry(data_path: Path, library_id: str, attachment_key: str) -> bool:
    return _meta_path(data_path, library_id, attachment_key).exists()


def delete_entry(data_path: Path, library_id: str, attachment_key: str) -> None:
    for path in (_bin_path(data_path, library_id, attachment_key), _meta_path(data_path, library_id, attachment_key)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def list_entries(data_path: Path, library_id: str) -> list[dict]:
    """Metadata for every pending entry in a library, oldest-enqueued first.

    Each dict has an injected `attachment_key` (derived from the filename);
    file bytes are not read (use `read_entry` for that).
    """
    lib_dir = _library_dir(data_path, library_id)
    if not lib_dir.exists():
        return []
    out: list[dict] = []
    for meta_path in lib_dir.glob("*.meta.json"):
        try:
            entry = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry["attachment_key"] = meta_path.name[: -len(".meta.json")]
        out.append(entry)
    out.sort(key=lambda e: e.get("enqueued_at", ""))
    return out


def record_failure(data_path: Path, library_id: str, attachment_key: str, error: str) -> None:
    """Bump `attempts` and set `last_error` on an existing entry. No-op if it's gone."""
    meta_path = _meta_path(data_path, library_id, attachment_key)
    try:
        entry = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = error
    _atomic_write_json(meta_path, entry)
