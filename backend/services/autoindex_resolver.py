"""Re-validate stored auto-index keys and resolve a deduplicated slug->key map.

Each cron run re-checks every stored key against api.zotero.org. Keys that are
permanently invalid (revoked, expired, or downgraded to write scope) are pruned
from the store and reported as issues. Keys whose validation fails for a
transient reason (Zotero API outage, network error, HTTP 429/500/503) are kept
and re-tried on the next run; their previously stored targets are reused so
indexing still attempts them this run. Surviving keys are merged into a
{slug: key} map where each library appears once, indexed with any currently-valid
key that grants it read.
"""

import logging

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import validate_key

logger = logging.getLogger(__name__)


async def resolve_targets(store: AutoIndexKeyStore) -> tuple[dict[str, str], list[dict]]:
    """Return (targets, issues).

    targets: {slug: plaintext_key} deduplicated across all valid keys.
    issues:  list of {fingerprint, user, reason, pruned} for keys that failed
             validation. ``pruned`` is True if the key was removed from the
             store (permanently invalid) and False if it was kept for retry
             (transient failure).
    """
    targets: dict[str, str] = {}
    issues: list[dict] = []

    for fp, api_key, entry in list(store.iter_decrypted()):
        validation = await validate_key(api_key)
        if validation.read_only:
            store.set_status(fp, "ok")
            for slug in validation.targets:
                targets.setdefault(slug, api_key)
        elif validation.transient:
            # Transient failure (outage/network/5xx): keep the key and reuse its
            # previously stored targets so indexing still attempts this run.
            store.set_status(fp, "transient_error")
            for slug in entry.get("targets", []):
                targets.setdefault(slug, api_key)
            issues.append({
                "fingerprint": fp,
                "user": entry.get("username"),
                "reason": validation.reason or "Validation temporarily unavailable; key kept.",
                "pruned": False,
            })
            logger.warning("Kept auto-index key %s (%s) despite transient validation failure: %s",
                           fp, entry.get("username"), validation.reason)
        else:
            store.remove(fp)
            issues.append({
                "fingerprint": fp,
                "user": entry.get("username"),
                "reason": validation.reason or "Key is no longer valid.",
                "pruned": True,
            })
            logger.warning("Pruned auto-index key %s (%s): %s",
                           fp, entry.get("username"), validation.reason)

    return targets, issues
