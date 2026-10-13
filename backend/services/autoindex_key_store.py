"""Encrypted persistence for auto-index Zotero keys.

Keys are stored Fernet-encrypted in a JSON file keyed by a non-secret
fingerprint (sha256(key)[:12]). User id, username, and resolved targets are
stored in plaintext for display; the key value never is. A filelock guards
concurrent writes (multi-worker uvicorn), mirroring RegistrationService.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock

from backend.zotero.key_validator import KeyValidation

logger = logging.getLogger(__name__)


def fingerprint(api_key: str) -> str:
    """Non-secret stable identifier for a key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


class AutoIndexKeyStore:
    """Read/write Fernet-encrypted auto-index keys."""

    def __init__(self, path: Path, secret: Optional[str]) -> None:
        self._path = Path(path)
        self._lock = FileLock(str(path) + ".lock")
        self._fernet = Fernet(secret.encode()) if secret else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def _require_enabled(self) -> None:
        if not self._fernet:
            raise RuntimeError("AUTOINDEX_SECRET is not configured; key store is disabled.")

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        text = self._path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception as e:
            logger.error("Failed to parse autoindex keys file: %s", e)
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._path.chmod(0o600)

    def add(self, api_key: str, validation: KeyValidation) -> str:
        """Encrypt and store a validated key. Returns its fingerprint.

        Re-adding an already-registered fingerprint (e.g. re-submitting the
        same Zotero key to refresh its validation) must not wipe an
        unrelated embedding key already stored on that entry — only the
        Zotero-key fields are refreshed here; the ``embedding_keys`` already
        present are carried over unchanged.

        A user may only have one registered Zotero key at a time: if this
        api_key rotates them onto a new fingerprint (a different key value —
        e.g. after regenerating their Zotero API key), any other entry
        already registered under the same user_id is removed, after carrying
        its ``embedding_keys`` forward — the embedding keys aren't tied to
        the Zotero key and shouldn't be lost just because the Zotero key changed.
        """
        self._require_enabled()
        fp = fingerprint(api_key)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            data = self._load()
            existing = data.get(fp, {})
            for other_fp, other_entry in list(data.items()):
                if other_fp == fp or other_entry.get("user_id") != validation.user_id:
                    continue
                carried = {**other_entry.get("embedding_keys", {}), **existing.get("embedding_keys", {})}
                if carried:
                    existing["embedding_keys"] = carried
                del data[other_fp]
            entry = {
                "ciphertext": self._fernet.encrypt(api_key.encode()).decode(),
                "user_id": validation.user_id,
                "username": validation.username,
                "targets": list(validation.targets),
                "target_names": dict(validation.target_names),
                "target_owners": dict(validation.target_owners),
                "validated_at": now,
                "last_status": "ok",
            }
            if existing.get("embedding_keys"):
                entry["embedding_keys"] = existing["embedding_keys"]
            data[fp] = entry
            self._save(data)
        return fp

    def get_decrypted(self, fp: str) -> Optional[str]:
        self._require_enabled()
        entry = self._load().get(fp)
        if not entry:
            return None
        try:
            return self._fernet.decrypt(entry["ciphertext"].encode()).decode()
        except InvalidToken:
            logger.error("Could not decrypt key %s (wrong AUTOINDEX_SECRET?)", fp)
            return None

    def remove(self, fp: str) -> bool:
        with self._lock:
            data = self._load()
            existed = fp in data
            if existed:
                data.pop(fp, None)
                self._save(data)
        return existed

    def remove_by_key(self, api_key: str) -> bool:
        return self.remove(fingerprint(api_key))

    def list_metadata(self, key_name: Optional[str] = None) -> list[dict]:
        """Return entry metadata without ciphertext or plaintext.

        ``embedding_keys`` lists every stored provider key by name with its
        status. The flat ``has_embedding_key`` / ``embedding_key_status`` /
        ``embedding_key_rate_limit_until`` fields describe the key called
        ``key_name`` (the active preset's personal key), or, with no name, the
        entry's only key.
        """
        out = []
        for fp, entry in self._load().items():
            keys = entry.get("embedding_keys", {})
            chosen = keys.get(key_name) if key_name else (next(iter(keys.values())) if len(keys) == 1 else None)
            out.append({
                "fingerprint": fp,
                "user_id": entry.get("user_id"),
                "username": entry.get("username"),
                "targets": entry.get("targets", []),
                "last_status": entry.get("last_status"),
                "validated_at": entry.get("validated_at"),
                "embedding_keys": {
                    name: {"status": k.get("status"), "rate_limit_until": k.get("rate_limit_until")}
                    for name, k in keys.items()
                },
                "has_embedding_key": bool(chosen and chosen.get("ciphertext")),
                "embedding_key_status": chosen.get("status") if chosen else None,
                "embedding_key_rate_limit_until": chosen.get("rate_limit_until") if chosen else None,
            })
        return out

    def get_target_labels(self) -> dict[str, tuple[str, Optional[int]]]:
        """Map each auto-indexed slug to (name, owner_id) captured during key
        validation (see KeyValidation.target_names/target_owners).

        Used as a fallback name source for the status endpoint's
        library_name/owner_id join, for slugs that were only ever
        auto-indexed and never separately registered via the manual
        RAG-query flow (see RegistrationService) — the common case for
        group libraries.
        """
        labels: dict[str, tuple[str, Optional[int]]] = {}
        for entry in self._load().values():
            names = entry.get("target_names") or {}
            owners = entry.get("target_owners") or {}
            for slug, name in names.items():
                if slug not in labels:
                    labels[slug] = (name, owners.get(slug))
        return labels

    def iter_decrypted(self) -> Iterator[tuple[str, str, dict]]:
        """Yield (fingerprint, plaintext_key, entry) for cron use."""
        self._require_enabled()
        for fp, entry in self._load().items():
            try:
                key = self._fernet.decrypt(entry["ciphertext"].encode()).decode()
            except InvalidToken:
                logger.error("Skipping undecryptable key %s", fp)
                continue
            yield fp, key, entry

    def set_embedding_key(self, fp: str, api_key: str, key_name: str, status: str = "ok") -> None:
        """Encrypt and store a provider API key on an existing entry, under its name.

        A user may hold several (a KISSKI key and a Hugging Face token, say);
        storing one never touches the others, so switching presets and back
        finds the earlier key still there.
        """
        self._require_enabled()
        with self._lock:
            data = self._load()
            if fp not in data:
                raise KeyError(f"No auto-index entry for fingerprint {fp}")
            data[fp].setdefault("embedding_keys", {})[key_name] = {
                "ciphertext": self._fernet.encrypt(api_key.encode()).decode(),
                "status": status,
                "rate_limit_until": None,
            }
            self._save(data)

    def get_decrypted_embedding_key(self, fp: str, key_name: Optional[str] = None) -> Optional[tuple[str, str]]:
        """Return (key_name, plaintext_key) for the entry's key called ``key_name``, or None.

        With no ``key_name`` the entry's first key (by name) is returned; meant
        for debugging tools, not the indexing path.
        """
        self._require_enabled()
        entry = self._load().get(fp)
        keys = (entry or {}).get("embedding_keys", {})
        name = key_name or (sorted(keys)[0] if keys else None)
        stored = keys.get(name) if name else None
        if not stored or not stored.get("ciphertext"):
            return None
        try:
            key = self._fernet.decrypt(stored["ciphertext"].encode()).decode()
        except InvalidToken:
            logger.error("Could not decrypt embedding key %s for %s (wrong AUTOINDEX_SECRET?)", name, fp)
            return None
        return name, key

    def set_status(
        self, fp: str, status: str,
        targets: Optional[list[str]] = None,
        target_names: Optional[dict[str, str]] = None,
        target_owners: Optional[dict[str, int]] = None,
    ) -> None:
        """Update an entry's validation status, optionally refreshing its
        resolved targets/names/owners from a fresh validate_key() result.

        Called on every successful cron re-validation so that group
        libraries added before name/owner capture existed (or whose group
        was renamed/transferred) get backfilled without the user having to
        resubmit their key.
        """
        with self._lock:
            data = self._load()
            if fp in data:
                data[fp]["last_status"] = status
                if targets is not None:
                    data[fp]["targets"] = targets
                if target_names is not None:
                    data[fp]["target_names"] = target_names
                if target_owners is not None:
                    data[fp]["target_owners"] = target_owners
                self._save(data)

    def set_embedding_key_status(
        self, fp: str, status: str, rate_limit_until: Optional[str] = None, key_name: Optional[str] = None,
    ) -> None:
        """Set the status of one stored key (``key_name``), or of all of the entry's keys if omitted."""
        with self._lock:
            data = self._load()
            keys = data.get(fp, {}).get("embedding_keys", {})
            for name, stored in keys.items():
                if key_name is None or name == key_name:
                    stored["status"] = status
                    stored["rate_limit_until"] = rate_limit_until
            if keys:
                self._save(data)

    def clear_rate_limits(self) -> int:
        """Clear stored embedding-key rate-limit skips.

        Resets ``rate_limit_until`` on every stored key and turns status
        ``"rate_limited"`` back into ``"ok"``. ``"invalid"`` keys are left
        untouched. Returns the number of keys changed.
        """
        with self._lock:
            data = self._load()
            changed = 0
            for entry in data.values():
                for stored in entry.get("embedding_keys", {}).values():
                    touched = False
                    if stored.get("rate_limit_until"):
                        stored["rate_limit_until"] = None
                        touched = True
                    if stored.get("status") == "rate_limited":
                        stored["status"] = "ok"
                        touched = True
                    if touched:
                        changed += 1
            if changed:
                self._save(data)
            return changed

    def count_embedding_keys_by_name(self) -> dict[str, int]:
        """Count stored, non-invalid embedding keys grouped by key name."""
        counts: dict[str, int] = {}
        for entry in self._load().values():
            for name, stored in entry.get("embedding_keys", {}).items():
                if not stored.get("ciphertext") or stored.get("status") == "invalid":
                    continue
                counts[name] = counts.get(name, 0) + 1
        return counts
