"""Re-validate stored auto-index keys and resolve a deduplicated slug->target map.

Each cron run re-checks every stored key against api.zotero.org. Keys that are
permanently invalid (revoked, expired, or downgraded to write scope) are pruned
from the store and reported as issues. Keys whose validation fails for a
transient reason (Zotero API outage, network error, HTTP 429/500/503) are kept
and re-tried on the next run; their previously stored targets are reused so
indexing still attempts them this run.

A slug is only included in the returned targets if its owning entry also has a
usable embedding API key (status "ok" or "unverified", and not currently inside
its own rate-limit window) — auto-indexing never falls back to a server-wide
embedding key, so a slug whose owner has no valid embedding key is skipped and
reported as an issue instead.
"""

import logging
from datetime import datetime, timezone

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import validate_key

logger = logging.getLogger(__name__)


async def resolve_targets(store: AutoIndexKeyStore) -> tuple[dict[str, dict], list[dict]]:
    """Return (targets, issues).

    targets: {slug: {"zotero_key", "embedding_key", "embedding_key_name", "fingerprint"}}
             deduplicated across all valid keys.
    issues:  list of {fingerprint, user, reason, pruned, kind} — "kind" is
             "zotero_key" for Zotero-key problems (unchanged from before) or
             "embedding_key" for a missing/invalid/rate-limited embedding key.
    """
    targets: dict[str, dict] = {}
    issues: list[dict] = []

    for fp, api_key, entry in list(store.iter_decrypted()):
        validation = await validate_key(api_key)
        if validation.read_only:
            store.set_status(fp, "ok")
            slugs = validation.targets
        elif validation.transient:
            # Transient failure (outage/network/5xx): keep the key and reuse its
            # previously stored targets so indexing still attempts this run.
            store.set_status(fp, "transient_error")
            slugs = entry.get("targets", [])
            issues.append({
                "fingerprint": fp,
                "user": entry.get("username"),
                "reason": validation.reason or "Validation temporarily unavailable; key kept.",
                "pruned": False,
                "kind": "zotero_key",
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
                "kind": "zotero_key",
            })
            logger.warning("Pruned auto-index key %s (%s): %s",
                           fp, entry.get("username"), validation.reason)
            continue

        embedding_info = store.get_decrypted_embedding_key(fp)
        embedding_status = entry.get("embedding_key_status")
        rate_limit_until_str = entry.get("embedding_key_rate_limit_until")
        still_rate_limited = False
        if rate_limit_until_str:
            try:
                rate_limit_until = datetime.fromisoformat(rate_limit_until_str)
                if rate_limit_until.tzinfo is None:
                    rate_limit_until = rate_limit_until.replace(tzinfo=timezone.utc)
                still_rate_limited = rate_limit_until > datetime.now(timezone.utc)
            except ValueError:
                still_rate_limited = False

        blocked = (
            not embedding_info
            or embedding_status == "invalid"
            or (embedding_status == "rate_limited" and still_rate_limited)
        )
        if blocked:
            if not embedding_info:
                reason = "No embedding API key configured; auto-indexing skipped."
            elif embedding_status == "invalid":
                reason = "Embedding API key was rejected; auto-indexing skipped."
            else:
                reason = f"Embedding API key is rate-limited until {rate_limit_until_str}; auto-indexing skipped."
            issues.append({
                "fingerprint": fp,
                "user": entry.get("username"),
                "reason": reason,
                "pruned": False,
                "kind": "embedding_key",
            })
            continue

        embedding_key_name, embedding_key = embedding_info
        for slug in slugs:
            targets.setdefault(slug, {
                "zotero_key": api_key,
                "embedding_key": embedding_key,
                "embedding_key_name": embedding_key_name,
                "fingerprint": fp,
            })

    return targets, issues
