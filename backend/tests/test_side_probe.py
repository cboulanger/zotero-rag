"""backend/services/side_probe.py: cached test requests and quota meters of a side."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend.services import side_probe
from backend.services.embeddings import EmbeddingRateLimitExhaustedError
from backend.services.usage_meters import recorder


class _FakeLLM:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    async def generate(self, prompt, max_tokens=1):
        self.calls += 1
        if self.error:
            raise self.error
        return "OK"


class SideProbeTest(unittest.TestCase):
    def setUp(self):
        side_probe.reset()
        recorder.reset()
        self.addCleanup(side_probe.reset)
        self.addCleanup(recorder.reset)

    def _run(self, service, send=True, fingerprint="fp1", preset=None):
        with patch.object(side_probe, "_service", return_value=(service, fingerprint)):
            return asyncio.run(side_probe.probe_side(preset, "llm", {}, send=send))

    def test_success_is_cached_so_polling_spends_no_quota(self):
        llm = _FakeLLM()
        self.assertTrue(self._run(llm).ok)
        self.assertTrue(self._run(llm).ok)
        self.assertEqual(llm.calls, 1)

    def test_no_request_without_send(self):
        llm = _FakeLLM()
        result = self._run(llm, send=False)
        self.assertIsNone(result.ok)
        self.assertEqual(llm.calls, 0)

    def test_a_rate_limit_is_reported_as_exhausted_quota(self):
        at = datetime(2026, 11, 15, 21, 0, tzinfo=timezone.utc)
        llm = _FakeLLM(EmbeddingRateLimitExhaustedError("limit", available_at=at))
        result = self._run(llm)
        self.assertFalse(result.ok)
        self.assertTrue(result.rate_limited)
        self.assertIn("21:00", result.detail)

    def test_the_named_model_is_tested_and_cached_separately(self):
        llm = _FakeLLM()
        with patch.object(side_probe, "_service", return_value=(llm, "fp1")) as service:
            first = asyncio.run(side_probe.probe_side(None, "llm", {}, send=True, model="gemma-4-31b-it"))
            asyncio.run(side_probe.probe_side(None, "llm", {}, send=True, model="gemma-4-31b-it"))
            asyncio.run(side_probe.probe_side(None, "llm", {}, send=True, model="qwen3-30b-a3b-instruct-2507"))
        self.assertEqual(service.call_args_list[0].args[2], "gemma-4-31b-it")
        self.assertEqual(first.model, "gemma-4-31b-it")
        self.assertEqual(llm.calls, 2)  # one per model; the repeat came from the cache

    def test_meters_come_from_the_callers_own_key_only(self):
        recorder.record("llm", {"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "3"}, "other")
        with patch.object(side_probe, "meters_from_headers", side_effect=lambda p, s, h, **kw: [h] if h else []):
            self.assertEqual(self._run(_FakeLLM(), send=False, fingerprint="fp1").meters, [])
            self.assertEqual(self._run(_FakeLLM(), send=False, fingerprint=None).meters, [])
            self.assertEqual(len(self._run(_FakeLLM(), send=False, fingerprint="other").meters), 1)


if __name__ == "__main__":
    unittest.main()
