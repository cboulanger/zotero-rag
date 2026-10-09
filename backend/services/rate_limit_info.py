"""Cheap, probe-free lookup of the most recent embedding rate-limit headers.

Shared by ``GET /api/rate-limits`` and ``GET /api/autoindex/status`` so both
resolve cached headers identically. This module never makes a network call.
"""

import json
import logging
from typing import Any, Optional

from backend.services.embeddings import get_last_rate_limit_snapshot

logger = logging.getLogger(__name__)


def get_cached_rate_limits(settings: Any) -> Optional[dict[str, Any]]:
    """Return the freshest cached rate-limit headers for the active preset.

    Resolution order:
      1. In-process cache (``source="run"``): populated by embedding calls made
         inside this process; cleared when the active preset is switched.
      2. ``cron_status.json["last_rate_limit_headers"]`` (``source="cache"``):
         written by the cron indexer. Ignored when it was produced under a
         different preset than the active one.

    Args:
        settings: Application settings (``data_path``, ``get_hardware_preset()``).

    Returns:
        ``{"limits": dict, "as_of": str | None, "source": "run" | "cache"}`` or
        ``None`` when nothing is cached or the active embedding is not remote.
    """
    try:
        preset = settings.get_hardware_preset()
    except Exception as exc:
        logger.debug("Could not resolve active preset for rate limits: %s", exc)
        return None
    if preset.embedding.model_type != "remote":
        return None

    headers, captured_at = get_last_rate_limit_snapshot()
    if headers:
        return {"limits": headers, "as_of": captured_at, "source": "run"}

    try:
        path = settings.data_path / "system" / "cron_status.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            limits = data.get("last_rate_limit_headers") or None
            if limits:
                produced_by = data.get("last_rate_limit_preset")
                if produced_by and produced_by != preset.name:
                    return None
                return {
                    "limits": limits,
                    "as_of": data.get("last_rate_limit_headers_at"),
                    "source": "cache",
                }
    except Exception as exc:
        logger.debug("Could not read rate-limit headers from cron status: %s", exc)
    return None
