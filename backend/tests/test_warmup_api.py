"""POST /api/config/warmup: wake cold endpoints when the question dialog opens."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.api import config as config_api
from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import ZoteroIdentity


class WarmupApiTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        settings = get_settings()
        settings.data_path = Path(self.tmp.name)
        settings.model_preset = "runpod"
        ensure_default_presets(settings.data_path)
        config_api._last_warmup.clear()
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _post(self, statuses):
        ident = ZoteroIdentity(user_id=5, username="u", targets=["users/5"])

        def check(preset, side, request=None):
            status = statuses.get(side)
            return None if status is None else config_api.EndpointHealth(status=status, detail="")

        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=ident)), \
             patch.object(config_api, "_check_side", side_effect=check), \
             patch.object(config_api, "_send_warmup", new=AsyncMock()) as send:
            r = self.client.post("/api/config/warmup", headers={"X-Zotero-API-Key": "Z"})
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["warming"], send

    def test_only_cold_sides_are_warmed(self):
        warming, send = self._post({"embedding": "ready", "llm": "cold"})
        self.assertEqual(warming, ["llm"])
        self.assertEqual([c.args[0] for c in send.call_args_list], ["llm"])

    def test_paused_or_unreachable_sides_are_left_alone(self):
        warming, send = self._post({"embedding": "paused", "llm": "unreachable"})
        self.assertEqual(warming, [])
        send.assert_not_called()

    def test_a_second_call_within_the_interval_does_not_repeat_it(self):
        self.assertEqual(self._post({"llm": "cold"})[0], ["llm"])
        self.assertEqual(self._post({"llm": "cold"})[0], [])
