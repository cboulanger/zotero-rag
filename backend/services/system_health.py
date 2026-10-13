"""On-demand system health snapshot for the admin auto-indexing status panel.

Surfaces host CPU/memory/swap and sidecar (Kreuzberg/Qdrant) reachability +
latency, so an admin can tell "is this stuck or just slow" straight from the
status dialog, without needing to SSH into the server and run `podman
stats`/`free -h` every time something looks wedged.
"""

import shutil
import time
from typing import Optional

import httpx
import psutil

from backend.config.settings import Settings

_HTTP_TIMEOUT_SECONDS = 3.0


async def _check_sidecar(client: httpx.AsyncClient, url: Optional[str]) -> dict:
    if not url:
        return {"status": "local-mode"}
    t0 = time.monotonic()
    try:
        resp = await client.get(url)
        return {
            "status": "ok" if resp.is_success else f"http_{resp.status_code}",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }
    except httpx.TimeoutException:
        return {"status": "timeout", "latency_ms": round((time.monotonic() - t0) * 1000)}
    except httpx.ConnectError:
        return {"status": "unreachable"}
    except Exception as exc:  # best-effort panel — one bad sub-check must not blank the rest
        return {"status": "error", "error": str(exc)}


async def get_system_health(settings: Settings) -> dict:
    """Best-effort snapshot; never raises.

    cpu_percent uses psutil's non-blocking mode (compares against the
    previous call rather than sampling over a blocking interval) — since
    this is polled on the dialog's own ~5s refresh cadence, that yields a
    meaningful "CPU over the last poll" reading without ever blocking the
    event loop. The very first call after backend startup reads 0.0 and
    self-corrects on the next poll.

    Kreuzberg's /health endpoint reports static capability info, not a
    queue depth or busy flag, so response latency is the best available
    proxy for "busy" without requiring host/podman-level access the
    backend doesn't have from inside its own container.
    """
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_percent = psutil.cpu_percent(interval=None)

    disk: Optional[dict] = None
    try:
        usage = shutil.disk_usage(settings.data_path)
        disk = {
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "free_percent": round((usage.free / usage.total) * 100, 1),
        }
    except OSError:
        pass

    result: dict = {
        "cpu_percent": round(cpu_percent, 1),
        "memory": {
            "used_gb": round((vm.total - vm.available) / (1024 ** 3), 1),
            "total_gb": round(vm.total / (1024 ** 3), 1),
            "percent": vm.percent,
        },
        "swap": {
            "used_gb": round(swap.used / (1024 ** 3), 1),
            "total_gb": round(swap.total / (1024 ** 3), 1),
            "percent": swap.percent,
        },
        "disk": disk,
    }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        result["sidecars"] = {
            "kreuzberg": await _check_sidecar(
                client, f"{settings.kreuzberg_url.rstrip('/')}/health" if settings.kreuzberg_url else None
            ),
            "qdrant": await _check_sidecar(client, settings.qdrant_url),
        }

    return result
