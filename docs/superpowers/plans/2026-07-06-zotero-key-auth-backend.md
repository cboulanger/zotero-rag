# Zotero-Key Backend Auth (IDOR Fix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the cross-tenant IDOR in the backend (any shared-key holder can read/delete any other user's library) by authenticating every non-loopback `/api/*` request with the caller's own validated, gate-approved Zotero API key, and enforcing that every `library_id` named in a request is within that key's readable targets.

**Architecture:** A single async resolver (`resolve_zotero_identity`) turns a request's headers into an `Optional[ZoteroIdentity]`: `None` on the loopback path (single trusted local user, Part 4 of the design doc) and on the transitional legacy-shared-key path (Part 5); otherwise a validated `(user_id, username, targets)` gated by group-membership-or-allowlist (Part 2). The existing `api_key_middleware` in `backend/main.py` calls this resolver once per request for every `/api/*` path and stashes the result on `request.state.zotero_identity`; a cheap dependency (`get_zotero_identity`) lets route handlers read it back without re-validating. Handlers that name a `library_id` call `assert_can_access(identity, library_id)`, which is a no-op for `None` and a 403 otherwise. A startup check (`assert_safe_to_start`) refuses to boot in remote mode without a configured access gate (fail-closed).

**Tech Stack:** FastAPI (dependency injection + ASGI middleware), `aiohttp` (Zotero API calls via the existing `key_validator.validate_key`), Python stdlib `unittest` + `aioresponses`/`unittest.mock.AsyncMock` (existing test conventions in `backend/tests/`).

**Source spec:** `docs/history/plan-zotero-key-auth.md` (Parts 1, 2, 4; Part 5 step 2 only — the transitional dual-auth window). Parts 3 (plugin wizard) and the rest of Part 5 (cutover) are a separate follow-up plan once these backend endpoints exist.

