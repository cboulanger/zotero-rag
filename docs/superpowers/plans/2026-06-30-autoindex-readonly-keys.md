# Per-library read-only auto-indexing keys — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let multiple users contribute read-only Zotero API keys so the cron indexer can auto-index several user libraries (plus accessible groups), validating and storing keys safely.

**Architecture:** Users submit a read-only key via the plugin. The backend validates it against `https://api.zotero.org/keys/<key>` (rejecting any write scope), resolves it to target slugs (`users/<id>` + accessible `groups/<id>`), and stores it Fernet-encrypted. The cron job re-validates all keys each run, prunes invalid ones, dedups targets, and indexes each library once with a key that grants it read. This replaces the single global `ZOTERO_API_KEY` + static `cron-indexing-slugs.conf`.

**Tech Stack:** Python 3.12, FastAPI, aiohttp, `cryptography` (Fernet), filelock, pytest/unittest, `aioresponses` for HTTP mocking. Plugin: vanilla JS (Zotero 7/8).

**Spec:** [docs/superpowers/specs/2026-06-30-autoindex-readonly-keys-design.md](../specs/2026-06-30-autoindex-readonly-keys-design.md)

---

## File structure

- Create `backend/zotero/key_validator.py` — validate a key, enforce read-only, resolve targets.
- Create `backend/services/autoindex_key_store.py` — Fernet-encrypted key persistence.
- Create `backend/api/autoindex.py` — POST/DELETE/GET `/api/autoindex/keys`.
- Create `bin/autoindex_add_key.py` — CLI to onboard a key without the plugin.
- Modify `backend/config/settings.py` — add `autoindex_secret`, `autoindex_keys_path`.
- Modify `backend/main.py` — register the new router.
- Modify `backend/services/cron_indexer.py` — `targets: dict[slug,key]` instead of `slugs + api_key`.
- Modify `bin/index_libraries.py` — load store, re-validate, prune, dedup, build targets.
- Modify `pyproject.toml` — add `cryptography` (and `aioresponses` as test dep if absent).
- Modify `plugin/src/preferences.xhtml`, `plugin/src/preferences.js` — auto-indexing UI.
- Modify `CLAUDE.md` — document the new cron flow.
- Tests: `backend/tests/test_key_validator.py`, `test_autoindex_key_store.py`, `test_autoindex_api.py`, update `test_cron_indexer.py`.

---

## Task 1: Dependencies and settings

**Files:**
- Modify: `pyproject.toml`
- Modify: `backend/config/settings.py`
- Test: `backend/tests/test_config.py`

- [ ] **Step 1: Add the failing test for the new settings fields**

Add to `backend/tests/test_config.py`:

```python
def test_autoindex_settings_defaults(monkeypatch):
    monkeypatch.delenv("AUTOINDEX_SECRET", raising=False)
    from backend.config.settings import Settings
    s = Settings()
    assert s.autoindex_secret is None
    # Defaults under data_path/system
    assert str(s.autoindex_keys_path).endswith("system/autoindex_keys.json")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest backend/tests/test_config.py::test_autoindex_settings_defaults -v`
Expected: FAIL (`AttributeError: ... 'autoindex_secret'`).

- [ ] **Step 3: Add `cryptography` to dependencies**

In `pyproject.toml`, add to the `dependencies` array (next to `filelock`):

```toml
    "cryptography>=42.0.0",
```

Then run: `uv sync`

- [ ] **Step 4: Add the settings fields**

In `backend/config/settings.py`, add fields near `registrations_path` (~line 131):

```python
    autoindex_secret: Optional[str] = Field(
        default=None,
        description="Fernet secret (urlsafe base64, 32 bytes) for encrypting auto-index keys. "
                    "If unset, the auto-indexing key feature is disabled.",
    )
    autoindex_keys_path: Optional[Path] = Field(
        default=None,
        description="Path to the encrypted auto-index keys JSON. Defaults to "
                    "<data_path>/system/autoindex_keys.json.",
    )
```

Add `autoindex_keys_path` to the path `field_validator` list (~line 173) and to the
defaults block in the `model_validator` (~line 193), mirroring `registrations_path`:

