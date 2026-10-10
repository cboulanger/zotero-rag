"""Unit tests for resolve_targets (re-validate + dedup + embedding-key gating)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.autoindex_resolver import is_embedding_key_usable, resolve_targets
from backend.zotero.key_validator import KeyValidation


def _mock_settings(model_type: str = "remote", model_kwargs: Optional[dict] = None) -> MagicMock:
    """A fake get_settings() return value with a controllable embedding model_type
    and model_kwargs, so tests don't depend on whatever preset this machine's
    real .env configures. Defaults model_kwargs to a personal-key preset
    (api_key_env, e.g. KISSKI) when model_type="remote" and not overridden,
    matching the existing tests below that assume per-user key gating applies."""
    settings = MagicMock()
    settings.data_path = Path(tempfile.mkdtemp())  # no user_settings.json: everyone is on the default
    settings.get_default_preset.return_value = settings.get_hardware_preset.return_value
    settings.get_hardware_preset.return_value.name = "default-preset"
    settings.get_hardware_preset.return_value.embedding.model_type = model_type
    if model_kwargs is None:
        model_kwargs = {"api_key_env": "KISSKI_API_KEY"} if model_type == "remote" else {}
    settings.get_hardware_preset.return_value.embedding.model_kwargs = model_kwargs
    return settings


class ResolveTargetsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # All existing tests assume a remote embedding provider (the only
        # config for which per-user embedding-key gating applies); override
        # per-test with another patch.object(...) call for local-preset cases.
        patcher = patch("backend.services.autoindex_resolver.get_settings",
                         return_value=_mock_settings("remote"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutoIndexKeyStore(Path(tmp.name) / "k.json", Fernet.generate_key().decode())

    async def test_dedup_shared_group(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2", "groups/99"], read_only=True)
        fp1 = store.add("KA", v1)
        fp2 = store.add("KB", v2)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        store.set_embedding_key(fp2, "EMB2", "KISSKI_API_KEY")
        # Pre-set one fingerprint to a non-"ok" status so the assertion that
        # resolve refreshes it back to "ok" is meaningful.
        store.set_status(fp1, "stale")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "users/2", "groups/99"})
        self.assertEqual(issues, [])
        self.assertEqual(targets["users/1"]["fingerprint"], fp1)
        self.assertEqual(targets["users/1"]["embedding_key"], "EMB1")
        # Valid keys have their status refreshed/confirmed to "ok" after resolve.
        for meta in store.list_metadata():
            self.assertEqual(meta["last_status"], "ok")

    async def test_backfills_target_names_on_revalidation(self):
        """Keys stored before name/owner capture existed (or whose group was
        renamed) must get target_names/target_owners backfilled on the very
        next cron re-validation, without the user resubmitting their key."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        self.assertEqual(store.get_target_labels(), {})

        v1_refreshed = KeyValidation(
            1, "a", ["users/1", "groups/99"],
            target_names={"groups/99": "Renamed Group"},
            target_owners={"groups/99": 42},
            read_only=True,
        )
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1_refreshed)):
            await resolve_targets(store)
        self.assertEqual(store.get_target_labels(), {"groups/99": ("Renamed Group", 42)})

    async def test_prunes_revoked_key(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        revoked = KeyValidation(1, "a", read_only=False, reason="Key not found (revoked or expired).")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=revoked)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertIn("revoked", issues[0]["reason"].lower())
        self.assertTrue(issues[0]["pruned"])
        self.assertEqual(issues[0]["kind"], "zotero_key")
        self.assertEqual(store.list_metadata(), [])

    async def test_transient_error_keeps_key(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/5"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        transient = KeyValidation(1, "a", read_only=False, reason="Could not reach Zotero API: boom", transient=True)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=transient)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "groups/5"})
        self.assertEqual(len(issues), 1)
        self.assertFalse(issues[0]["pruned"])
        self.assertEqual(issues[0]["kind"], "zotero_key")
        self.assertEqual(len(store.list_metadata()), 1)
        self.assertEqual(store.list_metadata()[0]["last_status"], "transient_error")

    async def test_missing_embedding_key_skips_slug_with_issue(self):
        """A valid Zotero key with no embedding key configured is excluded from targets."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v1)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")
        self.assertFalse(issues[0]["pruned"])

    async def test_invalid_embedding_key_skips_slug_with_issue(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "BADEMB", "KISSKI_API_KEY", status="invalid")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")

    async def test_rate_limited_embedding_key_skips_slug(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        store.set_embedding_key_status(fp1, "rate_limited", rate_limit_until=future)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")

    async def test_expired_rate_limit_allows_slug(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        store.set_embedding_key_status(fp1, "rate_limited", rate_limit_until=past)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertEqual(targets["users/1"]["embedding_key"], "EMB1")
        self.assertEqual(targets["users/1"]["fingerprint"], fp1)
        self.assertEqual(issues, [])

    async def test_unverified_embedding_key_is_allowed(self):
        """An embedding key that hasn't been actively verified yet is usable."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY", status="unverified")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertEqual(targets["users/1"]["embedding_key"], "EMB1")
        self.assertEqual(issues, [])

    async def test_dedup_shared_group_survives_one_blocked_owner(self):
        """A shared slug still resolves via the owner with a valid embedding key,
        even when the other owner's embedding key is blocked."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2", "groups/99"], read_only=True)
        fp1 = store.add("KA", v1)
        fp2 = store.add("KB", v2)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        store.set_embedding_key(fp2, "BADEMB", "KISSKI_API_KEY", status="invalid")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "groups/99"})
        self.assertEqual(targets["groups/99"]["fingerprint"], fp1)
        self.assertNotIn("users/2", targets)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")
        self.assertEqual(issues[0]["fingerprint"], fp2)

    async def test_shared_key_preset_bypasses_personal_key_gating(self):
        """A preset with a shared, admin-set embedding key (e.g. remote-mpcdf,
        which declares shared_api_key_env instead of api_key_env) has no
        per-user key at all — a stale rate-limited status left over from a
        previously-active *personal*-key preset (e.g. KISSKI) must not block
        this user's targets. Regression test for a real bug: switching from
        apple-silicon-kisski (personal key, hit a genuine KISSKI rate limit)
        to remote-mpcdf (shared key, unaffected) still excluded every target
        because resolve_targets only checked model_type == "remote"."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        store.set_embedding_key_status(fp1, "rate_limited", rate_limit_until=future)
        with patch("backend.services.autoindex_resolver.get_settings",
                   return_value=_mock_settings("remote", model_kwargs={
                       "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                       "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                   })), \
             patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertIsNone(targets["users/1"]["embedding_key"])
        self.assertIsNone(targets["users/1"]["embedding_key_name"])
        self.assertEqual(issues, [])

    async def test_local_model_type_bypasses_embedding_key_gating(self):
        """A local (non-remote) embedding preset has no API key at all, so
        per-user gating must not exclude slugs for lacking one."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v1)  # no embedding key configured
        with patch("backend.services.autoindex_resolver.get_settings",
                   return_value=_mock_settings("local")), \
             patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertIsNone(targets["users/1"]["embedding_key"])
        self.assertIsNone(targets["users/1"]["embedding_key_name"])
        self.assertEqual(issues, [])


class IsEmbeddingKeyUsableTest(unittest.TestCase):
    def test_ok_status_is_usable(self):
        self.assertTrue(is_embedding_key_usable("ok", None))

    def test_unverified_status_is_usable(self):
        self.assertTrue(is_embedding_key_usable("unverified", None))

    def test_invalid_status_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable("invalid", None))

    def test_missing_status_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable(None, None))

    def test_rate_limited_within_window_is_not_usable(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertFalse(is_embedding_key_usable("rate_limited", future))

    def test_rate_limited_after_window_is_usable(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(is_embedding_key_usable("rate_limited", past))

    def test_rate_limited_without_timestamp_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable("rate_limited", None))

    def test_unrecognized_status_is_not_usable(self):
        self.assertFalse(is_embedding_key_usable("some-future-status", None))


if __name__ == "__main__":
    unittest.main()


class ResolveByKeyNameTest(unittest.IsolatedAsyncioTestCase):
    """The key used is the one the active preset names; others are kept, not pruned."""

    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutoIndexKeyStore(Path(tmp.name) / "k.json", Fernet.generate_key().decode())

    async def _resolve(self, store, key_env, v):
        settings = _mock_settings("remote", {"api_key_env": key_env})
        with patch("backend.services.autoindex_resolver.get_settings", return_value=settings), \
             patch("backend.services.autoindex_resolver.validate_key", new=AsyncMock(return_value=v)):
            return await resolve_targets(store)

    async def test_picks_the_key_the_active_preset_names(self):
        store = self._store()
        v = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp = store.add("KA", v)
        store.set_embedding_key(fp, "kisski", "KISSKI_API_KEY")
        store.set_embedding_key(fp, "hf", "HF_API_TOKEN")
        targets, issues = await self._resolve(store, "HF_API_TOKEN", v)
        self.assertEqual(targets["users/1"]["embedding_key"], "hf")
        self.assertEqual(targets["users/1"]["embedding_key_name"], "HF_API_TOKEN")
        self.assertEqual(issues, [])

    async def test_a_missing_key_for_the_active_preset_is_reported_and_other_keys_are_kept(self):
        store = self._store()
        v = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp = store.add("KA", v)
        store.set_embedding_key(fp, "kisski", "KISSKI_API_KEY")
        targets, issues = await self._resolve(store, "HF_API_TOKEN", v)
        self.assertEqual(targets, {})
        self.assertIn("HF_API_TOKEN", issues[0]["reason"])
        self.assertEqual(store.get_decrypted_embedding_key(fp, "KISSKI_API_KEY")[1], "kisski")  # kept

    async def test_status_of_another_key_does_not_block_this_one(self):
        store = self._store()
        v = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp = store.add("KA", v)
        store.set_embedding_key(fp, "kisski", "KISSKI_API_KEY", status="invalid")
        store.set_embedding_key(fp, "hf", "HF_API_TOKEN")
        targets, _ = await self._resolve(store, "HF_API_TOKEN", v)
        self.assertIn("users/1", targets)

    async def test_a_shared_key_preset_needs_no_personal_key(self):
        store = self._store()
        v = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v)
        settings = _mock_settings("remote", {"shared_api_key_env": "MPCDF_EMBEDDING_API_KEY"})
        with patch("backend.services.autoindex_resolver.get_settings", return_value=settings), \
             patch("backend.services.autoindex_resolver.validate_key", new=AsyncMock(return_value=v)):
            targets, issues = await resolve_targets(store)
        self.assertIn("users/1", targets)
        self.assertIsNone(targets["users/1"]["embedding_key"])
        self.assertEqual(issues, [])


class ResolvePerUserPresetTest(unittest.IsolatedAsyncioTestCase):
    """Each user is indexed on their own preset: their key name, recorded as the target's preset."""

    async def test_users_on_different_presets_use_their_own_keys(self):
        from backend.config.presets import ensure_default_presets
        from backend.config.settings import get_settings, reset_settings
        from backend.services.user_settings import set_preferred_preset

        reset_settings()
        self.addCleanup(reset_settings)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = get_settings()
        settings.data_path = Path(tmp.name)
        settings.model_preset = "remote-kisski"
        ensure_default_presets(settings.data_path)
        set_preferred_preset(settings.data_path, 2, "runpod")

        store = AutoIndexKeyStore(settings.data_path / "k.json", Fernet.generate_key().decode())
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2"], read_only=True)
        fp1, fp2 = store.add("KA", v1), store.add("KB", v2)
        store.set_embedding_key(fp1, "kisski-key", "KISSKI_API_KEY")
        store.set_embedding_key(fp2, "runpod-key", "RUNPOD_API_KEY")
        store.set_embedding_key(fp2, "other-kisski", "KISSKI_API_KEY")  # kept, not used

        with patch("backend.services.autoindex_resolver.validate_key", new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)

        self.assertEqual(issues, [])
        self.assertEqual((targets["users/1"]["preset_name"], targets["users/1"]["embedding_key"]), ("remote-kisski", "kisski-key"))
        self.assertEqual((targets["users/2"]["preset_name"], targets["users/2"]["embedding_key"]), ("runpod", "runpod-key"))
        self.assertEqual(targets["users/2"]["embedding_key_name"], "RUNPOD_API_KEY")
