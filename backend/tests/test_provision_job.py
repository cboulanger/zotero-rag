"""Tests for the provisioning job service and /api/config/provision endpoints."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.services import provisioning
from backend.services.admin_settings_store import get_remote_config_value
from backend.services.zotero_identity import ZoteroIdentity
from backend.zotero.group_roles import reset_admin_role_cache
from backend.services.zotero_identity import reset_identity_cache


def _fake_proc(returncode, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class ParseResultTest(unittest.TestCase):
    def test_parses_line_amid_other_output(self):
        out = 'hello\nPROVISION_RESULT: {"A": "x"}\nbye\n'
        self.assertEqual(provisioning.parse_result(out), {"A": "x"})

    def test_missing_or_malformed_is_none(self):
        self.assertIsNone(provisioning.parse_result("nothing"))
        self.assertIsNone(provisioning.parse_result("PROVISION_RESULT: {oops"))
        self.assertIsNone(provisioning.parse_result('PROVISION_RESULT: {"A": 1}'))


class AwaitJobTest(unittest.TestCase):
    def setUp(self):
        provisioning.reset_job_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        provisioning.reset_job_state()

    def test_success_applies_remote_config(self):
        provisioning.mark_running()
        proc = _fake_proc(0, b'PROVISION_RESULT: {"RUNPOD_LLM_BASE_URL": "https://u/v1"}\n')
        asyncio.run(provisioning.await_job(proc, self.data_path))
        self.assertEqual(provisioning.get_job_state()["status"], "succeeded")
        self.assertEqual(get_remote_config_value(self.data_path, "RUNPOD_LLM_BASE_URL"), "https://u/v1")

    def test_nonzero_exit_fails_with_stderr_tail(self):
        provisioning.mark_running()
        asyncio.run(provisioning.await_job(_fake_proc(1, b"", b"boom"), self.data_path))
        state = provisioning.get_job_state()
        self.assertEqual(state["status"], "failed")
        self.assertIn("boom", state["message"])

    def test_zero_exit_without_result_line_fails(self):
        provisioning.mark_running()
        asyncio.run(provisioning.await_job(_fake_proc(0, b"no result"), self.data_path))
        self.assertEqual(provisioning.get_job_state()["status"], "failed")


class ProvisionEndpointsTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        from fastapi.testclient import TestClient
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        provisioning.reset_job_state()
        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        ensure_default_presets(s.data_path)
        s.model_preset = "runpod"
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.tmp.cleanup()
        provisioning.reset_job_state()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _override_admin(self):
        from backend.dependencies import require_authorized_group_admin
        identity = ZoteroIdentity(user_id=1, username="admin", targets=["users/1"])
        self.app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_requires_admin(self):
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post("/api/config/provision", headers={"X-Zotero-API-Key": "K"})
        self.assertEqual(r.status_code, 403)

    def test_400_when_preset_has_no_script(self):
        self._override_admin()
        get_settings().model_preset = "remote-kisski"
        r = self.client.post("/api/config/provision")
        self.assertEqual(r.status_code, 400)

    def test_409_when_already_running(self):
        self._override_admin()
        provisioning.mark_running()
        r = self.client.post("/api/config/provision")
        self.assertEqual(r.status_code, 409)

    def test_success_flow_applies_config_and_reports_status(self):
        self._override_admin()
        proc = _fake_proc(0, b'PROVISION_RESULT: {"RUNPOD_EMBEDDING_BASE_URL": "https://e/v1"}\n')
        with patch("backend.services.provisioning.start_job", new=AsyncMock(return_value=proc)) as start:
            r = self.client.post("/api/config/provision")
            self.assertEqual(r.status_code, 202)
            start.assert_awaited_once_with("scripts/provision_runpod_endpoints.py")
        # TestClient runs the background task on its portal loop; poll briefly.
        import time
        for _ in range(50):
            status = self.client.get("/api/config/provision/status").json()
            if status["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(
            get_remote_config_value(get_settings().data_path, "RUNPOD_EMBEDDING_BASE_URL"), "https://e/v1"
        )

    def test_start_failure_marks_failed(self):
        self._override_admin()
        with patch("backend.services.provisioning.start_job", new=AsyncMock(side_effect=OSError("no uv"))):
            r = self.client.post("/api/config/provision")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["status"], "failed")


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

    def test_null_for_preset_without_health_check(self):
        get_settings().model_preset = "remote-kisski"
        r = self.client.get("/api/config/health")
        self.assertEqual(r.json(), {"embedding": None, "llm": None})
        self.assertFalse(self.client.get("/api/config").json()["provisionable"])

    def test_runpod_reports_statuses(self):
        get_settings().model_preset = "runpod"
        from backend.services.admin_settings_store import update_remote_config
        update_remote_config(get_settings().data_path, {
            "RUNPOD_API_KEY": "k",
            "RUNPOD_EMBEDDING_BASE_URL": "https://api.runpod.ai/v2/e1/openai/v1",
            "RUNPOD_LLM_BASE_URL": "https://api.runpod.ai/v2/l1/openai/v1",
        })
        def fake(base_url, api_key):
            return {"status": "ready" if "e1" in base_url else "cold", "detail": "d"}
        with patch.dict("backend.api.config.HEALTH_CHECKS", {"runpod": fake}):
            r = self.client.get("/api/config/health")
        self.assertEqual(r.json()["embedding"]["status"], "ready")
        self.assertEqual(r.json()["llm"]["status"], "cold")
        self.assertTrue(self.client.get("/api/config").json()["provisionable"])

    def test_unconfigured_side_is_unreachable_not_null(self):
        get_settings().model_preset = "runpod"
        with patch.dict("os.environ", {}, clear=True):
            r = self.client.get("/api/config/health")
        self.assertEqual(r.json()["embedding"], {"status": "unreachable", "detail": "not configured"})


if __name__ == "__main__":
    unittest.main()
