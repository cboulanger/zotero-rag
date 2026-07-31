"""Persist the chapter-review queue: items awaiting an admin decision
(bucket="review") or awaiting a batch commit (bucket="commit"), produced
by chapter_segmentation.run()/chapter_upload.run()/chapter_retrofit.run()'s
dry-run paths. See design spec
docs/superpowers/specs/2026-07-30-chapter-review-ui-design.md section 3.

One JSON file, keyed first by library slug then by a deterministic
queue_id, following the same whole-file read/modify/write + FileLock
pattern as AutoIndexKeyStore -- unencrypted, since nothing stored here is
a credential.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

_VALID_STATUSES = {"pending", "approved", "rejected"}
_VALID_BUCKETS = {"review", "commit"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_many(path: Path, slug: str, entries: list[dict]) -> None:
    """Upsert `entries` (each `{"queue_id", "type", "bucket", "payload"}`)
    into the queue for `slug`. An entry whose queue_id already exists with
    a non-"pending" status (already approved/rejected) is left completely
    untouched -- a re-run must never resurrect a decision the admin
    already made. A new or still-pending entry is written/refreshed.
    """
    if not entries:
        return
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        library = data.setdefault(slug, {})
        now = _now()
        for entry in entries:
            queue_id = entry["queue_id"]
            existing = library.get(queue_id)
            if existing is not None and existing["status"] != "pending":
                continue
            library[queue_id] = {
                "type": entry["type"],
                "bucket": entry["bucket"],
                "status": "pending",
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
                "payload": entry["payload"],
            }
        _save(path, data)


def list_pending(path: Path, slug: str, bucket: str | None = None, entry_type: str | None = None) -> list[dict]:
    """Return pending entries for `slug`, each merged with its `queue_id`,
    optionally filtered by `bucket` and/or `entry_type`.
    """
    data = _load(path)
    library = data.get(slug, {})
    results = []
    for queue_id, entry in library.items():
        if entry["status"] != "pending":
            continue
        if bucket is not None and entry["bucket"] != bucket:
            continue
        if entry_type is not None and entry["type"] != entry_type:
            continue
        results.append({**entry, "queue_id": queue_id})
    return results


def get_entry(path: Path, slug: str, queue_id: str) -> dict | None:
    """Return one entry (merged with its `queue_id`), or None if missing."""
    data = _load(path)
    entry = data.get(slug, {}).get(queue_id)
    if entry is None:
        return None
    return {**entry, "queue_id": queue_id}


def set_status(path: Path, slug: str, queue_id: str, status: str) -> None:
    """Set an entry's status ("approved" | "rejected"). Raises KeyError if
    the library/queue_id does not exist."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        entry = data[slug][queue_id]
        entry["status"] = status
        entry["updated_at"] = _now()
        _save(path, data)


def set_bucket(path: Path, slug: str, queue_id: str, bucket: str) -> None:
    """Flip an entry's bucket ("review" | "commit"), keeping its status.
    Raises KeyError if the library/queue_id does not exist."""
    if bucket not in _VALID_BUCKETS:
        raise ValueError(f"invalid bucket: {bucket!r}")
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        entry = data[slug][queue_id]
        entry["bucket"] = bucket
        entry["updated_at"] = _now()
        _save(path, data)


def remove_entry(path: Path, slug: str, queue_id: str) -> None:
    """Delete an entry outright. A no-op if it doesn't exist -- used by the
    OCR-approve flow to clear a stale entry before a fresh analyze run
    re-upserts whatever the current state actually is."""
    with FileLock(str(path) + ".lock"):
        data = _load(path)
        data.get(slug, {}).pop(queue_id, None)
        _save(path, data)
