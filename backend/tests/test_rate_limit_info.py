"""Tests for get_cached_rate_limits, the usage recorder and the autoindex status rate_limits field."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import backend.services.embeddings as emb
from backend.services.rate_limit_info import get_cached_rate_limits
from backend.services.usage_meters import key_fingerprint, recorder
from backend.tests.test_providers_registry import make_preset

HEADERS = {"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40"}
OPENAI_HEADERS = {"x-ratelimit-limit-requests": "60", "x-ratelimit-remaining-requests": "10"}


def _settings(tmp: Path, model_type: str = "remote", name: str = "remote-kisski", kisski: bool = True):
    preset = make_preset()
    preset.name = name
    if kisski:
        preset.embedding.provider.id = "kisski"
        preset.embedding.model_kwargs = {"api_key_env": "KISSKI_API_KEY"}
    preset.embedding.model_type = model_type
    preset.llm.model_type = "local"
    return SimpleNamespace(data_path=tmp, get_hardware_preset=lambda: preset)


class TestGetCachedRateLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / "system").mkdir()
        recorder.reset()

    def tearDown(self):
        recorder.reset()
        self.tmp.cleanup()

    def _write_cron(self, entry):
        (self.path / "system" / "cron_status.json").write_text(json.dumps({"last_usage": {"embedding": entry}}))

    def test_in_process_hit_yields_meters(self):
        recorder.record("embedding", HEADERS)
        res = get_cached_rate_limits(_settings(self.path))
        self.assertEqual(res["source"], "run")
        (meter,) = res["meters"]
        self.assertEqual((meter["unit"], meter["period"], meter["limit"], meter["remaining"]), ("requests", "hour", 100, 40))
        self.assertEqual(meter["side"], "embedding")

    def test_other_keys_usage_is_not_shown(self):
        recorder.record("embedding", HEADERS, key_fingerprint("someone-else"))
        self.assertIsNone(get_cached_rate_limits(_settings(self.path), {"embedding": key_fingerprint("mine")}))
        self.assertIsNotNone(get_cached_rate_limits(_settings(self.path), {"embedding": key_fingerprint("someone-else")}))

    def test_cron_status_fallback(self):
        self._write_cron({"headers": HEADERS, "at": "2026-10-09T09:00:00+00:00", "preset": "remote-kisski"})
        res = get_cached_rate_limits(_settings(self.path))
        self.assertEqual(res["source"], "cache")
        self.assertEqual(res["as_of"], "2026-10-09T09:00:00+00:00")

    def test_preset_mismatch_ignored(self):
        self._write_cron({"headers": HEADERS, "at": "2026-10-09T09:00:00+00:00", "preset": "remote-mpcdf"})
        self.assertIsNone(get_cached_rate_limits(_settings(self.path)))

    def test_generic_provider_reads_openai_style_headers(self):
        recorder.record("embedding", OPENAI_HEADERS)
        res = get_cached_rate_limits(_settings(self.path, kisski=False))
        (meter,) = res["meters"]
        self.assertEqual((meter["unit"], meter["limit"], meter["remaining"]), ("requests", 60, 10))

    def test_nothing_cached(self):
        self.assertIsNone(get_cached_rate_limits(_settings(self.path)))

    def test_local_embedding_unavailable(self):
        recorder.record("embedding", HEADERS)
        self.assertIsNone(get_cached_rate_limits(_settings(self.path, model_type="local")))

    def test_never_probes(self):
        with patch.object(emb.RemoteEmbeddingService, "probe_rate_limits", new=AsyncMock()) as probe:
            get_cached_rate_limits(_settings(self.path))
        probe.assert_not_called()

    def test_reset_clears_in_process(self):
        recorder.record("embedding", HEADERS)
        recorder.reset()
        self.assertEqual(recorder.latest("embedding"), (None, None))

    def test_record_ignores_responses_without_rate_limit_headers(self):
        recorder.record("embedding", {"content-type": "application/json"})
        self.assertEqual(recorder.latest("embedding"), (None, None))


class TestStatusRateLimits(unittest.TestCase):
    def test_status_includes_rate_limits(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        from backend.config.settings import get_settings, reset_settings
        reset_settings()
        with tempfile.TemporaryDirectory() as tmp:
            get_settings().data_path = Path(tmp)
            c = TestClient(app)
            with patch("backend.api.autoindex.get_cached_rate_limits", return_value=None):
                self.assertEqual(c.get("/api/autoindex/status").json()["rate_limits"], {"available": False})
            cached = {"meters": [{"id": "requests/hour", "side": "embedding", "unit": "requests", "period": "hour",
                                  "limit": 100, "remaining": 40}], "as_of": None, "source": "cache"}
            with patch("backend.api.autoindex.get_cached_rate_limits", return_value=cached):
                rl = c.get("/api/autoindex/status").json()["rate_limits"]
            self.assertTrue(rl["available"])
            self.assertEqual(rl["meters"][0]["remaining"], 40)
            self.assertEqual(rl["source"], "cache")
        reset_settings()


class TestLLMUsageRecording(unittest.IsolatedAsyncioTestCase):
    async def test_openai_side_records_headers_under_the_key_fingerprint(self):
        from unittest.mock import AsyncMock, Mock
        from backend.services.llm import RemoteLLMService
        from backend.tests.test_llm import _raw
        recorder.reset()
        service = RemoteLLMService.__new__(RemoteLLMService)
        service.api_key = "k-1"
        service._resolve_api_key = lambda: "k-1"
        service._record_usage(OPENAI_HEADERS)
        self.assertEqual(recorder.latest("llm", key_fingerprint("k-1"))[0], OPENAI_HEADERS)
        self.assertEqual(recorder.latest("llm", key_fingerprint("other")), (None, None))
        recorder.reset()