```python
        if self.autoindex_keys_path is None:
            self.autoindex_keys_path = self.data_path / "system" / "autoindex_keys.json"
```

In `ensure_directories()` (~line 218), add:

```python
        if self.autoindex_keys_path:
            self.autoindex_keys_path.parent.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest backend/tests/test_config.py::test_autoindex_settings_defaults -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock backend/config/settings.py backend/tests/test_config.py
git commit -m "feat: add cryptography dep and autoindex settings"
```

---

## Task 2: Key validator

**Files:**
- Create: `backend/zotero/key_validator.py`
- Test: `backend/tests/test_key_validator.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_key_validator.py`:

```python
"""Unit tests for backend.zotero.key_validator."""

import unittest

from aioresponses import aioresponses

from backend.zotero.key_validator import validate_key, ZOTERO_API_BASE


class KeyValidatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_user_library(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/RO", payload={
                "key": "RO", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True, "files": True, "notes": True}},
            })
            res = await validate_key("RO")
        self.assertTrue(res.read_only)
        self.assertEqual(res.user_id, 39226)
        self.assertEqual(res.targets, ["users/39226"])

    async def test_write_scope_rejected(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/RW", payload={
                "key": "RW", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True, "write": True}},
            })
            res = await validate_key("RW")
        self.assertFalse(res.read_only)
        self.assertIsNotNone(res.reason)
        self.assertEqual(res.targets, [])

    async def test_group_write_scope_rejected(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GW", payload={
                "key": "GW", "userID": 1, "username": "u",
                "access": {"user": {"library": True},
                           "groups": {"all": {"library": True, "write": True}}},
            })
            res = await validate_key("GW")
        self.assertFalse(res.read_only)

    async def test_groups_all_enumerated(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GA", payload={
                "key": "GA", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True},
                           "groups": {"all": {"library": True}}},
            })
            m.get(f"{ZOTERO_API_BASE}/users/39226/groups", payload=[
                {"id": 456}, {"id": 789},
            ])
            res = await validate_key("GA")
        self.assertTrue(res.read_only)
        self.assertEqual(set(res.targets), {"users/39226", "groups/456", "groups/789"})

    async def test_specific_group(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/SG", payload={
                "key": "SG", "userID": 1, "username": "u",
                "access": {"groups": {"123": {"library": True}}},
            })
            res = await validate_key("SG")
        self.assertTrue(res.read_only)
        self.assertEqual(res.targets, ["groups/123"])

    async def test_revoked_key_404(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GONE", status=404)
            res = await validate_key("GONE")
        self.assertFalse(res.read_only)
        self.assertIn("revoked", res.reason.lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest backend/tests/test_key_validator.py -v`
Expected: FAIL (module `backend.zotero.key_validator` does not exist). If `aioresponses`
import fails, add it: `uv add --dev aioresponses` then add to `pyproject.toml` dev deps.

- [ ] **Step 3: Implement the validator**

Create `backend/zotero/key_validator.py`:

