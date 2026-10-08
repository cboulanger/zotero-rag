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
reported as an issue instead. This per-user gating only applies when the active
preset's embedding config declares a *personal* key (``api_key_env``, e.g.
KISSKI, one key per user). It does not apply to a local model (no API key at
all) or to a preset with a *shared*, admin-set key (``shared_api_key_env``,
e.g. remote-mpcdf) — there, every caller uses the same server-wide credential,
so there is no per-user key to validate, and a status left over from a
previously-active personal-key preset must not be reused to gate an unrelated
shared-key preset.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from backend.config.settings import get_settings
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import validate_key

logger = logging.getLogger(__name__)


def is_embedding_key_usable(status: Optional[str], rate_limit_until_str: Optional[str]) -> bool:
    """Fail closed: only explicitly recognized "good" statuses let a key through.

    An unrecognized/typo'd/future status string is treated as blocked rather
    than silently permitted. A "rate_limited" status becomes usable again once
    its recorded window has passed.
    """
    if status in ("ok", "unverified"):
        return True
    if status != "rate_limited":
        return False
    if not rate_limit_until_str:
        return False
    try:
        rate_limit_until = datetime.fromisoformat(rate_limit_until_str)
        if rate_limit_until.tzinfo is None:
            rate_limit_until = rate_limit_until.replace(tzinfo=timezone.utc)
        return rate_limit_until <= datetime.now(timezone.utc)
    except ValueError:
        logger.warning("Invalid embedding_key_rate_limit_until: %r", rate_limit_until_str)
        return False


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

    # Per-user embedding keys only make sense for a remote provider that uses a
    # *personal* key (api_key_env, e.g. KISSKI — one key per user, so per-user
    # billing/quota and per-user rate-limit tracking are meaningful). A local
    # model has no API key at all, and a remote preset with a *shared*,
    # admin-set key (shared_api_key_env, e.g. remote-mpcdf) has no per-user key
    # either — every caller uses the same server-wide credential, resolved
    # later via admin_settings_store, independent of any individual user's
    # stored status. Gating on a personal-key status in either of those cases
    # would make auto-indexing a permanent no-op (no key configured) or
    # incorrectly block it on a stale status left over from a previously
    # active personal-key preset (the actual bug this guards against).
    embedding_config = get_settings().get_hardware_preset().embedding
    requires_embedding_key = (
        embedding_config.model_type == "remote"
        and "api_key_env" in embedding_config.model_kwargs
    )

    for fp, api_key, entry in list(store.iter_decrypted()):
        validation = await validate_key(api_key)
        if validation.read_only:
            store.set_status(
                fp, "ok", targets=validation.targets,
                target_names=validation.target_names,
                target_owners=validation.target_owners,
            )
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

        if not requires_embedding_key:
            for slug in slugs:
                targets.setdefault(slug, {
                    "zotero_key": api_key,
                    "embedding_key": None,
                    "embedding_key_name": None,
                    "fingerprint": fp,
                })
            continue

        embedding_info = store.get_decrypted_embedding_key(fp)
        embedding_status = entry.get("embedding_key_status")
        rate_limit_until_str = entry.get("embedding_key_rate_limit_until")
        usable = is_embedding_key_usable(embedding_status, rate_limit_until_str)
        if not embedding_info or not usable:
            if not embedding_info:
                reason = "No embedding API key configured; auto-indexing skipped."
            elif embedding_status == "invalid":
                reason = "Embedding API key was rejected; auto-indexing skipped."
            elif embedding_status == "rate_limited":
                reason = f"Embedding API key is rate-limited until {rate_limit_until_str}; auto-indexing skipped."
            else:
                reason = f"Embedding API key has unrecognized status {embedding_status!r}; auto-indexing skipped."
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
