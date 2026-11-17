"""Stand-in endpoints: PUT/DELETE /api/config/my-preset/standin and the GET /api/config field."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.config.presets import ensure_default_presets, get_preset
from backend.config.settings import get_settings, reset_settings
from backend.services.usage_meters import key_fingerprint, recorder
from backend.services.user_settings import get_standin, set_preferred_preset, set_standin
from backend.services.zotero_identity import ZoteroIdentity
from backend.tests import fakes

KEY = "vendor-test-key"
HEADERS = {"X-Testvendor-Api-Key": KEY}
URL = "/api/config/my-preset/standin"


def identity(user_id):
    return ZoteroIdentity(user_id=user_id, username=f"u{user_id}", targets=[f"users/{user_id}"])


class StandinApiTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        reset_settings()
        recorder.reset()
        self.addCleanup(recorder.reset)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        self.settings.model_preset = "test-remote"
        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.until = datetime.now(timezone.utc) + timedelta(hours=3)
        patcher = patch.object(fakes.session_cls(), "session_status", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def exhaust(self, side="embedding", key=KEY):
        recorder.mark_rate_limited(side, self.until, key_fingerprint(key))

    def call(self, method, path=URL, user_id=None, headers=None, **kw):
        h = {"X-Zotero-API-Key": "Z", **HEADERS, **(headers or {})}
        if user_id is None:
            return self.client.request(method, path, headers=h, **kw)
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity(user_id))):
            return self.client.request(method, path, headers=h, **kw)

    def test_put_standin_stores_with_server_until(self):
        self.exhaust()
        r = self.call("PUT", json={"preset_name": "test-session"})
        self.assertEqual(r.status_code, 200, r.text)
        stored = get_standin(self.settings.data_path, None)
        self.assertEqual((stored["preset"], stored["replaces"]), ("test-session", "test-remote"))
        self.assertEqual(datetime.fromisoformat(stored["until"]), self.until)
        cfg = self.call("GET", "/api/config").json()
        self.assertEqual(cfg["preset_name"], "test-session")
        self.assertEqual(cfg["standin"]["preset"], "test-session")
        self.assertEqual(cfg["standin"]["replaces"], "test-remote")

    def test_put_standin_for_an_identity(self):
        self.exhaust()
        r = self.call("PUT", user_id=7, json={"preset_name": "test-session"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(get_standin(self.settings.data_path, 7)["preset"], "test-session")
        self.assertIsNone(get_standin(self.settings.data_path, None))
        self.assertIsNone(self.call("GET", "/api/config", user_id=8).json()["standin"])

    def test_put_standin_without_exhausted_quota_is_409(self):
        r = self.call("PUT", json={"preset_name": "test-session"})
        self.assertEqual(r.status_code, 409, r.text)
        self.until = datetime.now(timezone.utc) + timedelta(minutes=5)  # below the 15 minute floor
        self.exhaust()
        self.assertEqual(self.call("PUT", json={"preset_name": "test-session"}).status_code, 409)

    def test_put_standin_not_a_standin_is_409(self):
        self.exhaust()
        r = self.call("PUT", json={"preset_name": "test-remote-other-llm"})  # same exhausted embedding provider
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNone(get_standin(self.settings.data_path, None))

    def test_a_limit_recorded_for_another_key_does_not_count(self):
        self.exhaust(key="someone-elses-key")
        self.assertEqual(self.call("PUT", json={"preset_name": "test-session"}).status_code, 409)

    def test_get_standins_returns_the_offer(self):
        self.exhaust()
        offer = self.call("GET", "/api/config/standins").json()["offer"]
        self.assertEqual((offer["code"], offer["side"], offer["provider"]), ("quota_exhausted", "embedding", "testvendor"))
        self.assertTrue(any(s["preset"] == "test-session" for s in offer["standins"]))

    def test_get_standins_is_null_without_a_long_exhaustion_or_for_another_key(self):
        self.assertEqual(self.call("GET", "/api/config/standins").json(), {"offer": None})
        self.exhaust(key="someone-elses-key")
        self.assertEqual(self.call("GET", "/api/config/standins").json(), {"offer": None})
        self.until = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.exhaust()
        self.assertEqual(self.call("GET", "/api/config/standins").json(), {"offer": None})

    def test_get_standins_is_null_while_a_standin_is_active(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "test-session"})
        self.assertEqual(self.call("GET", "/api/config/standins").json(), {"offer": None})

    def test_delete_standin_ends_it(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "test-session"})
        self.assertEqual(self.call("DELETE").status_code, 200)
        self.assertIsNone(get_standin(self.settings.data_path, None))
        self.assertEqual(self.call("GET", "/api/config").json()["preset_name"], "test-remote")

    def test_choosing_a_preset_clears_the_standin(self):
        self.exhaust()
        self.call("PUT", user_id=7, json={"preset_name": "test-session"})
        r = self.call("PUT", "/api/config/my-preset", user_id=7,
                      headers={"X-Testcloud-Api-Key": "tck_x"}, json={"preset_name": "test-cloud"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(get_standin(self.settings.data_path, 7))

    def test_changing_server_default_clears_loopback_standin(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "test-session"})
        self.assertIsNotNone(get_standin(self.settings.data_path, None))
        r = self.call("POST", "/api/config", json={"preset_name": "test-cloud"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(get_standin(self.settings.data_path, None))

    def test_fell_back_choice_ignores_an_active_standin(self):
        from backend.api.config import _fell_back_choice
        d = self.settings.data_path
        set_preferred_preset(d, 7, "test-cloud")
        set_standin(d, 7, "test-session", self.until, "test-cloud")
        session = get_preset("test-session", d)
        self.assertIsNone(_fell_back_choice(self.settings, identity(7), session))
        # without a stand-in the same mismatch is a real fall-back
        set_standin(d, 7, "test-session", datetime.now(timezone.utc) - timedelta(minutes=1), "test-cloud")
        self.assertEqual(_fell_back_choice(self.settings, identity(7), session), "test-cloud")

    def test_switching_between_standins_keeps_the_own_keys(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "test-session"})
        # The client now only sends what /api/required-keys lists for the active stand-in.
        r = self.client.get("/api/required-keys", headers={"X-Zotero-API-Key": "Z"})
        names = [k["key_name"] for k in r.json()["keys"]]
        self.assertIn("TESTVENDOR_API_KEY", names)
        r = self.client.put(URL, headers={"X-Zotero-API-Key": "Z", **HEADERS}, json={"preset_name": "test-cloud"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(get_standin(self.settings.data_path, None)["preset"], "test-cloud")

    def test_standin_equal_to_server_default_is_effective(self):
        d = self.settings.data_path
        set_preferred_preset(d, 7, "test-remote-rerank")
        set_standin(d, 7, "test-remote", self.until, "test-remote-rerank")
        cfg = self.call("GET", "/api/config", user_id=7).json()
        self.assertEqual(cfg["preset_name"], "test-remote")
        self.assertEqual(cfg["standin"]["preset"], "test-remote")

    def test_unusable_standin_is_not_reported(self):
        set_standin(self.settings.data_path, None, "no-such-preset", self.until, "test-remote")
        cfg = self.call("GET", "/api/config").json()
        self.assertEqual(cfg["preset_name"], "test-remote")
        self.assertIsNone(cfg["standin"])

    def test_index_route_ignores_standin(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "test-session"})
        router = self.app.router
        before = len(router.routes)

        def probe():
            return {"preset": get_settings().get_hardware_preset().name}

        self.app.add_api_route("/api/config/standin-probe", probe, methods=["GET"])
        self.app.add_api_route("/api/index-probe", probe, methods=["GET"])
        self.addCleanup(lambda: [router.routes.pop() for _ in range(len(router.routes) - before)])
        self.assertEqual(self.call("GET", "/api/config/standin-probe").json()["preset"], "test-session")
        self.assertEqual(self.call("GET", "/api/index-probe").json()["preset"], "test-remote")


if __name__ == "__main__":
    unittest.main()