```python
"""Validate Zotero API keys and resolve them to indexable library slugs.

A key is accepted only if it is read-only (no write flag anywhere in `access`)
and grants read access to at least one library. The validator resolves the key
to concrete target slugs: `users/<userID>` plus accessible `groups/<id>`.
"""

import logging
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"


@dataclass
class KeyValidation:
    """Result of validating a Zotero API key."""

    user_id: int | None
    username: str | None
    targets: list[str] = field(default_factory=list)
    read_only: bool = False
    reason: str | None = None


def _has_write(access: dict) -> bool:
    """True if any write flag is set anywhere in the access object."""
    if access.get("user", {}).get("write"):
        return True
    for grp in access.get("groups", {}).values():
        if isinstance(grp, dict) and grp.get("write"):
            return True
    return False


async def validate_key(api_key: str, base_url: str = ZOTERO_API_BASE) -> KeyValidation:
    """Validate `api_key` and resolve indexable target slugs.

    Returns a KeyValidation. On any failure (revoked, write scope, network),
    `read_only` is False and `reason` explains why; `targets` is empty.
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{base_url}/keys/{api_key}") as resp:
                if resp.status == 404:
                    return KeyValidation(None, None, reason="Key not found (revoked or expired).")
                if resp.status != 200:
                    return KeyValidation(None, None, reason=f"Zotero key lookup failed (HTTP {resp.status}).")
                data = await resp.json()
        except aiohttp.ClientError as exc:
            return KeyValidation(None, None, reason=f"Could not reach Zotero API: {exc}")

        user_id = data.get("userID")
        username = data.get("username")
        access = data.get("access", {})

        if _has_write(access):
            return KeyValidation(
                user_id, username, read_only=False,
                reason="This key has write access. Create a read-only key at "
                       "https://www.zotero.org/settings/keys.",
            )

        targets: list[str] = []
        if access.get("user", {}).get("library"):
            targets.append(f"users/{user_id}")

        groups = access.get("groups", {})
        if groups.get("all", {}).get("library"):
            # Key can read all groups the user belongs to — enumerate them.
            try:
                async with session.get(f"{base_url}/users/{user_id}/groups") as gresp:
                    if gresp.status == 200:
                        for grp in await gresp.json():
                            targets.append(f"groups/{grp['id']}")
            except aiohttp.ClientError as exc:
                logger.warning("Failed to enumerate groups for user %s: %s", user_id, exc)
        for gid, grp in groups.items():
            if gid != "all" and isinstance(grp, dict) and grp.get("library"):
                slug = f"groups/{gid}"
                if slug not in targets:
                    targets.append(slug)

        if not targets:
            return KeyValidation(
                user_id, username, read_only=False,
                reason="Key grants no readable library.",
            )

        return KeyValidation(user_id, username, targets=targets, read_only=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_key_validator.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/zotero/key_validator.py backend/tests/test_key_validator.py pyproject.toml uv.lock
git commit -m "feat: add read-only Zotero key validator with target resolution"
```

---

## Task 3: Encrypted key store

**Files:**
- Create: `backend/services/autoindex_key_store.py`
- Test: `backend/tests/test_autoindex_key_store.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_autoindex_key_store.py`:

```python
"""Unit tests for backend.services.autoindex_key_store."""

import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import KeyValidation


def _validation():
    return KeyValidation(user_id=39226, username="cboulanger",
                         targets=["users/39226", "groups/456"], read_only=True)


class AutoIndexKeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "autoindex_keys.json"
        self.secret = Fernet.generate_key().decode()
        self.store = AutoIndexKeyStore(self.path, self.secret)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_decrypt_roundtrip(self):
        fp = self.store.add("SECRETKEY", _validation())
        self.assertEqual(self.store.get_decrypted(fp), "SECRETKEY")

    def test_list_metadata_has_no_secrets(self):
        self.store.add("SECRETKEY", _validation())
        meta = self.store.list_metadata()
        self.assertEqual(len(meta), 1)
        entry = meta[0]
        self.assertNotIn("ciphertext", entry)
        self.assertEqual(entry["user_id"], 39226)
        self.assertEqual(entry["targets"], ["users/39226", "groups/456"])
        # The raw plaintext must not appear anywhere in the serialized metadata
        self.assertNotIn("SECRETKEY", str(meta))

    def test_remove_by_key(self):
        self.store.add("SECRETKEY", _validation())
        self.assertTrue(self.store.remove_by_key("SECRETKEY"))
        self.assertEqual(self.store.list_metadata(), [])

    def test_iter_decrypted(self):
        self.store.add("K1", _validation())
        items = list(self.store.iter_decrypted())
        self.assertEqual(len(items), 1)
        fp, key, entry = items[0]
        self.assertEqual(key, "K1")
        self.assertEqual(entry["targets"], ["users/39226", "groups/456"])

    def test_disabled_when_no_secret(self):
        store = AutoIndexKeyStore(self.path, secret=None)
        self.assertFalse(store.enabled)
        with self.assertRaises(RuntimeError):
            store.add("K", _validation())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest backend/tests/test_autoindex_key_store.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the store**

Create `backend/services/autoindex_key_store.py`:

```python
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

    def add(self, api_key: str, validation: KeyValidation) -> str:
        """Encrypt and store a validated key. Returns its fingerprint."""
        self._require_enabled()
        fp = fingerprint(api_key)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            data = self._load()
            data[fp] = {
                "ciphertext": self._fernet.encrypt(api_key.encode()).decode(),
                "user_id": validation.user_id,
                "username": validation.username,
                "targets": list(validation.targets),
                "validated_at": now,
                "last_status": "ok",
            }
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
            })
        return out

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

    def set_status(self, fp: str, status: str) -> None:
        with self._lock:
            data = self._load()
            if fp in data:
                data[fp]["last_status"] = status
                self._save(data)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_autoindex_key_store.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/services/autoindex_key_store.py backend/tests/test_autoindex_key_store.py
