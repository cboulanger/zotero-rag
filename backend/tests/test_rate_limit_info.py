"""Tests for get_cached_rate_limits and the autoindex status rate_limits field."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import backend.services.embeddings as emb
from backend.services.rate_limit_info import get_cached_rate_limits

HEADERS = {"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40"}


def _settings(tmp: Path, model_type: str = "remote", preset: str = "remote-kisski"):
    return SimpleNamespace(
        data_path=tmp,
        get_hardware_preset=lambda: SimpleNamespace(
            name=preset, embedding=SimpleNamespace(model_type=model_type)
        ),
    )


class TestGetCachedRateLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / "system").mkdir()
        emb.reset_rate_limit_cache()

    def tearDown(self):
        emb.reset_rate_limit_cache()
        self.tmp.cleanup()

    def _write_cron(self, **extra):
        data = {"last_rate_limit_headers": HEADERS, **extra}
        (self.path / "system" / "cron_status.json").write_text(json.dumps(data))

    def test_in_process_hit(self):
        emb._last_rate_limit_headers = dict(HEADERS)
        emb._last_rate_limit_headers_at = "2026-10-09T10:00:00+00:00"
        res = get_cached_rate_limits(_settings(self.path))
        self.assertEqual(res["source"], "run")
        self.assertEqual(res["limits"], HEADERS)
        self.assertEqual(res["as_of"], "2026-10-09T10:00:00+00:00")

    def test_cron_status_fallback(self):
        self._write_cron(last_rate_limit_headers_at="2026-10-09T09:00:00+00:00",
                         last_rate_limit_preset="remote-kisski")
        res = get_cached_rate_limits(_settings(self.path))
        self.assertEqual(res["source"], "cache")
        self.assertEqual(res["as_of"], "2026-10-09T09:00:00+00:00")

    def test_cron_status_without_timestamp(self):
        self._write_cron()
        res = get_cached_rate_limits(_settings(self.path))
        self.assertIsNone(res["as_of"])

    def test_preset_mismatch_ignored(self):
        self._write_cron(last_rate_limit_preset="remote-mpcdf")
        self.assertIsNone(get_cached_rate_limits(_settings(self.path)))

    def test_nothing_cached(self):
        self.assertIsNone(get_cached_rate_limits(_settings(self.path)))

    def test_local_embedding_unavailable(self):
        emb._last_rate_limit_headers = dict(HEADERS)
        self.assertIsNone(get_cached_rate_limits(_settings(self.path, model_type="local")))

    def test_never_probes(self):
        with patch.object(emb.RemoteEmbeddingService, "probe_rate_limits", new=AsyncMock()) as probe:
            get_cached_rate_limits(_settings(self.path))
        probe.assert_not_called()

    def test_reset_clears_in_process(self):
        emb._last_rate_limit_headers = dict(HEADERS)
        emb.reset_rate_limit_cache()
        self.assertEqual(emb.get_last_rate_limit_snapshot(), (None, None))


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
            cached = {"limits": HEADERS, "as_of": None, "source": "cache"}
            with patch("backend.api.autoindex.get_cached_rate_limits", return_value=cached):
                rl = c.get("/api/autoindex/status").json()["rate_limits"]
            self.assertTrue(rl["available"])
            self.assertEqual(rl["limits"], HEADERS)
            self.assertEqual(rl["source"], "cache")
        reset_settings()
