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
from datetime import datetime, timedelta, timezone
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


def list_entries(data_path: Path, library_id: str, include_quarantined: bool = False) -> list[dict]:
    """Metadata for pending entries in a library, least-attempted first, then oldest-enqueued.

    Ordering by attempts keeps a permanently failing entry from blocking the
    never-tried ones queued behind it. Quarantined entries (see `quarantine_entry`)
    are omitted unless `include_quarantined` is set.

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
        if entry.get("quarantined") and not include_quarantined:
            continue
        entry["attachment_key"] = meta_path.name[: -len(".meta.json")]
        out.append(entry)
    out.sort(key=lambda e: (e.get("attempts", 0), e.get("enqueued_at", "")))
    return out


def record_failure(
    data_path: Path, library_id: str, attachment_key: str, error: str, count_attempt: bool = True
) -> int:
    """Bump `attempts` (unless `count_attempt` is False) and set `last_error`.

    Returns the new attempt count; 0 if the entry is gone. Pass
    `count_attempt=False` for systemic failures (embedding auth/quota) that say
    nothing about this particular file.
    """
    meta_path = _meta_path(data_path, library_id, attachment_key)
    try:
        entry = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if count_attempt:
        entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error"] = error
    _atomic_write_json(meta_path, entry)
    return entry.get("attempts", 0)


def _set_quarantined(data_path: Path, library_id: str, attachment_key: str, quarantined: bool) -> Optional[dict]:
    meta_path = _meta_path(data_path, library_id, attachment_key)
    try:
        entry = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry["quarantined"] = quarantined
    if not quarantined:
        entry["attempts"] = 0
        entry["last_error"] = None
    _atomic_write_json(meta_path, entry)
    return entry


def quarantine_entry(data_path: Path, library_id: str, attachment_key: str) -> Optional[dict]:
    """Stop retrying an entry; it stays on disk. Returns its metadata (None if gone)."""
    return _set_quarantined(data_path, library_id, attachment_key, True)


def release_entry(data_path: Path, library_id: str, attachment_key: str) -> None:
    """Undo `quarantine_entry` and reset attempts, so the next drain retries it."""
    _set_quarantined(data_path, library_id, attachment_key, False)


def note_failure(
    settings, library_id: str, attachment_key: str, item_key: Optional[str], error: str,
    count_attempt: bool = True,
) -> bool:
    """Record a failed attempt; quarantine and flag the attachment once the cap is hit.

    Returns True when the entry was quarantined by this call. The attachment is
    then marked failed (tagged `rag-failed` in Zotero) until the user removes the tag.
    """
    attempts = record_failure(settings.data_path, library_id, attachment_key, error, count_attempt)
    if not attempts or attempts < settings.pending_upload_max_attempts:
        return False
    entry = quarantine_entry(settings.data_path, library_id, attachment_key)
    from backend.services.failed_attachments import REASON_QUARANTINED, get_failed_store

    get_failed_store().mark_failed(
        library_id, attachment_key, item_key or (entry or {}).get("item_key", ""),
        REASON_QUARANTINED, f"Failed {attempts} times: {error}",
    )
    return True


def compute_queue_eta(settings) -> tuple[Optional[str], Optional[str]]:
    """Return (eta_iso8601_or_None, reason). `reason` is None when `eta` is
    present, else "paused" if the built-in scheduler is explicitly paused.

    ETA is the built-in interval scheduler's next tick (last run time, or
    now, plus the configured interval) when `autoindex_interval_minutes` is
    set, else the top of the next UTC hour — matching the documented
    external-cron deployment mode's hourly crontab.
    """
    from backend.services.autoindex_scheduler import read_scheduler_state

    if read_scheduler_state(settings.data_path).get("paused", False):
        return None, "paused"

    now = datetime.now(timezone.utc)
    if settings.autoindex_interval_minutes:
        status_path = settings.data_path / "system" / "cron_status.json"
        last_ts = None
        try:
            cron_status = json.loads(status_path.read_text(encoding="utf-8"))
            last_ts = cron_status.get("finished_at") or cron_status.get("started_at")
        except (OSError, json.JSONDecodeError):
            pass
        last_dt = datetime.fromisoformat(last_ts) if last_ts else now
        # cron_status.json is always written with an aware UTC timestamp today,
        # but coerce defensively so a naive one can never raise a naive/aware
        # TypeError on the comparison below.
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        eta_dt = last_dt + timedelta(minutes=settings.autoindex_interval_minutes)
        # scheduler missed its tick (or this is the first-ever run) — report
        # "imminent" rather than a stale past timestamp.
        if eta_dt < now:
            eta_dt = now
        return eta_dt.isoformat(), None

    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour.isoformat(), None


def library_has_valid_autoindex_target(library_id: str, key_store) -> bool:
    """Whether some stored autoindex key currently targets this library."""
    from backend.api.public_query import backend_id_to_slug

    if not key_store.enabled:
        return False
    slug = backend_id_to_slug(library_id)
    return any(slug in (entry.get("targets") or []) for entry in key_store.list_metadata())


def get_queue_status(settings, library_id: str, key_store) -> dict:
    """Combine `compute_queue_eta` with the per-library key-validity check.

    Priority when `eta` would otherwise be null: "paused" (global) takes
    precedence over "key_invalid" (per-library), since a paused scheduler
    blocks every library regardless of key state.
    """
    eta, reason = compute_queue_eta(settings)
    if reason == "paused":
        return {"eta": None, "reason": "paused"}
    if not library_has_valid_autoindex_target(library_id, key_store):
        return {"eta": None, "reason": "key_invalid"}
    return {"eta": eta, "reason": None}