git commit -m "feat: add Fernet-encrypted auto-index key store"
```

---

## Task 4: Backend API router

**Files:**
- Create: `backend/api/autoindex.py`
- Modify: `backend/main.py:17,188`
- Test: `backend/tests/test_autoindex_api.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_autoindex_api.py`:

```python
"""Endpoint tests for /api/autoindex/keys."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.zotero.key_validator import KeyValidation


class AutoIndexApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        s = get_settings()
        s.api_key = None  # disable auth middleware for the test
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_post_accepts_read_only_key(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", return_value=validation):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["targets"], ["users/1"])
        # Plaintext key must not appear in the admin list
        r2 = self.client.get("/api/autoindex/keys")
        self.assertNotIn("RO", r2.text)

    def test_post_rejects_write_key(self):
        validation = KeyValidation(user_id=1, username="u", read_only=False,
                                   reason="This key has write access.")
        with patch("backend.api.autoindex.validate_key", return_value=validation):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RW"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("write access", r.json()["detail"])

    def test_delete_removes_key(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", return_value=validation):
            self.client.post("/api/autoindex/keys", json={"api_key": "RO"})
            r = self.client.request("DELETE", "/api/autoindex/keys", json={"api_key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["removed"])

    def test_disabled_without_secret(self):
        get_settings().autoindex_secret = None
        r = self.client.get("/api/autoindex/keys")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
```

Note: `validate_key` is async; patch it with an `AsyncMock`-returning value. Use
`patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation))`
if the plain `return_value` form does not await correctly — adjust in Step 3 verification.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest backend/tests/test_autoindex_api.py -v`
Expected: FAIL (router not mounted).

- [ ] **Step 3: Implement the router**

Create `backend/api/autoindex.py`:

```python
"""Auto-index key endpoints.

POST   /api/autoindex/keys  — submit a read-only key (validated, stored encrypted)
DELETE /api/autoindex/keys  — remove a key
GET    /api/autoindex/keys  — list key metadata (admin; no plaintext)

All endpoints are protected by the global X-API-Key middleware. When
AUTOINDEX_SECRET is unset the feature is disabled and endpoints return 503.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import validate_key

router = APIRouter()
logger = logging.getLogger(__name__)


class KeyRequest(BaseModel):
    api_key: str


def _store() -> AutoIndexKeyStore:
    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        raise HTTPException(
            status_code=503,
            detail="Auto-indexing is not configured on this server (AUTOINDEX_SECRET unset).",
        )
    return store


@router.post("/autoindex/keys", summary="Submit a read-only auto-index key")
async def add_key(request: KeyRequest) -> dict:
    store = _store()
    validation = await validate_key(request.api_key)
    if not validation.read_only:
        raise HTTPException(status_code=400, detail=validation.reason or "Key is not read-only.")
    store.add(request.api_key, validation)
    return {
        "user_id": validation.user_id,
        "username": validation.username,
        "targets": validation.targets,
    }


@router.delete("/autoindex/keys", summary="Remove an auto-index key")
async def delete_key(request: KeyRequest) -> dict:
    store = _store()
    removed = store.remove_by_key(request.api_key)
    return {"removed": removed}


@router.get("/autoindex/keys", summary="List auto-index key metadata (admin)")
async def list_keys() -> dict:
    store = _store()
    return {"keys": store.list_metadata()}
```

In `backend/main.py` line 17, add `autoindex` to the import:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex
```

After line 188 (the `public_query` include), add:

```python
app.include_router(autoindex.router, prefix="/api", tags=["autoindex"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_autoindex_api.py -v`
Expected: PASS (4 tests). If async patching fails, switch the patches to
`new=AsyncMock(return_value=validation)` (import `from unittest.mock import AsyncMock`).

