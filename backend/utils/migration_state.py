"""Resume-from-cursor state tracking for bin/migrate_library.py.

Pure stdlib, no Qdrant/VectorStore dependency, so it's testable in
isolation. See
docs/superpowers/specs/2026-10-05-library-rag-migration-resume-design.md.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

MIGRATION_COLLECTIONS = ("chunks", "dedup")


def state_path(slug: str, source_url: str, dest_url: str, data_path: Path) -> Path:
    """Return the state file path for this (slug, source_url, dest_url) key."""
    key = hashlib.sha1(f"{slug}|{source_url}|{dest_url}".encode()).hexdigest()
    return data_path / "system" / "migration_state" / f"{key}.json"


def load_state(path: Path) -> Optional[dict]:
    """Return the parsed state dict, or None if missing or unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] Ignoring unreadable migration state file {path}: {exc}", file=sys.stderr)
        return None


def save_state(path: Path, state: dict) -> None:
    """Atomically write state to path (write to a .tmp sibling, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, path)


def delete_state(path: Path) -> None:
    """Remove the state file if present; no-op if it doesn't exist."""
    path.unlink(missing_ok=True)


def new_state(
    slug: str,
    source_url: str,
    dest_url: str,
    library_id: str,
    embedding_model_name: str,
    embedding_dim: int,
    started_at: str,
) -> dict:
    """Build a fresh state dict for a new migration run."""
    return {
        "slug": slug,
        "source_url": source_url,
        "dest_url": dest_url,
        "library_id": library_id,
        "embedding_model_name": embedding_model_name,
        "embedding_dim": embedding_dim,
        "begin_done": False,
        "collections": {
            collection: {"cursor": None, "transferred": 0, "done": False}
            for collection in MIGRATION_COLLECTIONS
        },
        "metadata_done": False,
        "started_at": started_at,
        "updated_at": started_at,
    }
