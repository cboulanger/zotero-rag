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
        Zotero-key fields are refreshed here; any embedding_key_* fields
        already present are carried over unchanged.

        A user may only have one registered Zotero key at a time: if this
        api_key rotates them onto a new fingerprint (a different key value —
        e.g. after regenerating their Zotero API key), any other entry
        already registered under the same user_id is removed, after carrying
        its embedding_key_* fields forward — the embedding key isn't tied to
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
                for key in (
                    "embedding_key_ciphertext", "embedding_key_name",
                    "embedding_key_status", "embedding_key_rate_limit_until",
                ):
                    if key in other_entry and key not in existing:
                        existing[key] = other_entry[key]
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
            for key in (
                "embedding_key_ciphertext", "embedding_key_name",
                "embedding_key_status", "embedding_key_rate_limit_until",
            ):
                if key in existing:
                    entry[key] = existing[key]
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

    def list_metadata(self) -> list[dict]:
        """Return entry metadata without ciphertext or plaintext."""
        out = []
        for fp, entry in self._load().items():
            out.append({
                "fingerprint": fp,
                "user_id": entry.get("user_id"),
                "username": entry.get("username"),
                "targets": entry.get("targets", []),
                "last_status": entry.get("last_status"),
                "validated_at": entry.get("validated_at"),
                "has_embedding_key": bool(entry.get("embedding_key_ciphertext")),
                "embedding_key_status": entry.get("embedding_key_status"),
                "embedding_key_rate_limit_until": entry.get("embedding_key_rate_limit_until"),
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
        """Encrypt and store an embedding API key on an existing entry."""
        self._require_enabled()
        with self._lock:
            data = self._load()
            if fp not in data:
                raise KeyError(f"No auto-index entry for fingerprint {fp}")
            data[fp]["embedding_key_ciphertext"] = self._fernet.encrypt(api_key.encode()).decode()
            data[fp]["embedding_key_name"] = key_name
            data[fp]["embedding_key_status"] = status
            data[fp]["embedding_key_rate_limit_until"] = None
            self._save(data)

    def get_decrypted_embedding_key(self, fp: str) -> Optional[tuple[str, str]]:
        """Return (key_name, plaintext_key) for the entry's embedding key, or None."""
        self._require_enabled()
        entry = self._load().get(fp)
        if not entry or not entry.get("embedding_key_ciphertext"):
            return None
        try:
            key = self._fernet.decrypt(entry["embedding_key_ciphertext"].encode()).decode()
        except InvalidToken:
            logger.error("Could not decrypt embedding key for %s (wrong AUTOINDEX_SECRET?)", fp)
            return None
        return entry.get("embedding_key_name"), key

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

    def set_embedding_key_status(self, fp: str, status: str, rate_limit_until: Optional[str] = None) -> None:
        with self._lock:
            data = self._load()
            if fp in data:
                data[fp]["embedding_key_status"] = status
                data[fp]["embedding_key_rate_limit_until"] = rate_limit_until
                self._save(data)
