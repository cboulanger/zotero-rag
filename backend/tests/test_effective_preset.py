"""Per-user preset resolution and its binding to a request."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.services.effective_preset import (
    embedding_model_identity,
    get_effective_preset,
    get_user_preset,
    is_compatible,
    use_preset,
)
from backend.services.user_settings import set_preferred_preset
from backend.services.zotero_identity import ZoteroIdentity


def identity(user_id):
    return ZoteroIdentity(user_id=user_id, username=f"u{user_id}", targets=[f"users/{user_id}"])


class EffectivePresetTest(unittest.TestCase):
    def setUp(self):
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        self.settings.model_preset = "remote-kisski"

    def test_identity_basename_normalisation(self):
        self.assertEqual(embedding_model_identity("intfloat/multilingual-e5-large-instruct"),
                         embedding_model_identity("multilingual-e5-large-instruct"))

    def test_a_valid_compatible_choice_wins(self):
        set_preferred_preset(self.settings.data_path, 1, "runpod")
        self.assertEqual(get_effective_preset(self.settings, identity(1)).name, "runpod")
        self.assertEqual(get_user_preset(self.settings, identity(1)).name, "runpod")

    def test_no_choice_or_the_default_itself_means_the_default(self):
        self.assertEqual(get_effective_preset(self.settings, identity(1)).name, "remote-kisski")
        set_preferred_preset(self.settings.data_path, 1, "remote-kisski")
        self.assertIsNone(get_user_preset(self.settings, identity(1)))

    def test_no_identity_gets_the_default_even_if_users_have_choices(self):
        set_preferred_preset(self.settings.data_path, 1, "runpod")
        self.assertEqual(get_effective_preset(self.settings, None).name, "remote-kisski")

    def test_removed_incompatible_or_unknown_choices_fall_back(self):
        for choice in ("no-such-preset", "remote-openai", "cpu-only"):  # unknown / other embedding model / local
            set_preferred_preset(self.settings.data_path, 1, choice)
            self.assertEqual(get_effective_preset(self.settings, identity(1)).name, "remote-kisski", choice)

    def test_a_local_default_admits_nothing_else(self):
        self.settings.model_preset = "cpu-only"
        set_preferred_preset(self.settings.data_path, 1, "runpod")
        self.assertEqual(get_effective_preset(self.settings, identity(1)).name, "cpu-only")

    def test_is_compatible_requires_same_embedding_model_and_remote_sides(self):
        from backend.config.presets import get_preset
        d = self.settings.data_path
        kisski, runpod, openai = (get_preset(n, d) for n in ("remote-kisski", "runpod", "remote-openai"))
        self.assertTrue(is_compatible(kisski, runpod))
        self.assertFalse(is_compatible(kisski, openai))
        self.assertTrue(is_compatible(kisski, kisski))

    def test_two_identities_get_different_presets_in_the_same_process(self):
        set_preferred_preset(self.settings.data_path, 1, "runpod")
        self.assertEqual(get_effective_preset(self.settings, identity(1)).name, "runpod")
        self.assertEqual(get_effective_preset(self.settings, identity(2)).name, "remote-kisski")


class RequestBindingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        self.settings.model_preset = "remote-kisski"

    async def test_the_bound_preset_is_seen_in_threads_and_tasks_but_not_outside(self):
        from backend.config.presets import get_preset
        runpod = get_preset("runpod", self.settings.data_path)
        with use_preset(runpod):
            self.assertEqual(self.settings.get_hardware_preset().name, "runpod")
            self.assertEqual((await asyncio.to_thread(self.settings.get_hardware_preset)).name, "runpod")
            self.assertEqual((await asyncio.create_task(self._name())), "runpod")
        self.assertEqual(self.settings.get_hardware_preset().name, "remote-kisski")

    async def _name(self):
        return self.settings.get_hardware_preset().name

    async def test_concurrent_requests_do_not_leak_into_each_other(self):
        from backend.config.presets import get_preset
        runpod = get_preset("runpod", self.settings.data_path)

        async def request(preset):
            with use_preset(preset):
                await asyncio.sleep(0.01)
                return self.settings.get_hardware_preset().name

        names = await asyncio.gather(request(runpod), request(None), request(runpod), request(None))
        self.assertEqual(names, ["runpod", "remote-kisski", "runpod", "remote-kisski"])

    def test_the_middleware_binds_each_users_own_preset(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        set_preferred_preset(self.settings.data_path, 1, "runpod")
        seen = {}

        for uid in (1, 2):
            with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity(uid))), \
                 TestClient(app) as client:
                seen[uid] = client.get("/api/config", headers={"X-Zotero-API-Key": "Z"}).json()["preset_name"]
        self.assertEqual(seen, {1: "runpod", 2: "remote-kisski"})


if __name__ == "__main__":
    unittest.main()
