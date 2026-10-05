"""CPU affinity restriction for the indexing process, so a configurable
number of CPUs stay free to serve RAG queries while an indexing run is busy.

Root-caused in the 2026-10 Qdrant-timeout incident: a concurrent indexing run
and a live /api/query search can each need CPU/IO time from Qdrant and the
backend at the same moment; capping how many cores indexing may use leaves
the rest free for query traffic instead of letting indexing starve it.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def compute_restricted_cpu_set(available_cpus: set[int], reserved_cpus: int) -> Optional[set[int]]:
    """Returns the CPU subset indexing should be restricted to, or None if no
    restriction should be applied. Indexing is always left at least one CPU,
    even if reserved_cpus would otherwise consume all of them."""
    if reserved_cpus <= 0:
        return None
    total = len(available_cpus)
    usable = max(1, total - reserved_cpus)
    if usable >= total:
        return None
    return set(sorted(available_cpus)[:usable])


def restrict_current_process_cpus(reserved_cpus: int) -> None:
    """Restricts the CURRENT process's CPU affinity so reserved_cpus stay
    free for other processes (the backend answering RAG queries). Child
    processes/threads inherit this restriction. No-op on platforms without
    os.sched_getaffinity/sched_setaffinity (e.g. macOS dev)."""
    getaffinity = getattr(os, "sched_getaffinity", None)
    setaffinity = getattr(os, "sched_setaffinity", None)
    if getaffinity is None or setaffinity is None:
        logger.debug("CPU affinity restriction unsupported on this platform; skipping.")
        return

    available = getaffinity(0)
    restricted = compute_restricted_cpu_set(available, reserved_cpus)
    if restricted is None:
        logger.debug(
            "No CPU affinity restriction applied (reserved_cpus=%s, available=%s).",
            reserved_cpus, len(available),
        )
        return

    setaffinity(0, restricted)
    logger.info(
        "Restricted indexing to CPUs %s (reserved %s of %s for serving queries).",
        sorted(restricted), reserved_cpus, len(available),
    )
