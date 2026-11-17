"""GET /api/config/health for the remote-mpcdf preset: the caller's own session endpoints."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.plugins.mpcdf.tests.test_sessions import FakeSessions


class HealthEndpointTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        from fastapi.testclient import TestClient
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        ensure_default_presets(s.data_path)
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_mpcdf_reports_the_callers_endpoint_state(self):
        get_settings().model_preset = "remote-mpcdf"
        from backend.plugins.mpcdf import sessions as mpcdf_sessions
        from backend.plugins.mpcdf.sessions import EndpointCredentials

        fake = FakeSessions()
        fake.creds[(None, "zotero-rag-embedding")] = EndpointCredentials("https://job1/v1", "k", "m")
        with patch.object(mpcdf_sessions, "available", return_value=True), \
             patch.object(mpcdf_sessions, "get_sessions", return_value=fake):
            r = self.client.get("/api/config/health").json()
        self.assertEqual(r["embedding"]["status"], "ready")
        self.assertEqual(r["llm"], {"status": "unreachable", "detail": "not running", "waking": False})

    def test_a_side_whose_start_job_runs_is_reported_as_starting(self):
        get_settings().model_preset = "remote-mpcdf"
        from backend.plugins.mpcdf import sessions as mpcdf_sessions
        from backend.services import provisioning

        provisioning.reset_job_state()
        self.addCleanup(provisioning.reset_job_state)
        provisioning.mark_running(["rerank"], provisioning.user_slot(None))
        with patch.object(mpcdf_sessions, "available", return_value=True), \
             patch.object(mpcdf_sessions, "get_sessions", return_value=FakeSessions()):
            r = self.client.get("/api/config/health").json()
        self.assertEqual(r["rerank"], {"status": "cold", "detail": "starting", "waking": True})
        self.assertEqual(r["llm"], {"status": "unreachable", "detail": "not running", "waking": False})


if __name__ == "__main__":
    unittest.main()