- [ ] **Step 5: Commit**

```bash
git add backend/api/autoindex.py backend/main.py backend/tests/test_autoindex_api.py
git commit -m "feat: add /api/autoindex/keys endpoints"
```

---

## Task 5: CronIndexer accepts per-slug targets

**Files:**
- Modify: `backend/services/cron_indexer.py:84-101,399-401`
- Test: `backend/tests/test_cron_indexer.py:33-45`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_cron_indexer.py`:

```python
def test_index_slug_uses_per_slug_key(tmp_path=None):
    import tempfile, logging
    from unittest.mock import MagicMock, AsyncMock, patch
    from backend.services.cron_indexer import CronIndexer
    with tempfile.TemporaryDirectory() as d:
        from pathlib import Path
        tmp = Path(d)
        emb = MagicMock(); emb.get_rate_limit_info = AsyncMock(return_value=None)
        vs = MagicMock(); vs.get_library_metadata.return_value = None
        indexer = CronIndexer(
            targets={"users/12345": "KEY_A", "groups/678": "KEY_B"},
            vector_store=vs, embedding_service=emb,
            lock_file=tmp / "l", status_file=tmp / "s.json",
            log=logging.getLogger("t"),
        )
        assert sorted(indexer.slugs) == ["groups/678", "users/12345"]
        assert indexer.targets["groups/678"] == "KEY_B"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest backend/tests/test_cron_indexer.py::test_index_slug_uses_per_slug_key -v`
Expected: FAIL (`CronIndexer() got an unexpected keyword argument 'targets'`).

- [ ] **Step 3: Refactor the constructor**

In `backend/services/cron_indexer.py`, change the `__init__` signature (~line 84). Replace
`slugs: list[str]` and `api_key: str` with `targets: dict[str, str]`:

```python
    def __init__(
        self,
        targets: dict[str, str],
        vector_store: VectorStore,
        embedding_service: EmbeddingService,
        lock_file: Path,
        status_file: Path,
        log: logging.Logger,
        mode: Literal["auto", "incremental", "full"] = "auto",
        max_items: Optional[int] = None,
        progress_update_interval: int = 10,
    ):
        self.targets = targets
        self.slugs = list(targets.keys())
        self.vector_store = vector_store
```

Remove the old `self.slugs = slugs` and `self.api_key = api_key` lines.

In `_index_slug` (line 401), use the per-slug key:

```python
        web_api = ZoteroWebAPI(api_key=self.targets[slug_info.slug])
