"""CLI entry point for the production health check (disk space + Qdrant
collection health).

Meant to run periodically via cron/systemd timer, inside the app container
so it can reach the Qdrant sidecar by its container-network hostname:

    podman exec zotero-rag python bin/check_production_health.py

Posts an ntfy.sh alert (if NTFY_TOPIC_URL is set) when a check starts or
stops failing; otherwise just logs to stdout. See docs/cron-indexing.md for
setup and the full list of checks.
"""

import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("health_check")

    from backend.config.settings import get_settings
    from backend.dependencies import make_vector_store
    from backend.services.health_check import run_health_check

    settings = get_settings()
    vector_store = make_vector_store()
    problems = run_health_check(settings, vector_store)

    if problems:
        for problem in problems:
            log.error("UNHEALTHY: %s", problem)
        return 1

    log.info("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
