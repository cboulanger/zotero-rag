"""Tests for stand-in preset cost declarations."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.config.presets import ensure_default_presets, get_preset
from backend.providers import PROVIDERS, discover_providers
from backend.services.standin import ExhaustedSide, find_standins, preset_cost, quota_error_detail, standin_offer
from backend.tests import fakes


class CostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.data_path = Path(cls._tmp.name)
        ensure_default_presets(cls.data_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def preset(self, name: str):
        return get_preset(name, self.data_path)

    def test_free_provider_presets_are_free(self):
        for name in ("test-remote", "test-session", "test-remote-rerank"):
            self.assertEqual(preset_cost(self.preset(name)), "free", name)

    def test_paid_provider_preset_is_paid(self):
        self.assertEqual(preset_cost(self.preset("test-cloud")), "paid")

    def test_all_local_preset_is_free(self):
        self.assertEqual(preset_cost(self.preset("cpu-only")), "free")

    def test_explicit_cost_overrides_derived(self):
        preset = self.preset("test-cloud").model_copy(update={"cost": "free"})
        self.assertEqual(preset_cost(preset), "free")

    def test_defaults(self):
        preset = self.preset("test-remote")
        self.assertIs(preset.standin, True)
        self.assertEqual(preset.standin_priority, 0)

    def test_every_provider_declares_cost(self):
        discover_providers()
        for provider_id, cls in PROVIDERS.items():
            self.assertIn(cls.cost, ("free", "paid"), provider_id)


class FindStandinsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)
        self.when = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def preset(self, name: str):
        return get_preset(name, self.data_path)

    def settings(self, default: str = "test-remote"):
        return SimpleNamespace(data_path=self.data_path, get_default_preset=lambda: self.preset(default))

    def add_variant(self, base: str, name: str, **extra) -> None:
        data = json.loads((self.data_path / "presets" / f"{base}.json").read_text())
        data.update(name=name, **extra)
        (self.data_path / "presets" / f"{name}.json").write_text(json.dumps(data))

    def find(self, current: str, side: str, provider: str, keys=None, default="test-remote"):
        exhausted = [ExhaustedSide(side, provider, self.when)]
        with patch.object(fakes.session_cls(), "session_status", return_value=None):
            return find_standins(self.settings(default), self.preset(current), exhausted, keys or {}, 1)

    def names(self, standins):
        return [s.preset for s in standins]

    def test_embedding_exhausted_free_before_paid(self):
        names = self.names(self.find("test-remote-rerank", "embedding", "testvendor"))
        self.assertIn("test-session", names)
        self.assertIn("test-cloud", names)
        self.assertLess(names.index("test-session"), names.index("test-cloud"))
        for excluded in ("test-remote-other-llm", "test-remote", "test-remote-rerank"):
            self.assertNotIn(excluded, names)

    def test_llm_exhausted_offers_other_llm_provider(self):
        names = self.names(self.find("test-remote", "llm", "testvendor"))
        self.assertIn("test-remote-other-llm", names)
        self.assertNotIn("test-remote", names)

    def test_standin_false_is_never_offered(self):
        self.add_variant("test-cloud", "cloud-hidden", standin=False)
        self.assertNotIn("cloud-hidden", self.names(self.find("test-remote", "embedding", "testvendor")))

    def test_priority_orders_within_cost(self):
        self.add_variant("test-cloud", "aaa-cloud", standin_priority=0)
        self.add_variant("test-cloud", "zzz-cloud", standin_priority=5)
        names = self.names(self.find("test-remote", "embedding", "testvendor"))
        self.assertLess(names.index("zzz-cloud"), names.index("aaa-cloud"))

    def test_different_embedding_model_never_offered(self):
        data = json.loads((self.data_path / "presets" / "test-cloud.json").read_text())
        data["name"] = "other-model"
        data["embedding"]["model_name"] = "some-org/other-embedder"
        (self.data_path / "presets" / "other-model.json").write_text(json.dumps(data))
        self.assertNotIn("other-model", self.names(self.find("test-remote", "embedding", "testvendor")))

    def test_key_need(self):
        by = {s.preset: s for s in self.find("test-remote", "embedding", "testvendor")}
        self.assertIn("key:TESTCLOUD_API_KEY", by["test-cloud"].needs)
        by = {s.preset: s for s in self.find("test-remote", "embedding", "testvendor", {"TESTCLOUD_API_KEY": "x"})}
        self.assertFalse([n for n in by["test-cloud"].needs if n.startswith("key:")])

    def test_login_need(self):
        exhausted = [ExhaustedSide("embedding", "testvendor", self.when)]
        for status, expected in ((None, ["login:testsession"]), ({"state": "login_required"}, ["login:testsession"]),
                                 ({"state": "ok"}, []), ({"state": "active"}, [])):
            with patch.object(fakes.session_cls(), "session_status", return_value=status):
                result = find_standins(self.settings(), self.preset("test-remote"), exhausted, {}, 1)
            by = {s.preset: s for s in result}
            self.assertEqual([n for n in by["test-session"].needs if n.startswith("login:")], expected, status)

    def test_to_dict(self):
        item = self.find("test-remote", "embedding", "testvendor")[0]
        self.assertEqual(set(item.to_dict()), {"preset", "cost", "label", "needs"})


class QuotaDetailTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)
        self.now = datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc)
        self.settings = SimpleNamespace(
            data_path=self.data_path,
            get_default_preset=lambda: get_preset("test-remote", self.data_path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def detail(self, available_at):
        preset = get_preset("test-remote", self.data_path)
        with patch.object(fakes.session_cls(), "session_status", return_value=None):
            return quota_error_detail(
                self.settings, preset, "embedding", available_at, "raw", {}, 1, now=self.now)

    def test_long_reset_offers_standins(self):
        detail = self.detail(datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(detail["code"], "quota_exhausted")
        self.assertEqual(detail["provider"], "testvendor")
        self.assertEqual(detail["side"], "embedding")
        self.assertEqual(detail["available_at"], "2026-11-16T23:00:00Z")
        self.assertTrue(detail["standins"])

    def test_message_names_provider_and_time(self):
        detail = self.detail(datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(detail["message"], "Test vendor's embedding quota is used up until 23:00 UTC.")

    def test_message_calls_the_llm_side_answering(self):
        preset = get_preset("test-remote", self.data_path)
        with patch.object(fakes.session_cls(), "session_status", return_value=None):
            detail = quota_error_detail(self.settings, preset, "llm",
                                        datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc), "raw", {}, 1, now=self.now)
        self.assertEqual(detail["message"], "Test vendor's answering (LLM) quota is used up until 23:00 UTC.")

    def test_short_reset_returns_none(self):
        self.assertIsNone(self.detail(datetime(2026, 11, 16, 19, 5, tzinfo=timezone.utc)))


class StandinOfferTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)
        self.now = datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc)
        self.settings = SimpleNamespace(
            data_path=self.data_path,
            get_default_preset=lambda: get_preset("test-remote", self.data_path))

    def offer(self, exhausted):
        preset = get_preset("test-remote", self.data_path)
        with patch.object(fakes.session_cls(), "session_status", return_value=None):
            return preset, standin_offer(self.settings, preset, exhausted, {}, 1, now=self.now)

    def test_two_exhausted_sides_list_standins_valid_for_both(self):
        at = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
        exhausted = [ExhaustedSide("embedding", "testvendor", at), ExhaustedSide("llm", "testvendor", at)]
        preset, offer = self.offer(exhausted)
        self.assertTrue(offer["standins"])
        with patch.object(fakes.session_cls(), "session_status", return_value=None):
            expected = [s.to_dict() for s in find_standins(self.settings, preset, exhausted, {}, 1)]
        self.assertEqual(offer["standins"], expected)
        self.assertEqual(offer["code"], "quota_exhausted")

    def test_none_when_nothing_lasts_fifteen_minutes(self):
        soon = datetime(2026, 11, 16, 19, 5, tzinfo=timezone.utc)
        self.assertIsNone(self.offer([ExhaustedSide("embedding", "testvendor", soon)])[1])
        self.assertIsNone(self.offer([])[1])


class StoredStandinTest(unittest.TestCase):
    """Storage and request-scoped resolution of a temporary stand-in."""

    def setUp(self):
        from backend.config.settings import get_settings, reset_settings
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        self.settings.model_preset = "test-remote"
        self.dp = self.settings.data_path

    @staticmethod
    def _ident(user_id):
        from backend.services.zotero_identity import ZoteroIdentity
        return ZoteroIdentity(user_id=user_id, username=f"u{user_id}", targets=[f"users/{user_id}"])

    @staticmethod
    def _in(**kw):
        from datetime import timedelta
        return datetime.now(timezone.utc) + timedelta(**kw)

    def test_active_standin_is_used(self):
        from backend.services.effective_preset import get_user_preset
        from backend.services.user_settings import set_preferred_preset, set_standin
        set_preferred_preset(self.dp, 7, "test-remote-rerank")
        set_standin(self.dp, 7, "test-session", self._in(hours=1), "test-remote-rerank")
        self.assertEqual(get_user_preset(self.settings, self._ident(7), allow_standin=True).name, "test-session")
        self.assertEqual(get_user_preset(self.settings, self._ident(7)).name, "test-remote-rerank")

    def test_expired_standin_is_dropped(self):
        from backend.services.effective_preset import get_user_preset
        from backend.services.user_settings import get_standin, set_preferred_preset, set_standin
        set_preferred_preset(self.dp, 7, "test-remote-rerank")
        set_standin(self.dp, 7, "test-session", self._in(seconds=-1), "test-remote-rerank")
        self.assertEqual(get_user_preset(self.settings, self._ident(7), allow_standin=True).name, "test-remote-rerank")
        self.assertIsNone(get_standin(self.dp, 7))

    def test_incompatible_standin_is_ignored(self):
        from backend.services.effective_preset import get_user_preset
        from backend.services.user_settings import set_standin
        set_standin(self.dp, 7, "test-other-embedding", self._in(hours=1), "test-remote")
        self.assertIsNone(get_user_preset(self.settings, self._ident(7), allow_standin=True))

    def test_loopback_standin(self):
        from backend.services.effective_preset import get_user_preset
        from backend.services.user_settings import get_standin, set_standin
        set_standin(self.dp, None, "test-session", self._in(hours=1), "test-remote")
        self.assertEqual(get_standin(self.dp, None)["preset"], "test-session")
        self.assertEqual(get_user_preset(self.settings, None, allow_standin=True).name, "test-session")
        self.assertIsNone(get_user_preset(self.settings, None))

    def test_clear_standin(self):
        from backend.services.user_settings import clear_standin, get_standin, set_standin
        set_standin(self.dp, 7, "test-session", self._in(hours=1), "test-remote")
        clear_standin(self.dp, 7)
        self.assertIsNone(get_standin(self.dp, 7))

    def test_standin_applies(self):
        from backend.services.effective_preset import standin_applies
        for p in ("/api/query", "/api/config", "/api/config/preset-view", "/api/required-keys"):
            self.assertTrue(standin_applies(p), p)
        for p in ("/api/index/library/1", "/api/index/document", "/api/autoindex/scheduler/run-now",
                  "/api/configuration"):
            self.assertFalse(standin_applies(p), p)

    def test_resolver_ignores_standins(self):
        from backend.services.effective_preset import get_effective_preset
        from backend.services.user_settings import set_preferred_preset, set_standin
        set_preferred_preset(self.dp, 7, "test-remote-rerank")
        set_standin(self.dp, 7, "test-session", self._in(hours=1), "test-remote-rerank")
        self.assertEqual(get_effective_preset(self.settings, self._ident(7)).name, "test-remote-rerank")

    def test_standin_equal_to_default_runs_on_default(self):
        from backend.services.effective_preset import get_user_preset
        from backend.services.user_settings import set_preferred_preset, set_standin
        set_preferred_preset(self.dp, 7, "test-remote-rerank")
        set_standin(self.dp, 7, "test-remote", self._in(hours=1), "test-remote-rerank")
        self.assertIsNone(get_user_preset(self.settings, self._ident(7), allow_standin=True))
        self.assertEqual(get_user_preset(self.settings, self._ident(7)).name, "test-remote-rerank")


if __name__ == "__main__":
    unittest.main()
