"""GET /api/config/providers: per-side descriptors for the plugin."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import ZoteroIdentity
from backend.tests.runpod_variants import write_managed_runpod_preset


class DescriptorsApiTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _get(self, preset, admin=False, user_id=5, loopback=True):
        self.settings.model_preset = preset
        if not loopback:
            self.settings.api_host = "rag.example.com"
            self.settings.authorized_group_id = 999
        ident = ZoteroIdentity(user_id=user_id, username="u", targets=[f"users/{user_id}"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=ident)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=admin)):
            r = self.client.get("/api/config/providers", headers={"X-Zotero-API-Key": "Z"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_user_scope_provider_is_operable_by_any_signed_in_user(self):
        body = self._get("runpod", loopback=False)
        for side in ("embedding", "llm"):
            d = body["sides"][side]["provider"]
            self.assertEqual((d["id"], d["key_scope"], d["operable_by_caller"]), ("runpod", "user", True))
            self.assertTrue(d["supports_provisioning"] and d["supports_suspend"])
            self.assertEqual(d["provisioning"]["credential"]["env"], "RUNPOD_API_KEY")

    def test_managed_scope_is_operable_by_admins_only(self):
        name = write_managed_runpod_preset(self.settings.data_path)
        self.assertTrue(self._get(name, admin=True, loopback=False)["sides"]["llm"]["provider"]["operable_by_caller"])
        self.assertFalse(self._get(name, admin=False, user_id=6, loopback=False)["sides"]["llm"]["provider"]["operable_by_caller"])  # admin role is cached per user

    def test_shared_scope_and_non_operable_providers_are_never_operable(self):
        body = self._get("remote-mpcdf")
        d = body["sides"]["llm"]["provider"]
        self.assertEqual((d["id"], d["key_scope"], d["operable_by_caller"]), ("mpcdf", "shared", False))
        self.assertIn("MPCDF", d["unavailable_hint"])
        kisski = self._get("remote-kisski")["sides"]["llm"]["provider"]
        self.assertFalse(kisski["operable_by_caller"])
        self.assertFalse(kisski["supports_provisioning"])

    def test_local_side_has_no_provider(self):
        body = self._get("cpu-only")
        self.assertEqual(body["sides"]["embedding"], {"model_type": "local", "provider": None})

    def test_mixed_preset_describes_each_side_by_its_own_provider(self):
        path = self.settings.data_path / "presets" / "mixed.json"
        data = json.loads((self.settings.data_path / "presets" / "remote-kisski.json").read_text())
        data["llm"]["provider"] = {"id": "anthropic"}
        data["llm"]["model_names"] = ["claude-sonnet-4"]
        data["llm"]["model_kwargs"] = {}
        data.pop("name", None)
        path.write_text(json.dumps(data))
        body = self._get("mixed")
        self.assertEqual((body["sides"]["embedding"]["provider"]["id"], body["sides"]["llm"]["provider"]["id"]),
                         ("kisski", "anthropic"))

    def test_no_secret_material_in_any_descriptor(self):
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from walk(v)
            elif isinstance(node, list):
                for v in node:
                    yield from walk(v)

        for name in ("runpod", "remote-kisski", "remote-mpcdf", "remote-openai"):
            keys = set(walk(self._get(name)))
            self.assertFalse({k for k in keys if "secret" in k or "token" in k or k in ("api_key", "key", "value")}, name)


if __name__ == "__main__":
    unittest.main()
