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

KEY = "kisski-test-key"
HEADERS = {"X-Kisski-Api-Key": KEY}
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
        self.settings.model_preset = "remote-kisski"
        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.until = datetime.now(timezone.utc) + timedelta(hours=3)
        patcher = patch("backend.providers.mpcdf.MpcdfProvider.session_status", return_value=None)
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
        r = self.call("PUT", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 200, r.text)
        stored = get_standin(self.settings.data_path, None)
        self.assertEqual((stored["preset"], stored["replaces"]), ("remote-mpcdf", "remote-kisski"))
        self.assertEqual(datetime.fromisoformat(stored["until"]), self.until)
        cfg = self.call("GET", "/api/config").json()
        self.assertEqual(cfg["preset_name"], "remote-mpcdf")
        self.assertEqual(cfg["standin"]["preset"], "remote-mpcdf")
        self.assertEqual(cfg["standin"]["replaces"], "remote-kisski")

    def test_put_standin_for_an_identity(self):
        self.exhaust()
        r = self.call("PUT", user_id=7, json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(get_standin(self.settings.data_path, 7)["preset"], "remote-mpcdf")
        self.assertIsNone(get_standin(self.settings.data_path, None))
        self.assertIsNone(self.call("GET", "/api/config", user_id=8).json()["standin"])

    def test_put_standin_without_exhausted_quota_is_409(self):
        r = self.call("PUT", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 409, r.text)
        self.until = datetime.now(timezone.utc) + timedelta(minutes=5)  # below the 15 minute floor
        self.exhaust()
        self.assertEqual(self.call("PUT", json={"preset_name": "remote-mpcdf"}).status_code, 409)

    def test_put_standin_not_a_standin_is_409(self):
        self.exhaust()
        r = self.call("PUT", json={"preset_name": "kisski-gemini"})  # same exhausted embedding provider
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNone(get_standin(self.settings.data_path, None))

    def test_a_limit_recorded_for_another_key_does_not_count(self):
        self.exhaust(key="someone-elses-key")
        self.assertEqual(self.call("PUT", json={"preset_name": "remote-mpcdf"}).status_code, 409)

    def test_delete_standin_ends_it(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(self.call("DELETE").status_code, 200)
        self.assertIsNone(get_standin(self.settings.data_path, None))
        self.assertEqual(self.call("GET", "/api/config").json()["preset_name"], "remote-kisski")

    def test_choosing_a_preset_clears_the_standin(self):
        self.exhaust()
        self.call("PUT", user_id=7, json={"preset_name": "remote-mpcdf"})
        r = self.call("PUT", "/api/config/my-preset", user_id=7,
                      headers={"X-Runpod-Api-Key": "rpa_x"}, json={"preset_name": "runpod"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(get_standin(self.settings.data_path, 7))

    def test_changing_server_default_clears_loopback_standin(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "remote-mpcdf"})
        self.assertIsNotNone(get_standin(self.settings.data_path, None))
        r = self.call("POST", "/api/config", json={"preset_name": "runpod"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(get_standin(self.settings.data_path, None))

    def test_fell_back_choice_ignores_an_active_standin(self):
        from backend.api.config import _fell_back_choice
        d = self.settings.data_path
        set_preferred_preset(d, 7, "runpod")
        set_standin(d, 7, "remote-mpcdf", self.until, "runpod")
        mpcdf = get_preset("remote-mpcdf", d)
        self.assertIsNone(_fell_back_choice(self.settings, identity(7), mpcdf))
        # without a stand-in the same mismatch is a real fall-back
        set_standin(d, 7, "remote-mpcdf", datetime.now(timezone.utc) - timedelta(minutes=1), "runpod")
        self.assertEqual(_fell_back_choice(self.settings, identity(7), mpcdf), "runpod")

    def test_index_route_ignores_standin(self):
        self.exhaust()
        self.call("PUT", json={"preset_name": "remote-mpcdf"})
        router = self.app.router
        before = len(router.routes)

        def probe():
            return {"preset": get_settings().get_hardware_preset().name}

        self.app.add_api_route("/api/config/standin-probe", probe, methods=["GET"])
        self.app.add_api_route("/api/index-probe", probe, methods=["GET"])
        self.addCleanup(lambda: [router.routes.pop() for _ in range(len(router.routes) - before)])
        self.assertEqual(self.call("GET", "/api/config/standin-probe").json()["preset"], "remote-mpcdf")
        self.assertEqual(self.call("GET", "/api/index-probe").json()["preset"], "remote-kisski")


if __name__ == "__main__":
    unittest.main()
