"""Cheap, probe-free lookup of the most recent usage meters.

Shared by ``GET /api/rate-limits`` and ``GET /api/autoindex/status`` so both
resolve cached headers identically. This module never makes a network call.
"""

import json
import logging
from typing import Any, Optional

from backend.services.usage_meters import meters_from_headers, recorder

logger = logging.getLogger(__name__)

SIDES = ("embedding", "llm")


def _cron_usage(settings: Any) -> dict:
    try:
        path = settings.data_path / "system" / "cron_status.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")).get("last_usage") or {}
    except Exception as exc:
        logger.debug("Could not read usage from cron status: %s", exc)
    return {}


def get_cached_rate_limits(settings: Any, fingerprints: Optional[dict[str, Optional[str]]] = None) -> Optional[dict[str, Any]]:
    """Return the freshest cached usage meters for the active preset.

    Per remote side, in order:
      1. In-process record (``source="run"``) for the caller's key fingerprint
         (``fingerprints[side]``; without one, the newest for that side);
         cleared when the active preset is switched.
      2. ``cron_status.json["last_usage"][side]`` (``source="cache"``), written
         by the cron indexer; ignored when produced under a different preset.

    Returns:
        ``{"meters": list[dict], "as_of": str | None, "source": "run" | "cache"}``
        (``as_of``/``source`` of the newest meter), or ``None`` when no remote
        side has any usage information.
    """
    try:
        preset = settings.get_hardware_preset()
    except Exception as exc:
        logger.debug("Could not resolve active preset for rate limits: %s", exc)
        return None

    fingerprints = fingerprints or {}
    cron = None
    meters: list[dict] = []
    for side in SIDES:
        if getattr(preset, side).model_type != "remote":
            continue
        headers, at = recorder.latest(side, fingerprints.get(side))
        source = "run"
        if not headers:
            cron = cron if cron is not None else _cron_usage(settings)
            entry = cron.get(side) or {}
            if entry.get("preset") not in (None, preset.name):
                continue
            headers, at, source = entry.get("headers"), entry.get("at"), "cache"
        meters += meters_from_headers(preset, side, headers, as_of=at, source=source)
    if not meters:
        return None
    newest = max(meters, key=lambda m: m.get("as_of") or "")
    return {"meters": meters, "as_of": newest.get("as_of"), "source": newest.get("source")}
