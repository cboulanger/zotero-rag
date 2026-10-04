"""Production health checks (disk space, Qdrant collection health) with
ntfy.sh alerting.

Meant to be run periodically (cron/systemd timer) via
bin/check_production_health.py — see docs/cron-indexing.md. An alert fires
once when a problem starts and once when it clears; it does not resend on
every tick in between, so a sustained incident doesn't spam the topic.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import requests

from backend.db.vector_store import VectorStore
from backend.utils import disk_space

logger = logging.getLogger(__name__)

_NTFY_TIMEOUT_SECONDS = 10


def check_qdrant_collections(vector_store: VectorStore) -> list[str]:
    """Returns one human-readable problem description per unhealthy
    collection (status not green, or optimizer_status not "ok"). Empty list
    means everything checked out. A query failure for a given collection is
    reported as a problem rather than raised, so one unreachable collection
    doesn't stop the others from being checked."""
    problems: list[str] = []
    collections = (
        vector_store.CHUNKS_COLLECTION,
        vector_store.DEDUP_COLLECTION,
        vector_store.METADATA_COLLECTION,
    )
    for name in collections:
        try:
            info = vector_store.client.get_collection(name)
        except Exception as exc:
            problems.append(f"{name}: failed to query collection status ({exc})")
            continue
        status = getattr(info.status, "value", str(info.status))
        if status != "green":
            problems.append(f"{name}: collection status is '{status}'")
        if info.optimizer_status != "ok":
            error = getattr(info.optimizer_status, "error", str(info.optimizer_status))
            problems.append(f"{name}: optimizer error: {error}")
    return problems


def send_ntfy_alert(topic_url: str, title: str, message: str, priority: str = "default") -> None:
    requests.post(
        topic_url,
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority},
        timeout=_NTFY_TIMEOUT_SECONDS,
    )


def _atomic_write_json(path: Path, data: dict) -> None:
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


def _read_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def run_health_check(settings, vector_store: VectorStore, state_path: Optional[Path] = None) -> list[str]:
    """Runs all checks and returns the list of current problems (empty if
    healthy). Sends one ntfy alert on the healthy->unhealthy transition and
    one recovery notice on unhealthy->healthy; silent in between either way."""
    state_path = state_path or (settings.data_path / "system" / "health_check_state.json")

    problems: list[str] = []
    disk_issue = disk_space.check_disk_space(settings.data_path, settings.health_check_min_free_disk_percent)
    if disk_issue:
        problems.append(f"Disk space: {disk_issue}")
    problems.extend(check_qdrant_collections(vector_store))

    state = _read_state(state_path)
    was_unhealthy = state.get("unhealthy", False)
    is_unhealthy = bool(problems)

    if settings.ntfy_topic_url:
        if is_unhealthy and not was_unhealthy:
            send_ntfy_alert(
                settings.ntfy_topic_url,
                title="zotero-rag: production health check FAILED",
                message="\n".join(problems),
                priority="high",
            )
        elif not is_unhealthy and was_unhealthy:
            send_ntfy_alert(
                settings.ntfy_topic_url,
                title="zotero-rag: production health check recovered",
                message="All checks passing again.",
                priority="default",
            )

    _atomic_write_json(state_path, {"unhealthy": is_unhealthy, "problems": problems})
    return problems