**Explicitly out of scope for this plan** (not named in the vulnerability report or the design doc's Part 1.2 table — left for a follow-up if desired): `GET /api/libraries/{id}/status`, `GET /api/libraries/{id}/index-status`, `POST /api/libraries/{id}/reconcile-count`. These remain reachable by any gate-authorized identity without per-library filtering, same as today's behavior for any shared-key holder — no regression, just not hardened in this pass.

---

### Task 1: Settings — access-gate configuration fields

**Files:**
- Modify: `backend/config/settings.py:163-173` (insert new fields after `zotero_api_key`, before `require_registration`)
- Test: `backend/tests/test_settings_access_gate.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the Part 2 access-gate settings fields."""

import os
import unittest
from unittest.mock import patch

from backend.config.settings import Settings


class AccessGateSettingsTest(unittest.TestCase):
    def test_authorized_group_id_defaults_none(self):
        s = Settings()
        self.assertIsNone(s.authorized_group_id)

    def test_authorized_user_ids_defaults_empty(self):
        s = Settings()
        self.assertEqual(s.authorized_user_ids, [])

    def test_authorized_group_id_from_env(self):
        with patch.dict(os.environ, {"AUTHORIZED_GROUP_ID": "998877"}):
            s = Settings()
        self.assertEqual(s.authorized_group_id, 998877)

    def test_authorized_user_ids_parses_comma_separated_env(self):
        with patch.dict(os.environ, {"AUTHORIZED_USER_IDS": "123, 456"}):
            s = Settings()
        self.assertEqual(s.authorized_user_ids, [123, 456])

    def test_authorized_user_ids_accepts_list(self):
        s = Settings(authorized_user_ids=[1, 2])
        self.assertEqual(s.authorized_user_ids, [1, 2])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_settings_access_gate.py -v`
Expected: FAIL — `Settings` has no field `authorized_group_id` (pydantic ignores unknown kwargs silently by default in this project's `extra="ignore"` config, so the first two tests fail on `AttributeError: 'Settings' object has no attribute 'authorized_group_id'`).

- [ ] **Step 3: Add the fields**

In `backend/config/settings.py`, insert immediately after the `zotero_api_key` field (currently lines 163-167) and before `require_registration`:

```python
    authorized_group_id: Optional[int] = Field(
        default=None,
        description="Zotero group ID that gates access to this instance in remote mode. "
                    "A caller's validated Zotero key must grant read access to "
                    "groups/<id> (i.e. the caller is a member of this group) to be "
                    "authorized. Combines with AUTHORIZED_USER_IDS via OR. "
                    "Ignored on loopback deployments (api_host=localhost/127.0.0.1)."
    )
    authorized_user_ids: list[int] = Field(
        default=[],
        description="Explicit allowlist of Zotero user IDs authorized to use this "
                    "instance in remote mode, in addition to AUTHORIZED_GROUP_ID "
                    "membership (OR semantics). Comma-separated list of integers "
                    "in the environment. At least one of AUTHORIZED_GROUP_ID / "
                    "AUTHORIZED_USER_IDS must be set for the server to start in "
                    "remote mode — see backend.services.access_gate.assert_safe_to_start."
    )

    @field_validator("authorized_user_ids", mode="before")
    @classmethod
    def parse_authorized_user_ids(cls, v):
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_settings_access_gate.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_settings_access_gate.py
git commit -m "feat: add AUTHORIZED_GROUP_ID / AUTHORIZED_USER_IDS settings"
```

---

### Task 2: Zotero identity validation cache

**Files:**
- Create: `backend/services/zotero_identity.py`
- Test: `backend/tests/test_zotero_identity.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for backend.services.zotero_identity (the Part 1.3 validation cache)."""

import unittest
from unittest.mock import AsyncMock, patch

from backend.services.zotero_identity import ZoteroIdentity, ZoteroIdentityCache
from backend.zotero.key_validator import KeyValidation


class ZoteroIdentityCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_caches_successful_validation(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        mock_validate = AsyncMock(return_value=validation)
        cache = ZoteroIdentityCache(ttl_seconds=60)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, validation)
        self.assertIs(second, validation)
        mock_validate.assert_awaited_once()

    async def test_expired_entry_revalidates(self):
        old = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        new = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/2"], read_only=True)
        mock_validate = AsyncMock(side_effect=[old, new])
        cache = ZoteroIdentityCache(ttl_seconds=0)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, old)
        self.assertIs(second, new)
        self.assertEqual(mock_validate.await_count, 2)

    async def test_transient_failure_serves_stale_cache(self):
        good = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        transient_failure = KeyValidation(None, None, read_only=False, reason="down", transient=True)
        mock_validate = AsyncMock(side_effect=[good, transient_failure])
        cache = ZoteroIdentityCache(ttl_seconds=0)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, good)
        self.assertIs(second, good)  # stale cache served instead of the transient failure

    async def test_hard_failure_not_masked_by_stale_cache(self):
        good = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        revoked = KeyValidation(None, None, read_only=False, reason="revoked", transient=False)
        mock_validate = AsyncMock(side_effect=[good, revoked])
        cache = ZoteroIdentityCache(ttl_seconds=0)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, good)
        self.assertIs(second, revoked)  # a hard failure always overwrites the cache

    async def test_no_cache_entry_and_transient_failure_propagates(self):
        transient_failure = KeyValidation(None, None, read_only=False, reason="down", transient=True)
        mock_validate = AsyncMock(return_value=transient_failure)
        cache = ZoteroIdentityCache(ttl_seconds=60)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            result = await cache.resolve("KEY")
        self.assertIs(result, transient_failure)

    def test_zotero_identity_is_a_plain_value_object(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self.assertEqual(identity.user_id, 1)
        self.assertEqual(identity.username, "u")
        self.assertEqual(identity.targets, ["users/1"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_zotero_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.zotero_identity'`

- [ ] **Step 3: Implement the module**

```python
"""Zotero-key identity resolution and caching (Part 1.3 of the design).

A Zotero API key resolves to a stable identity — (user_id, username, targets)
— via backend.zotero.key_validator.validate_key(), which calls
api.zotero.org. That call is too slow and too fragile to make on every
request, so ZoteroIdentityCache caches the result per key fingerprint with a
TTL. A transient zotero.org failure (5xx, network error) serves a still-cached
entry rather than failing the request; a hard failure (revoked key, write
scope, no readable library) always overwrites the cache so it is never masked
by stale "valid" data.
"""

import logging
import time
from dataclasses import dataclass

from backend.zotero.key_validator import KeyValidation, validate_key
from backend.services.autoindex_key_store import fingerprint

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600


@dataclass
class ZoteroIdentity:
    """A validated, gate-approved Zotero identity resolved from an API key."""

    user_id: int
    username: str
    targets: list[str]


class ZoteroIdentityCache:
    """In-memory TTL cache of KeyValidation results, keyed by key fingerprint."""

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, KeyValidation]] = {}

    async def resolve(self, api_key: str) -> KeyValidation:
        """Return a cached or freshly validated KeyValidation for api_key."""
        fp = fingerprint(api_key)
        now = time.monotonic()
        cached = self._entries.get(fp)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        result = await validate_key(api_key)
        if result.transient and cached is not None:
            logger.warning("zotero.org validation failed transiently; serving stale cache for %s", fp)
            return cached[1]
        if not result.transient:
            self._entries[fp] = (now, result)
        return result

    def clear(self) -> None:
        """Test helper: drop all cached entries."""
        self._entries.clear()


_cache = ZoteroIdentityCache()


def get_identity_cache() -> ZoteroIdentityCache:
    """Return the process-wide identity cache used by the auth middleware."""
    return _cache


def reset_identity_cache() -> None:
    """Test helper: clear the process-wide cache between test cases."""
    _cache.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_zotero_identity.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/zotero_identity.py backend/tests/test_zotero_identity.py
git commit -m "feat: add Zotero-key identity validation cache"
```

---

### Task 3: Access gate (Part 2) + startup fail-closed check + per-library enforcement helper

**Files:**
- Create: `backend/services/access_gate.py`
- Test: `backend/tests/test_access_gate.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for backend.services.access_gate (Part 2 gate, Part 4 loopback
exception, and the per-library enforcement helper used by route handlers)."""

import unittest

from fastapi import HTTPException

from backend.config.settings import Settings
from backend.services.access_gate import (
    assert_can_access,
    assert_safe_to_start,
    is_gate_configured,
    is_loopback,
    passes_gate,
)
from backend.services.zotero_identity import ZoteroIdentity


class LoopbackTest(unittest.TestCase):
    def test_localhost_is_loopback(self):
        self.assertTrue(is_loopback(Settings(api_host="localhost")))

    def test_127_0_0_1_is_loopback(self):
        self.assertTrue(is_loopback(Settings(api_host="127.0.0.1")))

    def test_fqdn_is_not_loopback(self):
        self.assertFalse(is_loopback(Settings(api_host="rag.example.com")))


class GateConfiguredTest(unittest.TestCase):
    def test_unconfigured(self):
        self.assertFalse(is_gate_configured(Settings()))

    def test_group_configured(self):
        self.assertTrue(is_gate_configured(Settings(authorized_group_id=1)))

    def test_allowlist_configured(self):
        self.assertTrue(is_gate_configured(Settings(authorized_user_ids=[1])))


class PassesGateTest(unittest.TestCase):
    def test_group_member_passes(self):
        settings = Settings(authorized_group_id=999)
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1", "groups/999"])
        self.assertTrue(passes_gate(identity, settings))

    def test_non_member_without_allowlist_fails(self):
        settings = Settings(authorized_group_id=999)
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self.assertFalse(passes_gate(identity, settings))

    def test_allowlisted_user_passes_even_without_group_membership(self):
        settings = Settings(authorized_group_id=999, authorized_user_ids=[1])
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self.assertTrue(passes_gate(identity, settings))

    def test_neither_configured_fails_closed(self):
        settings = Settings()
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1", "groups/999"])
        self.assertFalse(passes_gate(identity, settings))


class AssertSafeToStartTest(unittest.TestCase):
    def test_loopback_always_safe(self):
        assert_safe_to_start(Settings(api_host="localhost"))  # must not raise

    def test_remote_without_gate_raises(self):
        with self.assertRaises(RuntimeError):
            assert_safe_to_start(Settings(api_host="rag.example.com"))

    def test_remote_with_group_configured_is_safe(self):
        assert_safe_to_start(Settings(api_host="rag.example.com", authorized_group_id=999))

    def test_remote_with_allowlist_configured_is_safe(self):
        assert_safe_to_start(Settings(api_host="rag.example.com", authorized_user_ids=[1]))


class AssertCanAccessTest(unittest.TestCase):
    def test_none_identity_always_allowed(self):
        assert_can_access(None, "users/1")  # must not raise

    def test_identity_with_target_allowed(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        assert_can_access(identity, "users/1")  # must not raise

    def test_identity_without_target_rejected(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with self.assertRaises(HTTPException) as ctx:
            assert_can_access(identity, "users/2")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_access_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.access_gate'`

- [ ] **Step 3: Implement the module**

```python
"""The Part 2 access gate and Part 4 loopback exception.

zotero_identity.py proves "this is a real Zotero user with these readable
libraries." That is authentication, not authorization to use this instance —
any Zotero account on earth can pass it. This module adds the second check:
is the identity a member of a designated Zotero group, or on an explicit
user_id allowlist? It also holds the per-library enforcement helper used by
route handlers, and the startup check that refuses to run an unguarded
remote instance.
"""

import logging
from typing import Optional

from fastapi import HTTPException

from backend.config.settings import Settings
from backend.services.zotero_identity import ZoteroIdentity

logger = logging.getLogger(__name__)


def is_loopback(settings: Settings) -> bool:
    """True for the single-trusted-local-user deployment mode (Part 4)."""
    return settings.api_host in ("localhost", "127.0.0.1")


def is_gate_configured(settings: Settings) -> bool:
    return bool(settings.authorized_group_id) or bool(settings.authorized_user_ids)


def passes_gate(identity: ZoteroIdentity, settings: Settings) -> bool:
    """True if identity is allowed to use this instance at all (Part 2)."""
    if settings.authorized_group_id and f"groups/{settings.authorized_group_id}" in identity.targets:
        return True
    if settings.authorized_user_ids and identity.user_id in settings.authorized_user_ids:
        return True
    return False


def assert_safe_to_start(settings: Settings) -> None:
    """Refuse to start in remote mode without a configured access gate.

    Loopback deployments are exempt: a single trusted local user needs no
    gate. Remote deployments must configure AUTHORIZED_GROUP_ID and/or
    AUTHORIZED_USER_IDS, or every Zotero user on earth could use the instance.
    """
    if is_loopback(settings):
        return
    if not is_gate_configured(settings):
        raise RuntimeError(
            f"Refusing to start in remote mode (api_host={settings.api_host!r}) "
            "without an access gate: set AUTHORIZED_GROUP_ID and/or "
            "AUTHORIZED_USER_IDS. See docs/history/plan-zotero-key-auth.md Part 2."
        )


def assert_can_access(identity: Optional[ZoteroIdentity], library_id: str) -> None:
    """Raise 403 unless library_id is within identity's readable targets.

    identity=None means the caller is on the loopback no-auth path (Part 4)
    or the transitional legacy-shared-key path (Part 5) — both bypass
    per-library enforcement (loopback because there is only one trusted
    user; legacy because un-migrated plugins predate per-library targets).
    """
    if identity is None:
        return
    if library_id not in identity.targets:
        raise HTTPException(
            status_code=403,
            detail=f"Your Zotero key does not grant read access to library {library_id!r}.",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_access_gate.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/access_gate.py backend/tests/test_access_gate.py
git commit -m "feat: add Part 2 access gate and per-library enforcement helper"
```

---

### Task 4: `resolve_zotero_identity` — the request-level resolver

**Files:**
- Modify: `backend/dependencies.py` (add near the top, after the imports and before `get_client_api_keys`)
- Test: `backend/tests/test_resolve_zotero_identity.py`

This is the function that turns request headers into an `Optional[ZoteroIdentity]`, combining the loopback exception, the legacy-shared-key transitional fallback, the identity cache, and the Part 2 gate. It is tested standalone here (via a throwaway FastAPI app, not the real one) so its own branches are covered in isolation before Task 5 wires it into `backend/main.py`.

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for backend.dependencies.resolve_zotero_identity, tested in
isolation via a throwaway FastAPI app (not backend.main.app)."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.config.settings import get_settings, reset_settings
from backend.dependencies import resolve_zotero_identity
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.key_validator import KeyValidation


def _make_probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(identity=Depends(resolve_zotero_identity)):
        if identity is None:
            return {"identity": None}
        return {"user_id": identity.user_id, "targets": identity.targets}

    return app


class ResolveZoteroIdentityTest(unittest.TestCase):
    def setUp(self):
        reset_settings()
        reset_identity_cache()
        self.client = TestClient(_make_probe_app())

    def tearDown(self):
        reset_settings()
        reset_identity_cache()

    def test_loopback_skips_auth_entirely(self):
        get_settings().api_host = "localhost"
        r = self.client.get("/probe")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["identity"])

    def test_remote_missing_key_rejected(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/probe")
        self.assertEqual(r.status_code, 401)

    def test_remote_valid_gated_key_returns_identity(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user_id"], 1)

    def test_remote_valid_but_ungated_key_rejected(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 403)

    def test_remote_revoked_key_rejected_with_401(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(None, None, read_only=False, reason="revoked", transient=False)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 401)

    def test_remote_zotero_unreachable_no_cache_returns_503(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(None, None, read_only=False, reason="down", transient=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 503)

    def test_legacy_shared_key_accepted_as_transitional_fallback(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        s.api_key = "SHARED"
        r = self.client.get("/probe", headers={"X-API-Key": "SHARED"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["identity"])

    def test_legacy_key_via_query_param_accepted(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        s.api_key = "SHARED"
        r = self.client.get("/probe?api_key=SHARED")
        self.assertEqual(r.status_code, 200)

    def test_wrong_legacy_key_rejected(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        s.api_key = "SHARED"
        r = self.client.get("/probe", headers={"X-API-Key": "WRONG"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_resolve_zotero_identity.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_zotero_identity' from 'backend.dependencies'`

- [ ] **Step 3: Implement the resolver and the state-reading dependency**

In `backend/dependencies.py`, add these imports alongside the existing ones at the top of the file:

```python
from typing import Optional

from fastapi import HTTPException, Request

from backend.services.access_gate import is_loopback, passes_gate
from backend.services.zotero_identity import ZoteroIdentity, get_identity_cache
```

(`Request` is already imported; keep the existing `from fastapi import Request` line and just add `HTTPException` to it instead of duplicating the import.)

Then add the resolver and reader, placed after the imports and before `get_client_api_keys`:

```python
async def resolve_zotero_identity(request: Request) -> Optional[ZoteroIdentity]:
    """Resolve and gate the caller's Zotero identity for the current request.

    Returns None for the loopback no-auth path (Part 4) and for the
    transitional legacy-shared-key path (Part 5) — both skip per-library
    enforcement in access_gate.assert_can_access(). Raises HTTPException:
    401 for a missing/invalid/revoked key, 403 if the Part 2 gate rejects
    an otherwise-valid identity, 503 if zotero.org is unreachable with no
    cached validation to fall back on.

    Used both as a FastAPI dependency (directly, or via get_zotero_identity
    reading back what the api_key_middleware already resolved) and as the
    middleware's own auth check for every /api/* request.
    """
    settings = get_settings()
    if is_loopback(settings):
        return None

    zotero_key = request.headers.get("X-Zotero-API-Key")
    if not zotero_key:
        legacy_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if settings.api_key and legacy_key == settings.api_key:
            logger.warning("Request authenticated via legacy shared API_KEY (transitional path)")
            return None
        raise HTTPException(status_code=401, detail="Missing X-Zotero-API-Key header.")

    validation = await get_identity_cache().resolve(zotero_key)
    if not validation.read_only:
        status_code = 503 if validation.transient else 401
        raise HTTPException(status_code=status_code, detail=validation.reason or "Invalid Zotero API key.")

    identity = ZoteroIdentity(user_id=validation.user_id, username=validation.username, targets=validation.targets)
    if not passes_gate(identity, settings):
        raise HTTPException(status_code=403, detail="This Zotero account is not authorized to use this server.")
    return identity


def get_zotero_identity(request: Request) -> Optional[ZoteroIdentity]:
    """FastAPI dependency: read back the identity api_key_middleware resolved.

    Route handlers should depend on this (not resolve_zotero_identity
    directly) to avoid re-validating the key a second time per request.
    Returns None if the middleware never ran for this path (shouldn't
    happen for any /api/* route) or resolved to the loopback/legacy path.
    """
    return getattr(request.state, "zotero_identity", None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_resolve_zotero_identity.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Run the full existing suite to check for regressions before wiring anything else**

Run: `uv run pytest backend/tests -x -q`
Expected: PASS, same pass count as before this task (no existing test touches `backend/dependencies.py`'s new additions yet)

- [ ] **Step 6: Commit**

```bash
git add backend/dependencies.py backend/tests/test_resolve_zotero_identity.py
git commit -m "feat: add resolve_zotero_identity request resolver"
```

---

### Task 5: Wire the resolver into `backend/main.py` (middleware + fail-closed startup)

**Files:**
- Modify: `backend/main.py:1-21` (imports, startup), `backend/main.py:140-168` (middleware)
- Test: `backend/tests/test_main_auth_middleware.py`

This task makes the new resolver run for **every** `/api/*` path (not just the ones touched later in this plan), replacing the old single-shared-secret check app-wide. Since every existing test uses the default `api_host="localhost"` (loopback), this is a no-op for the whole existing suite — verified in Step 5.

- [ ] **Step 1: Write the failing test**

```python
"""End-to-end tests for the api_key_middleware wiring in backend.main.

Uses the real app (TestClient(app)) and an already-existing, unmodified
route (GET /api/libraries) purely to exercise the middleware's status-code
behavior. Response *content* filtering is covered later once
backend/api/libraries.py itself is wired (see test_libraries_api.py)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.key_validator import KeyValidation


class AuthMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def test_loopback_reaches_handler_without_any_key(self):
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 200)

    def test_remote_without_any_key_is_401(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 401)

    def test_remote_with_gated_zotero_key_reaches_handler(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/api/libraries", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 200)

    def test_remote_with_legacy_shared_key_still_works(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        s.api_key = "SHARED"
        r = self.client.get("/api/libraries", headers={"X-API-Key": "SHARED"})
        self.assertEqual(r.status_code, 200)

    def test_health_and_version_exempt_even_on_remote_host(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/api/version").status_code, 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_main_auth_middleware.py -v`
Expected: FAIL on the remote-host tests — the old middleware 401s on a missing/mismatched `X-API-Key` even when no `api_key` is configured to compare against is a non-issue today (it's a no-op when `settings.api_key` is unset), but the *new* behaviors (rejecting on a bare remote host with no key at all when `api_key` is unset, accepting a valid gated Zotero key) don't exist yet.

- [ ] **Step 3: Replace the middleware and add the startup check**

In `backend/main.py`, change the import line (currently `from fastapi import FastAPI, Request`):

```python
from fastapi import FastAPI, HTTPException, Request
```

Add two imports near the other `backend.*` imports (after `from backend.dependencies import make_vector_store`):

```python
from backend.dependencies import resolve_zotero_identity
from backend.services.access_gate import assert_safe_to_start
```

Immediately after `settings = get_settings()` (currently line 21), add:

```python
assert_safe_to_start(settings)
```

Replace the entire middleware block (currently lines ~140-168, from the `# API key authentication middleware` comment through the end of `api_key_middleware`) with:

```python
# Zotero-key identity middleware
# Resolves and gates the caller's identity for every /api/* request — see
# docs/history/plan-zotero-key-auth.md for the design. Exempt health-check /
# version endpoints so the plugin can discover the backend without needing
# credentials first, and /public/* which is intentionally unauthenticated.
_AUTH_EXEMPT_PATHS = {"/", "/health", "/api/version"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Resolve and gate the caller's Zotero identity for every /api/* request.

    Loopback deployments (api_host in {localhost, 127.0.0.1}) skip this
    entirely (Part 4) — there is exactly one trusted local user. Everywhere
    else the caller must present either a valid, gate-approved Zotero API
    key (X-Zotero-API-Key) or, during the transitional migration window, the
    legacy shared secret (X-API-Key header / ?api_key= query param — the
    latter for SSE endpoints where EventSource cannot set headers). The
    resolved identity (or None for loopback/legacy) is stashed on
    request.state.zotero_identity so downstream route dependencies
    (backend.dependencies.get_zotero_identity) don't re-validate.

    OPTIONS requests (CORS preflight) are always allowed so the browser can
    complete the preflight handshake.
    """
    request.state.zotero_identity = None
    if (
        request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path not in _AUTH_EXEMPT_PATHS
    ):
        try:
            request.state.zotero_identity = await resolve_zotero_identity(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_main_auth_middleware.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full existing suite to confirm no regressions**

Run: `uv run pytest backend/tests -x -q`
Expected: PASS. Every pre-existing test uses the default `api_host="localhost"`, so the new middleware takes the loopback branch exactly as the old middleware did when `api_key` was unset — no behavioral change for the existing suite. (`test_autoindex_api.py`'s `s.api_key = None` line becomes redundant but harmless — the loopback branch is checked first regardless.)

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/tests/test_main_auth_middleware.py
git commit -m "feat: replace shared-secret middleware with Zotero-key identity resolution"
```

---

### Task 6: Enforce access on `POST /api/query`

**Files:**
- Modify: `backend/api/query.py:1-63` (imports, handler signature), `backend/api/query.py:79-83` (enforcement)
- Test: `backend/tests/test_query_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""Endpoint tests for POST /api/query's per-library authorization."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class QueryAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.testing = True
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        """Bypass the middleware/network round-trip: override the
        get_zotero_identity dependency directly, matching how FastAPI's own
        dependency_overrides mechanism is meant to be used in tests."""
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def tearDown_overrides(self):
        app.dependency_overrides.clear()

    def test_loopback_query_with_no_identity_is_unrestricted(self):
        self._set_identity(None)
        try:
            r = self.client.post("/api/query", json={"question": "q?", "library_ids": ["users/1"]})
        finally:
            app.dependency_overrides.clear()
        self.assertNotEqual(r.status_code, 403)

    def test_query_outside_targets_is_403(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self._set_identity(identity)
        try:
            r = self.client.post("/api/query", json={"question": "q?", "library_ids": ["users/2"]})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(r.status_code, 403)

    def test_query_within_targets_is_not_403(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self._set_identity(identity)
        try:
            r = self.client.post("/api/query", json={"question": "q?", "library_ids": ["users/1"]})
        finally:
            app.dependency_overrides.clear()
        self.assertNotEqual(r.status_code, 403)

    def test_query_with_one_target_outside_is_403_even_if_another_is_inside(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self._set_identity(identity)
        try:
            r = self.client.post(
                "/api/query",
                json={"question": "q?", "library_ids": ["users/1", "users/2"]},
            )
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_query_api.py -v`
Expected: FAIL on the two 403 tests (the 403 is never raised today) — `AssertionError: 200 != 403` or similar, depending on what `MockLLMService`/`MockEmbeddingService` do with a nonexistent library (should not itself error, since `s.testing = True` uses mocks).

- [ ] **Step 3: Wire the dependency and enforcement**

In `backend/api/query.py`, change the import line:

```python
from backend.dependencies import get_client_api_keys, get_vector_store, get_zotero_identity, make_embedding_service, make_llm_service
```

Add two new imports:

```python
from backend.services.access_gate import assert_can_access
from backend.services.zotero_identity import ZoteroIdentity
```

Change the handler signature (currently lines 58-63):

```python
@router.post("/query", response_model=QueryResponse)
async def query_libraries(
    query: QueryRequest,
    http_request: Request,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
```

Add the enforcement loop right after the existing "at least one library ID" check (currently lines 79-83):

```python
    if not query.library_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one library ID must be provided"
        )

    for library_id in query.library_ids:
        assert_can_access(identity, library_id)

```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_query_api.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full existing suite to confirm no regressions**

Run: `uv run pytest backend/tests -x -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/api/query.py backend/tests/test_query_api.py
git commit -m "fix: enforce per-library access on POST /api/query"
```

---

### Task 7: Enforce access on `backend/api/libraries.py` (list filtering + delete/mutate endpoints)

**Files:**
- Modify: `backend/api/libraries.py` (imports; `list_libraries` at ~line 80; `clear_library_index` at ~line 152; `sync_library_deletions` at ~line 234; `clear_item_chunks` at ~line 273)
- Test: `backend/tests/test_libraries_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""Endpoint tests for backend.api.libraries authorization:
- GET /api/libraries is filtered to the caller's own targets
- DELETE .../index, POST .../sync-deletions, DELETE .../items/{key}/chunks
  all 403 for a library outside the caller's targets
"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata
from backend.services.registration_service import RegistrationService
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class LibrariesAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.testing = True
        self.client = TestClient(app)

        vector_store = VectorStore(
            storage_path=Path(self.tmp.name) / "qdrant",
            embedding_dim=8,
            embedding_model_name="test-model",
        )
        vector_store.update_library_metadata(
            LibraryIndexMetadata(library_id="users/1", library_type="user", library_name="Mine")
        )
        vector_store.update_library_metadata(
            LibraryIndexMetadata(library_id="users/2", library_type="user", library_name="Someone Else's")
        )
        app.state.vector_store = vector_store

        RegistrationService(s.registrations_path).register("users/1", "Mine", 1, "u")
        RegistrationService(s.registrations_path).register("users/2", "Someone Else's", 2, "other")

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_list_filters_to_own_targets(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 200)
        ids = [lib["library_id"] for lib in r.json()]
        self.assertEqual(ids, ["users/1"])

    def test_list_unrestricted_when_no_identity(self):
        self._set_identity(None)
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 200)
        ids = {lib["library_id"] for lib in r.json()}
        self.assertEqual(ids, {"users/1", "users/2"})

    def test_delete_index_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.delete("/api/libraries/users/2/index")
        self.assertEqual(r.status_code, 403)

    def test_delete_index_within_targets_succeeds(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.delete("/api/libraries/users/1/index")
        self.assertEqual(r.status_code, 200)

    def test_sync_deletions_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post("/api/libraries/users/2/sync-deletions", json={"current_item_keys": []})
        self.assertEqual(r.status_code, 403)

    def test_clear_item_chunks_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.delete("/api/libraries/users/2/items/ITEM1/chunks")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_libraries_api.py -v`
Expected: FAIL — `test_list_filters_to_own_targets` sees both libraries; the three 403 tests get 200 instead.

- [ ] **Step 3: Wire the dependency and enforcement**

In `backend/api/libraries.py`, add to the imports:

```python
from typing import Optional

from backend.dependencies import get_vector_store, get_zotero_identity
from backend.services.access_gate import assert_can_access
from backend.services.zotero_identity import ZoteroIdentity
```

(There is already a `from typing import Optional` line — merge rather than duplicate; there is already `from backend.dependencies import get_vector_store` — merge into a single import line.)

Change `list_libraries` (currently lines 80-103):

```python
@router.get("/libraries", response_model=list[LibraryDetailResponse])
def list_libraries(
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    List libraries known to the backend (indexed or registered), filtered to
    the caller's own readable targets when authenticated via a Zotero key.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    settings = get_settings()
    reg_service = RegistrationService(settings.registrations_path)
    registrations = reg_service.get_all()

    all_metadata = vector_store.get_all_library_metadata()
    metadata_by_id = {m.library_id: m for m in all_metadata}

    # Union of all known library IDs (indexed + registered)
    all_ids = set(metadata_by_id.keys()) | set(registrations.keys())
    if identity is not None:
        all_ids &= set(identity.targets)

    return [
        _build_detail(lid, metadata_by_id.get(lid), registrations.get(lid))
        for lid in sorted(all_ids)
    ]
```

Change `clear_library_index` (currently lines 152-172): add the dependency param and an `assert_can_access` call right after the vector-store-availability check:

```python
@router.delete("/libraries/{library_id}/index")
def clear_library_index(
    library_id: str,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Remove all indexed data for a library (chunks, dedup records, metadata).
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, library_id)

    try:
        chunks_deleted = vector_store.delete_library_chunks(library_id)
        dedup_deleted = vector_store.delete_library_deduplication_records(library_id)
        metadata_deleted = vector_store.delete_library_metadata(library_id)

        return {
            "library_id": library_id,
            "chunks_deleted": chunks_deleted,
            "dedup_deleted": dedup_deleted,
            "metadata_deleted": metadata_deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear library index: {str(e)}")
```

Change `sync_library_deletions` (currently lines 234-270):

```python
@router.post("/libraries/{library_id}/sync-deletions", response_model=SyncDeletionsResponse)
def sync_library_deletions(
    library_id: str,
    request: SyncDeletionsRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Remove chunks for indexed items no longer present in the Zotero library.

    Called by the plugin after it has collected the complete set of current Zotero
    item keys (parent items only, not attachment keys).  Any indexed item whose key
    is absent from *current_item_keys* is treated as deleted and its chunks and
    deduplication records are purged.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, library_id)

    try:
        indexed = vector_store.get_all_indexed_item_versions(library_id)
        current_keys = set(request.current_item_keys)
        orphaned = set(indexed) - current_keys

        deleted_chunks = 0
        for key in orphaned:
            deleted_chunks += vector_store.delete_item_chunks(library_id, key)
            vector_store.delete_item_deduplication_records(library_id, key)

        if orphaned:
            logger.info(
                "sync-deletions: removed %d orphaned item(s) (%d chunks) from library %s",
                len(orphaned),
                deleted_chunks,
                library_id,
            )

        return SyncDeletionsResponse(deleted_items=len(orphaned), deleted_chunks=deleted_chunks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"sync-deletions failed: {str(e)}")
```

Change `clear_item_chunks` (currently lines 273-293):

```python
@router.delete("/libraries/{library_id}/items/{item_key}/chunks")
def clear_item_chunks(
    library_id: str,
    item_key: str,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Remove all indexed chunks for a specific item within a library.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, library_id)

    try:
        chunks_deleted = vector_store.delete_item_chunks(library_id, item_key)
        vector_store.delete_item_deduplication_records(library_id, item_key)
        return {
            "library_id": library_id,
            "item_key": item_key,
            "chunks_deleted": chunks_deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear item chunks: {str(e)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_libraries_api.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full existing suite to confirm no regressions**

Run: `uv run pytest backend/tests -x -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/api/libraries.py backend/tests/test_libraries_api.py
git commit -m "fix: filter GET /api/libraries and enforce access on delete/sync endpoints"
```

---

### Task 8: Enforce access on `backend/api/document_upload.py`, remove self-asserted `user_id`

**Files:**
- Modify: `backend/api/document_upload.py` (imports at ~lines 17-40; delete `_check_registration` at ~lines 247-264; `AbstractIndexRequest` at ~line 239; `upload_and_index_abstract` at ~lines 788-892; `batch_update_metadata` at ~lines 900-950; `_parse_upload_request` at ~lines 958-999; the two call sites at ~lines 594-637 and ~675-693)
- Test: `backend/tests/test_document_upload_authorization.py`

- [ ] **Step 1: Write the failing test**

```python
"""Endpoint tests for backend.api.document_upload authorization:
- batch metadata update, abstract indexing, and file upload all 403 for a
  library outside the caller's targets
- user_id is taken from the validated identity, not the request body
"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class DocumentUploadAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.testing = True
        self.client = TestClient(app)
        app.state.vector_store = VectorStore(
            storage_path=Path(self.tmp.name) / "qdrant",
            embedding_dim=8,
            embedding_model_name="test-model",
        )

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_batch_metadata_update_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/index/items/metadata",
            json={"library_id": "users/2", "items": []},
        )
        self.assertEqual(r.status_code, 403)

    def test_batch_metadata_update_within_targets_succeeds(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/index/items/metadata",
            json={"library_id": "users/1", "items": []},
        )
        self.assertEqual(r.status_code, 200)

    def test_abstract_index_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/index/abstract",
            json={
                "library_id": "users/2",
                "item_key": "ITEM1",
                "abstract_text": "word " * 200,
            },
        )
        self.assertEqual(r.status_code, 403)

    def test_upload_document_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "users/2",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata},
        )
        self.assertEqual(r.status_code, 403)

    def test_upload_document_body_user_id_is_ignored_in_favor_of_identity(self):
        self._set_identity(ZoteroIdentity(user_id=42, username="real", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "users/1",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
            "user_id": 999,  # attacker-supplied — must be ignored
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata},
        )
        self.assertNotEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest backend/tests/test_document_upload_authorization.py -v`
Expected: FAIL on the three 403 tests (200 or a processing error instead of 403); the body-`user_id` test may pass or fail depending on downstream mocks — the important regression check happens in Step 4 after the fix, where we can additionally grep the code to confirm `meta_dict.get("user_id")` is gone from the trust path.

- [ ] **Step 3: Wire enforcement, remove `_check_registration` and the self-asserted `user_id`**

In `backend/api/document_upload.py`, change the import block. Replace:

```python
from backend.dependencies import get_client_api_keys, get_vector_store, make_embedding_service
```

with:

```python
from backend.dependencies import get_client_api_keys, get_vector_store, get_zotero_identity, make_embedding_service
from backend.services.access_gate import assert_can_access
from backend.services.zotero_identity import ZoteroIdentity
```

Remove the now-unused import (it was only used by `_check_registration`, which this task deletes):

```python
from backend.services.registration_service import RegistrationService
```

Remove the `user_id` field from `AbstractIndexRequest` (currently line 239):

```python
class AbstractIndexRequest(BaseModel):
    """Request to index an item via its abstractNote (no attachment file)."""

    library_id: str
    library_type: str = "user"
    item_key: str
    item_version: int = 0
    title: Optional[str] = "Untitled"
    authors: list[str] = []
    year: Optional[int] = None
    item_type: Optional[str] = None
    zotero_modified: str = ""
    abstract_text: str
    library_name: str = ""
```

(i.e. delete the trailing `user_id: Optional[int] = None` line.)

Delete the `_check_registration` function entirely (currently lines 247-264):

```python
def _check_registration(library_id: str, user_id: Optional[int], settings: Settings) -> None:
    """Raise 403 if registration is required and the user is not registered.

    Skipped when api_host is localhost/127.0.0.1 or REQUIRE_REGISTRATION=false.
    """
    if not settings.require_registration:
        return
    if settings.api_host in ("localhost", "127.0.0.1"):
        return
    service = RegistrationService(settings.registrations_path)
    if not service.is_registered(library_id, user_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "Library not registered for this user. "
                "Please update the plugin to the newest version."
            ),
        )
```

In `upload_and_index_abstract` (currently lines 788-806), change the signature and the top of the body:

```python
@router.post(
    "/index/abstract",
    response_model=DocumentUploadResult,
    summary="Index an item via its abstractNote (remote mode, no attachment file)",
)
async def upload_and_index_abstract(
    http_request: Request,
    request: AbstractIndexRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Index a Zotero item using its abstractNote when no attachment file is available.

    The abstract is chunked and embedded directly.  A virtual attachment key
    ``{item_key}:abstract`` is used so the chunks can be tracked independently.
    The abstract must meet the configured minimum word count (MIN_ABSTRACT_WORDS).
    """
    settings = get_settings()
    assert_can_access(identity, request.library_id)
    user_id = identity.user_id if identity else None
    word_count = len(request.abstract_text.split())
    logger.info(
        f"Abstract index: library={request.library_id} user={user_id} "
        f"item={request.item_key} words={word_count}"
    )
```

(The rest of the function body is unchanged — it never referenced `request.user_id` again after the log line.)

In `batch_update_metadata` (currently lines 900-913), add the dependency and enforcement:

```python
@router.post(
    "/index/items/metadata",
    response_model=BatchMetadataUpdateResult,
    summary="Update payload metadata for existing chunks without re-embedding",
)
def batch_update_metadata(
    request: BatchMetadataUpdateRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Update bibliographic metadata fields on existing indexed chunks without re-embedding.

    Used by the plugin after check-indexed returns needs_metadata_update=True for items
    whose schema_version is below the current version (e.g. item_type was added in v3).

    No file bytes are uploaded; the backend calls Qdrant set_payload() directly.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, request.library_id)
```

(The rest of the function body, starting from `updated_items = 0`, is unchanged.)

Change `_parse_upload_request` (currently lines 958-999) to accept and use the identity:

```python
async def _parse_upload_request(file: UploadFile, metadata: str, identity: Optional[ZoteroIdentity]):
    """Parse and validate the common multipart upload fields.

    Returns a tuple of all parsed fields needed by both sync and async endpoints.
    Raises HTTPException 400 on validation errors, 403 if library_id is
    outside identity's readable targets (see access_gate.assert_can_access).
    """
    try:
        meta_dict = json.loads(metadata)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid metadata JSON: {e}")

    required = {"library_id", "item_key", "attachment_key"}
    missing = required - meta_dict.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required metadata fields: {sorted(missing)}",
        )

    library_id: str = meta_dict["library_id"]
    item_key: str = meta_dict["item_key"]
    attachment_key: str = meta_dict["attachment_key"]
    assert_can_access(identity, library_id)
    user_id: Optional[int] = identity.user_id if identity else meta_dict.get("user_id")
    library_type: str = meta_dict.get("library_type", "user")
    mime_type: str = meta_dict.get("mime_type", "application/pdf")
    item_version: int = int(meta_dict.get("item_version", 0))
    attachment_version: int = int(meta_dict.get("attachment_version", 0))
    item_modified: str = meta_dict.get(
        "zotero_modified", datetime.now(timezone.utc).isoformat()
    )

    doc_metadata = DocumentMetadata(
        library_id=library_id,
        item_key=item_key,
        attachment_key=attachment_key,
        title=meta_dict.get("title", "Untitled"),
        authors=meta_dict.get("authors", []),
        year=meta_dict.get("year"),
        item_type=meta_dict.get("item_type"),
    )
```

(Everything after this point in `_parse_upload_request` — reading the file bytes and returning the tuple — is unchanged.)

Update the two call sites. In `upload_and_index_document` (currently lines 594-637 area), add the dependency to the function signature and pass it through:

```python
    file: UploadFile = File(..., description="Raw attachment bytes"),
    metadata: str = Form(
        ...,
        description=(
            "JSON string with fields: library_id, library_type, item_key, "
            "attachment_key, mime_type, item_version, attachment_version, "
            "title, authors (array), year, item_type, "
            "zotero_modified (ISO 8601 string)"
        ),
    ),
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
```

and change the call:

```python
    meta_dict, doc_metadata, library_id, item_key, attachment_key, user_id, \
        library_type, mime_type, item_version, attachment_version, item_modified, \
        file_bytes = await _parse_upload_request(file, metadata, identity)
```

In `upload_and_index_document_async` (currently lines 675-693 area), add the same dependency and pass it through:

```python
async def upload_and_index_document_async(
    http_request: Request,
    file: UploadFile = File(..., description="Raw attachment bytes"),
    metadata: str = Form(...),
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    ...unchanged docstring...
    """
    meta_dict, doc_metadata, library_id, item_key, attachment_key, user_id, \
        library_type, mime_type, item_version, attachment_version, item_modified, \
        file_bytes = await _parse_upload_request(file, metadata, identity)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest backend/tests/test_document_upload_authorization.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full existing suite to confirm no regressions**

Run: `uv run pytest backend/tests -x -q`
Expected: PASS. `backend/tests/test_async_upload.py` and `backend/tests/test_upload_and_batching.py` call the upload endpoints without any identity override, so `get_zotero_identity` returns `None` (loopback default), taking the same no-op path `_check_registration` used to take for loopback — no behavior change for them. Neither file references `user_id` (confirmed via `grep -n "user_id" backend/tests/test_async_upload.py backend/tests/test_upload_and_batching.py` returning no matches), so removing the `AbstractIndexRequest.user_id` field cannot break them.

- [ ] **Step 6: Commit**

```bash
git add backend/api/document_upload.py backend/tests/test_document_upload_authorization.py
git commit -m "fix: enforce per-library access on upload/index endpoints, stop trusting body user_id"
```

---

### Task 9: Document the new environment variables

**Files:**
- Modify: `.env.dist` (insert a new section after "Registration", currently ending around line 53)

- [ ] **Step 1: Add the new section**

Insert after the `# REQUIRE_REGISTRATION=true` line and its blank line (currently line 53-54), before the "Document extraction backend" comment block:

```
# =============================================================================
# Access Control (Zotero-key authentication)
# =============================================================================

# Loopback deployments (API_HOST unset or localhost/127.0.0.1) need none of
# this: there is exactly one trusted local user and no Zotero-key auth is
# required at all.
#
# Every other deployment authenticates each request with the caller's own
# read-only Zotero API key (sent by the plugin as X-Zotero-API-Key) and then
# checks it against an access gate below — a valid Zotero key alone is NOT
# enough, since any Zotero.org account could otherwise use your server.
# At least one of AUTHORIZED_GROUP_ID / AUTHORIZED_USER_IDS is REQUIRED for a
# non-loopback deployment; the server refuses to start without one.

# Zotero group ID that gates access. A caller is authorized if their key
# grants read access to this group (i.e. they are a member of it). Manage
# membership entirely on zotero.org — no redeploy needed to add/remove users.
# AUTHORIZED_GROUP_ID=998877

# Explicit allowlist of Zotero user IDs, in addition to AUTHORIZED_GROUP_ID
# (OR semantics). Comma-separated. Useful as a fallback or for a small fixed
# team without a dedicated Zotero group.
# AUTHORIZED_USER_IDS=39226,123456

# Legacy shared secret, kept only for the transitional migration window while
# users upgrade to a plugin version that sends X-Zotero-API-Key. A request
# presenting this key bypasses per-library authorization entirely (the same
# way ALL requests did before this feature) — do not treat it as a
# long-term deployment mode for a multi-user instance. See
# docs/history/plan-zotero-key-auth.md Part 5.
# API_KEY=your_shared_secret
```

- [ ] **Step 2: Commit**

```bash
git add .env.dist
git commit -m "docs: document AUTHORIZED_GROUP_ID / AUTHORIZED_USER_IDS in .env.dist"
```

---

### Task 10: Full suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend suite**

Run: `uv run pytest backend/tests -q`
Expected: All tests pass, including every test file created in Tasks 1-8.

- [ ] **Step 2: Run the container smoke test if this branch touched `backend/main.py` or `backend/dependencies.py` (it did)**

Run: `uv run pytest -m container -v -s`
Expected: PASS, or SKIPPED if neither podman nor docker is available in the current environment. Per `CLAUDE.md`, this is required after any change to `backend/main.py`/`backend/dependencies.py`.

- [ ] **Step 3: Manually sanity-check the fail-closed startup guard**

Run:
```bash
API_HOST=rag.example.com uv run python -c "from backend.main import app"
```
Expected: raises `RuntimeError: Refusing to start in remote mode (api_host='rag.example.com') without an access gate: ...` — confirming the guard is live for a real process start, not just in unit tests where settings are mutated after import.

Then confirm it starts cleanly with the gate configured:
```bash
API_HOST=rag.example.com AUTHORIZED_GROUP_ID=1 uv run python -c "from backend.main import app; print('OK')"
```
Expected: prints `OK`.

- [ ] **Step 4: No commit for this task** — it is verification-only. If Step 3 reveals a problem, fix it in the relevant earlier task's files and re-commit there (or as a small follow-up commit), then re-run Step 1.

---

## Deliberately deferred to a follow-up plan

- **Plugin setup wizard** (design doc Part 3): sending `X-Zotero-API-Key` from the plugin, the "create a key on zotero.org" UI flow, and the `POST /api/auth/validate` convenience endpoint the wizard would call. Needs a place to test against — do this once this plan's endpoints exist on a real deployment or local server.
- **Migration cutover** (design doc Part 5, steps 3-4): announcing the change, monitoring the "legacy shared API_KEY" warning log to see when it's safe, and removing the legacy fallback branch from `resolve_zotero_identity`. Depends on the plugin wizard shipping first.
- **`GET /api/libraries/{id}/status`, `GET /api/libraries/{id}/index-status`, `POST /api/libraries/{id}/reconcile-count`**: not named in the vulnerability report or the design doc's Part 1.2 table; left unfiltered for now (see the "Explicitly out of scope" note at the top of this plan).
- **`docs/history/master.md` phase-summary entry**: once this plan and its follow-up are both executed, add the short summary + link per `CLAUDE.md`'s "Implementation progress documentation" convention — appropriate once there's a complete phase to summarize, not mid-plan.
