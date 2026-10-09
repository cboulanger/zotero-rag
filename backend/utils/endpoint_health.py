"""Health checks for scale-to-zero remote endpoints.

A preset's embedding/LLM config can name a ``health_check_provider``; the
registry here maps that name to a check function. See
docs/superpowers/specs/2026-10-09-endpoint-health-provisioning-design.md.
"""

import logging
import re
from typing import Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_RUNPOD_ENDPOINT_ID_RE = re.compile(r"/v2/([^/]+)/")
_TIMEOUT_SECONDS = 10.0


def check_runpod_health(
    base_url: str, api_key: str, client: Optional[httpx.Client] = None
) -> dict:
    """Classify a RunPod serverless endpoint as ready, cold, or unreachable.

    Never raises: this is a display-only signal.

    Returns:
        ``{"status": "ready"|"cold"|"unreachable", "detail": str}``.
    """
    match = _RUNPOD_ENDPOINT_ID_RE.search(base_url or "")
    if not match:
        return {"status": "unreachable", "detail": f"Not a RunPod endpoint URL: {base_url!r}"}
    url = f"https://api.runpod.ai/v2/{match.group(1)}/health"
    try:
        http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            response = http.get(
                url, headers={"Authorization": f"Bearer {api_key}"}, timeout=_TIMEOUT_SECONDS
            )
        finally:
            if client is None:
                http.close()
        if not 200 <= response.status_code < 300:
            return {"status": "unreachable", "detail": f"HTTP {response.status_code}"}
        workers = (response.json() or {}).get("workers") or {}
        ready = int(workers.get("ready") or 0)
        running = int(workers.get("running") or 0)
        if ready > 0 or running > 0:
            return {"status": "ready", "detail": f"{ready} ready, {running} running workers"}
        return {"status": "cold", "detail": "No ready or running workers (scaled to zero)"}
    except Exception as exc:  # display-only signal — must never 500 the health route
        return {"status": "unreachable", "detail": str(exc) or type(exc).__name__}


HEALTH_CHECKS: Dict[str, Callable[[str, str], dict]] = {
    "runpod": check_runpod_health,
}
