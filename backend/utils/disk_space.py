"""Shared free-disk-space check, used by both the auto-indexer's pre-flight
guard (bin/index_libraries.py) and the production health check
(backend/services/health_check.py).

Qdrant's segment optimizer needs multi-GB of free space to merge segments;
running a volume down to near-zero free space leaves the optimizer unable to
merge, so unmerged segments pile up and keep the disk full (seen in
production: optimizer error "Not enough space available for optimization" at
~98% used).
"""

import shutil
from pathlib import Path
from typing import Optional


def check_disk_space(path: Path, min_free_percent: float) -> Optional[str]:
    """None if path's volume has enough free space, otherwise a human-readable
    reason describing the shortfall."""
    usage = shutil.disk_usage(path)
    free_percent = (usage.free / usage.total) * 100
    if free_percent < min_free_percent:
        free_gb = usage.free / (1024 ** 3)
        return (
            f"Only {free_percent:.1f}% disk free ({free_gb:.1f} GB) at {path}, "
            f"below the required {min_free_percent:.0f}% minimum."
        )
    return None
