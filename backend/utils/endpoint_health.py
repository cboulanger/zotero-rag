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
    """Classify a RunPod serverless endpoint's readiness.

    Never raises: this is a display-only signal.

    Returns:
        ``{"status": "ready"|"cold"|"throttled"|"unreachable", "detail": str}``.
        "cold" means scaled to zero (or a worker actively initializing) —
        it will wake up normally on the next request. "throttled" means
        RunPod has no available capacity for this endpoint's GPU type right
        now — jobs sit queued and nothing will progress until capacity
        frees up or the endpoint is reconfigured with a different GPU type.
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
        if response.status_code in (401, 403):
            # An endpoint-restricted RunPod key that doesn't cover this
            # endpoint (e.g. after a --recreate gave it a new ID) gets 403.
            return {
                "status": "unreachable",
                "detail": f"HTTP {response.status_code}: the API key has no access to "
                          f"endpoint {match.group(1)}",
            }
        if not 200 <= response.status_code < 300:
            return {"status": "unreachable", "detail": f"HTTP {response.status_code}"}
        payload = response.json() or {}
        workers = payload.get("workers") or {}
        jobs = payload.get("jobs") or {}
        ready = int(workers.get("ready") or 0)
        running = int(workers.get("running") or 0)
        if ready > 0 or running > 0:
            return {"status": "ready", "detail": f"{ready} ready, {running} running workers"}
        throttled = int(workers.get("throttled") or 0)
        if throttled > 0:
            # RunPod has no available capacity for this endpoint's GPU type
            # right now — distinct from a normal cold start: a throttled
            # worker never reaches "initializing", and queued jobs sit
            # indefinitely rather than progressing. Observed live: a query
            # hung for minutes with no feedback because this was reported
            # as plain "cold" (implying it would wake up normally).
            in_queue = int(jobs.get("inQueue") or 0)
            return {
                "status": "throttled",
                "detail": f"{throttled} worker(s) throttled (no available capacity), "
                          f"{in_queue} job(s) queued — try a different GPU type or wait for capacity",
            }
        initializing = int(workers.get("initializing") or 0)
        if initializing > 0:
            return {"status": "cold", "detail": f"{initializing} worker(s) initializing"}
        return {"status": "cold", "detail": "No ready or running workers (scaled to zero)"}
    except Exception as exc:  # display-only signal — must never 500 the health route
        return {"status": "unreachable", "detail": str(exc) or type(exc).__name__}


HEALTH_CHECKS: Dict[str, Callable[[str, str], dict]] = {
    "runpod": check_runpod_health,
}