```

- [ ] **Step 4: Update the existing test helper**

In `backend/tests/test_cron_indexer.py`, change `_make_indexer` (lines 33-45) to build a
targets dict from `slugs`:

```python
    return CronIndexer(
        targets={s: "test-api-key" for s in slugs},
        vector_store=vector_store,
        embedding_service=embedding_service,
        lock_file=tmp_dir / "cron.lock",
        status_file=tmp_dir / "cron_status.json",
```

Remove the now-removed `slugs=` and `api_key=` kwargs.

- [ ] **Step 5: Run the cron tests to verify they pass**

Run: `uv run pytest backend/tests/test_cron_indexer.py -v`
Expected: PASS (existing tests + the new one).

- [ ] **Step 6: Commit**

```bash
git add backend/services/cron_indexer.py backend/tests/test_cron_indexer.py
git commit -m "refactor: CronIndexer takes per-slug targets dict"
```

---

## Task 6: index_libraries.py loads keys, re-validates, dedups

**Files:**
- Modify: `bin/index_libraries.py:89-185`
- Create: `backend/services/autoindex_resolver.py`
- Test: `backend/tests/test_autoindex_resolver.py`

The dedup/re-validation logic is unit-testable in isolation, so extract it into a helper
module rather than burying it in the CLI.

- [ ] **Step 1: Write the failing test for the resolver**

Create `backend/tests/test_autoindex_resolver.py`:

```python
"""Unit tests for resolve_targets (re-validate + dedup)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.autoindex_resolver import resolve_targets
from backend.zotero.key_validator import KeyValidation


class ResolveTargetsTest(unittest.IsolatedAsyncioTestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutoIndexKeyStore(Path(tmp.name) / "k.json", Fernet.generate_key().decode())

    async def test_dedup_shared_group(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2", "groups/99"], read_only=True)
        store.add("KA", v1)
        store.add("KB", v2)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)
        # groups/99 indexed once; both user libs present
        self.assertEqual(set(targets), {"users/1", "users/2", "groups/99"})
        self.assertEqual(issues, [])

    async def test_prunes_revoked_key(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v1)
        revoked = KeyValidation(1, "a", read_only=False, reason="Key not found (revoked or expired).")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=revoked)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertIn("revoked", issues[0]["reason"].lower())
        # The pruned key is removed from the store
        self.assertEqual(store.list_metadata(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest backend/tests/test_autoindex_resolver.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the resolver**

Create `backend/services/autoindex_resolver.py`:

```python
"""Re-validate stored auto-index keys and resolve a deduplicated slug→key map.

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_autoindex_resolver.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Rewire `bin/index_libraries.py`**

Replace the slug/key collection block in `_main` (~lines 110-145, from `# Collect slugs`
through the `CronIndexer(...)` construction). New body after `settings = get_settings()`
and logging setup:

```python
    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.services.autoindex_resolver import resolve_targets

    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        log.error("AUTOINDEX_SECRET is not set; no keys can be decrypted. Nothing to index.")
        return 1

    targets, key_issues = await resolve_targets(store)
    for issue in key_issues:
        log.warning("Key pruned for user %s: %s", issue.get("user"), issue.get("reason"))

    if not targets:
        log.error("No valid auto-index keys found. Submit a read-only key via the plugin.")
        return 1

    log.info("Starting cron indexer for: %s", ", ".join(sorted(targets)))

    lock_file = settings.data_path / "system" / "cron_indexer.lock"
    status_file = settings.data_path / "system" / "cron_status.json"

    if args.force and lock_file.exists():
        log.warning("--force: removing existing lock file.")
        lock_file.unlink(missing_ok=True)

    try:
        vector_store = make_vector_store()
        embedding_service = make_embedding_service()

        indexer = CronIndexer(
            targets=targets,
            vector_store=vector_store,
            embedding_service=embedding_service,
            lock_file=lock_file,
            status_file=status_file,
            log=log,
            mode=args.mode,
            max_items=args.max_items,
        )
        indexer.key_issues = key_issues  # surfaced in status; see Step 6
        stats = await indexer.run()
```

Remove the old `slugs`, `--slugs-file` reading, and the `api_key = settings.zotero_api_key`
block. Keep the `--slugs-file`/`slugs` argparse args for now but ignore them (or delete them;
deleting is cleaner — remove the `slugs` positional and `--slugs-file` argument definitions).

- [ ] **Step 6: Surface key_issues in cron status**

In `backend/services/cron_indexer.py`, initialise `self.key_issues = []` in `__init__`, and
in the method that builds the status dict (search for where `status["slugs"]` /
`_write_status` first populates the top-level status), add:

```python
        status["key_issues"] = getattr(self, "key_issues", [])
```

Run: `grep -n "status = {" backend/services/cron_indexer.py` to find the status init site
and add the line there.

- [ ] **Step 7: Run the full cron + resolver suite**

Run: `uv run pytest backend/tests/test_cron_indexer.py backend/tests/test_autoindex_resolver.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add bin/index_libraries.py backend/services/autoindex_resolver.py \
        backend/services/cron_indexer.py backend/tests/test_autoindex_resolver.py
git commit -m "feat: cron indexer resolves per-library keys with re-validation and dedup"
```

---

## Task 7: CLI onboarding helper

**Files:**
- Create: `bin/autoindex_add_key.py`

- [ ] **Step 1: Implement the CLI**

Create `bin/autoindex_add_key.py`:

```python
"""Onboard a read-only Zotero key for auto-indexing without the plugin.

Usage:
    uv run python bin/autoindex_add_key.py <read-only-key>

Validates the key (must be read-only), resolves its target libraries, and stores
it encrypted in the auto-index key store. Requires AUTOINDEX_SECRET to be set.
"""

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


async def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: uv run python bin/autoindex_add_key.py <read-only-key>")
        return 2
    api_key = argv[0].strip()

    from backend.config.settings import get_settings
    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.zotero.key_validator import validate_key

    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        print("[FAIL] AUTOINDEX_SECRET is not set; cannot store keys.")
        return 1

    validation = await validate_key(api_key)
    if not validation.read_only:
        print(f"[FAIL] {validation.reason}")
        return 1

    fp = store.add(api_key, validation)
    print(f"[PASS] Stored key {fp} for {validation.username} "
          f"({validation.user_id}) -> {', '.join(validation.targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
```

- [ ] **Step 2: Smoke-test the usage/error paths (no real key needed)**

Run: `AUTOINDEX_SECRET= uv run python bin/autoindex_add_key.py DUMMY`
Expected: prints `[FAIL] AUTOINDEX_SECRET is not set; cannot store keys.` and exits 1.

Run: `uv run python bin/autoindex_add_key.py`
Expected: prints the usage line and exits 2.

- [ ] **Step 3: Commit**

```bash
git add bin/autoindex_add_key.py
git commit -m "feat: add CLI to onboard a read-only auto-index key"
```

---

## Task 8: Plugin auto-indexing UI

**Files:**
- Modify: `plugin/src/preferences.xhtml`
- Modify: `plugin/src/preferences.js`

The plugin uses a hot-reload dev server — do NOT rebuild; changes reload automatically.
Match the existing pattern in [registerLibrary](../../../plugin/src/zotero-rag.js#L555) for
auth headers and the HTTP→HTTPS guard.

- [ ] **Step 1: Add the UI section to preferences.xhtml**

Open `plugin/src/preferences.xhtml`, find an existing preferences group, and add a sibling
section (use the `html:` namespace consistent with the file's existing elements):

```xml
<html:fieldset id="zrag-autoindex">
  <html:legend>&zrag.autoindex.title;</html:legend>
  <html:p>&zrag.autoindex.desc;</html:p>
  <html:label for="zrag-autoindex-key">&zrag.autoindex.keyLabel;</html:label>
  <html:input type="password" id="zrag-autoindex-key" />
  <html:button id="zrag-autoindex-enable">&zrag.autoindex.enable;</html:button>
  <html:button id="zrag-autoindex-remove">&zrag.autoindex.remove;</html:button>
  <html:div id="zrag-autoindex-status"></html:div>
</html:fieldset>
```

Add the referenced entity strings to the plugin's `.dtd`/`.ftl` locale file (find the
existing one under `plugin/locale/`), e.g. for `en-US`:

```
zrag.autoindex.title = Automatic indexing
zrag.autoindex.desc  = Submit a READ-ONLY Zotero API key (create one at zotero.org/settings/keys) to have your library auto-indexed hourly.
zrag.autoindex.keyLabel = Read-only API key:
zrag.autoindex.enable = Enable auto-indexing
zrag.autoindex.remove = Remove
```

(Match the existing locale file format — `.ftl` Fluent vs `.dtd`. Inspect a sibling string
to confirm syntax before adding.)

- [ ] **Step 2: Wire up the handlers in preferences.js**

In `plugin/src/preferences.js`, in the init/load function that runs when the prefs pane
opens, add listeners. Reuse the backend URL + auth-header helpers already used by the pane
(grep the file for `backendURL` / `getAuthHeaders` to match the existing accessor):

```javascript
/** Post the read-only key to the backend and show the resolved libraries. */
async function enableAutoIndex() {
    const keyEl = document.getElementById('zrag-autoindex-key');
    const statusEl = document.getElementById('zrag-autoindex-status');
    const apiKey = keyEl.value.trim();
    if (!apiKey) { statusEl.textContent = 'Please enter a read-only API key.'; return; }
    const url = `${getBackendURL()}/api/autoindex/keys`;
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ api_key: apiKey }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            statusEl.textContent = `Error: ${err.detail || resp.status}`;
            return;
        }
        const data = await resp.json();
        statusEl.textContent = `Auto-indexing enabled for: ${data.targets.join(', ')}`;
        keyEl.value = '';
    } catch (e) {
        statusEl.textContent = `Request failed: ${e.message}`;
    }
}

/** Remove this key from the backend. */
async function removeAutoIndex() {
    const keyEl = document.getElementById('zrag-autoindex-key');
    const statusEl = document.getElementById('zrag-autoindex-status');
    const apiKey = keyEl.value.trim();
    if (!apiKey) { statusEl.textContent = 'Enter the key to remove.'; return; }
    const url = `${getBackendURL()}/api/autoindex/keys`;
    const resp = await fetch(url, {
        method: 'DELETE',
        headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ api_key: apiKey }),
    });
    const data = await resp.json().catch(() => ({}));
    statusEl.textContent = data.removed ? 'Auto-indexing key removed.' : 'No matching key found.';
    keyEl.value = '';
}

document.getElementById('zrag-autoindex-enable')
    .addEventListener('command', enableAutoIndex);
document.getElementById('zrag-autoindex-remove')
    .addEventListener('command', removeAutoIndex);
```

Replace `getBackendURL()` / `getAuthHeaders()` with the actual accessors used in this file
(confirm via grep). Use the `command` event for XUL buttons or `click` for HTML buttons —
match what other buttons in this pane use.

- [ ] **Step 3: Manually verify in the running Zotero dev instance**

With `npm run start` running and the backend up with `AUTOINDEX_SECRET` set, open the plugin
preferences, paste a read-only key, click Enable, and confirm the status line lists the
resolved libraries. Paste a write-enabled key and confirm the "has write access" error shows.

- [ ] **Step 4: Commit**

```bash
git add plugin/src/preferences.xhtml plugin/src/preferences.js plugin/locale/
git commit -m "feat: plugin UI to submit read-only auto-index keys"
```

---

## Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md` (the "Debugging the cron indexer" section)

- [ ] **Step 1: Update the cron docs**

In `CLAUDE.md`, update the "Debugging the cron indexer" section to reflect the new flow:

- The cron job no longer uses `--slugs-file` or `ZOTERO_API_KEY`. Targets come from the
  encrypted auto-index key store (`<data_path>/system/autoindex_keys.json`).
- `AUTOINDEX_SECRET` (a Fernet key, generate with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
  must be set in the deploy env; without it the cron job exits with "no decryptable keys".
- Users add keys via the plugin (Preferences → Automatic indexing) or an admin can use
  `uv run python bin/autoindex_add_key.py <read-only-key>`.
- The manual run command becomes (no `--slugs-file`):
  ```bash
  sudo podman exec zotero-rag-zotero-rag-panya-de python bin/index_libraries.py \
    > /dev/null 2>> /home/cloud/data/zotero-rag/logs/cron_indexer.log &
  ```
- Note that each run re-validates keys and prunes revoked/write-scoped ones; pruned keys
  appear in `cron_status.json` under `key_issues` and in the plugin prefs.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document auto-index key flow for the cron indexer"
```

---

## Task 10: Full verification

- [ ] **Step 1: Run the whole backend suite**

Run: `uv run pytest -q`
Expected: PASS (no regressions; new tests green). Investigate any failures referencing
`slugs=`/`api_key=` — those are call sites of the old CronIndexer constructor that still
need updating.

- [ ] **Step 2: Grep for stragglers**

Run: `grep -rn "zotero_api_key\|--slugs-file\|cron-indexing-slugs" backend bin`
Expected: only historical/doc references remain; no live code path still depends on the
global key or static slugs file for cron indexing.

- [ ] **Step 3: Final commit if anything changed**

```bash
git add -A && git commit -m "chore: finalize auto-index key migration"
```

---

## Self-review notes

- **Spec coverage:** validator (Task 2), encrypted store (Task 3), API (Task 4), plugin UX
  (Task 8), cron refactor + re-validate/dedup (Tasks 5–6), migration CLI + docs (Tasks 7, 9),
  status `key_issues` (Task 6 Step 6). All spec sections mapped.
- **Type consistency:** `KeyValidation(user_id, username, targets, read_only, reason)`,
  `AutoIndexKeyStore` methods (`add`, `get_decrypted`, `remove`, `remove_by_key`,
  `list_metadata`, `iter_decrypted`, `set_status`), `resolve_targets(store) -> (targets, issues)`,
  and `CronIndexer(targets=...)` are used consistently across tasks.
- **Async patching caveat** flagged in Task 4 (use `AsyncMock` if needed).
