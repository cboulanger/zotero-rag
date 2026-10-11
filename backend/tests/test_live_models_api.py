"""GET /api/config and GET /api/models/status use the LLM provider's live model list."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.providers.kisski import KisskiProvider
from backend.tests.provider_fakes import FakeHTTP, FakeResponse

MODELS = {"data": [
    # busy/free are entries of the bundled remote-kisski preset's model_names;
    # unlisted-model is suitable for RAG but not named there, so the preset filters it out.
    {"id": "qwen3.5-122b-a10b", "input": ["text"], "output": ["text"], "demand": 7},
    {"id": "mistral-large-3-675b-instruct-2512", "input": ["text"], "output": ["text"], "demand": 0},
    {"id": "unlisted-model", "input": ["text"], "output": ["text"], "demand": 0},
    {"id": "some-coder", "input": ["text"], "output": ["text"], "demand": 0},
]}
BUSY, FREE = "qwen3.5-122b-a10b", "mistral-large-3-675b-instruct-2512"


class _Base(unittest.TestCase):
    PRESET = "remote-kisski"

    def setUp(self):
        from backend.main import app
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        ensure_default_presets(s.data_path)
        s.model_preset = self.PRESET
        self.client = TestClient(app)
        self.fake = FakeHTTP({("POST", r"/models$"): FakeResponse(200, MODELS)})
        # Every KisskiProvider instance created while serving a request uses the fake.
        original_init = KisskiProvider.__init__
        fake = self.fake

        def init(this, *a, **kw):
            original_init(this, *a, **kw)
            this._http_client = fake

        self._patch = patch.object(KisskiProvider, "__init__", init)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()


class TestConfigLiveModels(_Base):
    def test_live_models_are_filtered_by_model_names_in_their_order(self):
        # The first model_names entry is the default, whatever the provider's demand order.
        r = self.client.get("/api/config", headers={"X-Kisski-Api-Key": "k"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["llm_models"], [FREE, BUSY])
        self.assertEqual(r.json()["llm_model"], FREE)

    def test_a_filter_matching_nothing_falls_back_to_the_static_list(self):
        self.fake.add("POST", r"/models$", FakeResponse(200, {"data": [
            {"id": "other-model", "input": ["text"], "output": ["text"], "demand": 0}]}))
        r = self.client.get("/api/config", headers={"X-Kisski-Api-Key": "k"})
        self.assertIn(FREE, r.json()["llm_models"])
        self.assertNotIn("other-model", r.json()["llm_models"])

    def test_without_a_key_the_static_list_is_used_and_nothing_is_fetched(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("KISSKI_API_KEY", None)
            r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn("mistral-large-3-675b-instruct-2512", r.json()["llm_models"])
        self.assertEqual(self.fake.calls, [])

    def test_a_failing_model_endpoint_falls_back_to_the_static_list(self):
        self.fake.add("POST", r"/models$", FakeResponse(500, {"error": "down"}))
        r = self.client.get("/api/config", headers={"X-Kisski-Api-Key": "k"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("mistral-large-3-675b-instruct-2512", r.json()["llm_models"])


class TestModelsStatus(_Base):
    def test_demand_and_labels(self):
        r = self.client.get("/api/models/status", headers={"X-Kisski-Api-Key": "k"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["models"], [
            {"model": FREE, "demand": 0, "status": "available"},
            {"model": BUSY, "demand": 7, "status": "very busy"},
        ])

    def test_empty_when_the_endpoint_fails(self):
        self.fake.add("POST", r"/models$", ConnectionError("down"))
        r = self.client.get("/api/models/status", headers={"X-Kisski-Api-Key": "k"})
        self.assertEqual(r.json(), {"models": []})


class TestPresetWithoutLiveModels(_Base):
    PRESET = "remote-mpcdf"

    def test_models_status_is_empty_and_nothing_is_fetched(self):
        r = self.client.get("/api/models/status")
        self.assertEqual(r.json(), {"models": []})
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
