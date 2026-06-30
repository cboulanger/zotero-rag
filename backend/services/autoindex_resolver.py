"""Re-validate stored auto-index keys and resolve a deduplicated slug->key map.

Each cron run re-checks every stored key against api.zotero.org. Keys that are
revoked, expired, or downgraded to write scope are pruned from the store and
reported as issues. Surviving keys are merged into a {slug: key} map where each
library appears once, indexed with any currently-valid key that grants it read.
"""

import logging

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import validate_key

logger = logging.getLogger(__name__)


async def resolve_targets(store: AutoIndexKeyStore) -> tuple[dict[str, str], list[dict]]:
    """Return (targets, issues).

    targets: {slug: plaintext_key} deduplicated across all valid keys.
    issues:  list of {fingerprint, user, reason} for pruned keys.
    """
    targets: dict[str, str] = {}
    issues: list[dict] = []

    for fp, api_key, entry in list(store.iter_decrypted()):
        validation = await validate_key(api_key)
        if not validation.read_only:
            store.remove(fp)
            issues.append({
                "fingerprint": fp,
                "user": entry.get("username"),
                "reason": validation.reason or "Key is no longer valid.",
            })
            logger.warning("Pruned auto-index key %s (%s): %s",
                           fp, entry.get("username"), validation.reason)
            continue
        for slug in validation.targets:
            targets.setdefault(slug, api_key)

    return targets, issues
