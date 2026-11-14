"""Per-user MPCDF sessions (backend/services/mpcdf_sessions.py) and the MPCDF rerank side.

The session tests run against ``mpcdf_llm.testing.FakeLlmis`` and are skipped when the
optional ``mpcdf-llm`` library is not installed. The provider and API tests use a stand-in
for the sessions registry and need no library.
"""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from backend.config.presets import HardwarePreset
from backend.config.settings import get_settings
from backend.providers import get_providers
from backend.services import mpcdf_sessions
from backend.services.mpcdf_sessions import EndpointCredentials, MpcdfSessions, user_key
from backend.services.rerank import RerankerUnavailable, RerankRequest

HAVE_LIB = mpcdf_sessions.available()


def rerank_preset(**options) -> HardwarePreset:
    return HardwarePreset.model_validate({
        "name": "t", "description": "t", "version": 2,
        "embedding": {"model_type": "remote", "model_name": "m", "model_kwargs": {"api_key_env": "K"}},
        "llm": {"model_type": "remote", "model_names": ["m"], "model_kwargs": {"api_key_env": "K"}},
        "rag": {}, "memory_budget_gb": 1.0,
        "rerank": {
            "model_type": "remote", "model_name": "BAAI/bge-reranker-v2-m3",
            "provider": {"id": "mpcdf", "scope": "user", "options": options},
        },
    })


class SecretMixin:
    def setUp(self):
        super().setUp()
        settings = get_settings()
        self._old_secret = settings.autoindex_secret
        settings.autoindex_secret = Fernet.generate_key().decode()
        self.addCleanup(setattr, settings, "autoindex_secret", self._old_secret)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class UserKeyTest(unittest.TestCase):
    def test_keys_are_stable_and_distinct(self):
        self.assertEqual(user_key(None), "local")
        self.assertEqual(user_key(5), user_key(5))
        self.assertNotEqual(user_key(5), user_key(6))
        self.assertNotIn("5", user_key(5)[:0] + "x")  # opaque: no raw id in the path


@unittest.skipUnless(HAVE_LIB, "mpcdf-llm not installed")
class SessionsWithFakeServiceTest(SecretMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from mpcdf_llm.manager import ProvisioningManager
        from mpcdf_llm.testing import FakeLlmis

        self.fake = FakeLlmis()
        self.fake.users["bob"] = ("pw2", "654321")
        base_url = await self.fake.start()
        self.http = httpx.AsyncClient(transport=self.fake.http_transport())

        def factory(directory, store):
            return ProvisioningManager(directory, base_url=base_url, http=self.http, sessions=store,
                                       hf_token=store.hf_token, poll_interval=0.05)

        self.sessions = MpcdfSessions(Path(self.tmp.name), manager_factory=factory)
        await self.sessions.open()

    async def asyncTearDown(self):
        await self.sessions.close()
        await self.fake.stop()
        await self.http.aclose()

    async def test_login_is_per_user_and_sealed(self):
        info = await self.sessions.login(1, "alice", "pw", "123456", hf_token="hf_x")
        self.assertEqual((info["state"], info["username"], info["hf_token_set"]), ("ok", "alice", True))
        self.assertEqual((await self.sessions.status(2))["state"], "login_required")
        raw = (Path(self.tmp.name) / user_key(1) / mpcdf_sessions.SESSION_FILE).read_text()
        self.assertNotIn("hf_x", raw)
        self.assertIn('"enc"', raw)

    async def test_rejected_login_raises(self):
        from mpcdf_llm.errors import LoginFailed

        with self.assertRaises(LoginFailed):
            await self.sessions.login(1, "alice", "wrong", "000000")

    async def test_login_attempts_are_rate_limited(self):
        from mpcdf_llm.errors import LoginFailed

        for _ in range(mpcdf_sessions.LOGIN_ATTEMPTS):
            with self.assertRaises(LoginFailed):
                await self.sessions.login(3, "alice", "wrong", "000000")
        with self.assertRaises(mpcdf_sessions.LoginRateLimited):
            await self.sessions.login(3, "alice", "pw", "123456")

    async def test_logged_out_user_has_no_endpoint(self):
        self.assertEqual(await self.sessions.endpoint_state(9, "zotero-rag-rerank"),
                         ("unreachable", "log in to the MPCDF service first"))
        self.assertIsNone(await self.sessions.credentials(9, "zotero-rag-rerank"))

    async def test_start_endpoint_and_isolation_between_users(self):
        from mpcdf_llm import names

        await self.sessions.login(1, "alice", "pw", "123456", hf_token="hf_x")
        await self.sessions.login(2, "bob", "pw2", "654321", hf_token="hf_y")
        preset = get_providers(rerank_preset())["rerank"].library_preset()
        await self.sessions.put_preset(1, preset)
        lines = []

        async def drive():
            while 1 not in self.fake.endpoints:
                await asyncio.sleep(0.005)
            for status in (names.BUILDING, names.RUNNING):
                await asyncio.sleep(0.05)
                self.fake.advance(1, status)

        driver = asyncio.create_task(drive())
        creds = await asyncio.wait_for(self.sessions.start_endpoint(1, preset.name, lines.append), 10)
        await driver
        self.assertTrue(creds.base_url and creds.api_key)
        self.assertEqual(creds.model, "BAAI/bge-reranker-v2-m3")
        self.assertTrue(lines)
        self.assertEqual((await self.sessions.endpoint_state(1, preset.name))[0], "ready")
        self.assertEqual((await self.sessions.credentials(1, preset.name)).base_url, creds.base_url)
        # Bob has his own session and store: Alice's endpoint is not his.
        self.assertIsNone(await self.sessions.credentials(2, preset.name))
        await self.sessions.touch(1, preset.name)
        self.assertEqual(self.fake.submitted[0]["hf_token"], "hf_x")

    async def test_stored_sessions_are_reopened_on_start(self):
        await self.sessions.login(1, "alice", "pw", "123456")
        await self.sessions.close()  # a server restart
        self.sessions = MpcdfSessions(Path(self.tmp.name), manager_factory=self.sessions._factory)
        await self.sessions.open()
        self.assertIn(user_key(1), self.sessions._managers)
        self.assertEqual((await self.sessions.status(1))["state"], "ok")


class FakeSessions:
    """Stand-in for MpcdfSessions; ``call`` runs the coroutine on a private loop."""

    def __init__(self):
        self.creds = {}
        self.touched = []
        self.state = {"state": "ok", "username": "alice", "expires_at": None, "hf_token_set": True}
        self.logins = []

    def call(self, fn, timeout=30.0):
        result = {}

        def run():
            try:
                result["v"] = asyncio.run(fn())
            except Exception as exc:  # re-raised in the caller's thread
                result["e"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout)
        if "e" in result:
            raise result["e"]
        return result["v"]

    async def credentials(self, user_id, preset):
        return self.creds.get((user_id, preset))

    async def touch(self, user_id, preset):
        self.touched.append((user_id, preset))

    async def endpoint_state(self, user_id, preset):
        return ("ready", "") if (user_id, preset) in self.creds else ("unreachable", "not running")

    async def status(self, user_id):
        return self.state

    async def login(self, user_id, username, password, otp, hf_token=None):
        self.logins.append((user_id, username))
        return self.state


class MpcdfRerankProviderTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSessions()
        patches = [
            patch.object(mpcdf_sessions, "available", return_value=True),
            patch.object(mpcdf_sessions, "get_sessions", return_value=self.fake),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.providers = get_providers(rerank_preset(gpus=2))
        self.provider = self.providers["rerank"]

    def test_rerank_side_is_per_user_and_provisionable(self):
        self.assertEqual(self.provider.scope, "user")
        self.assertTrue(self.provider.supports_provisioning and self.provider.supports_suspend)
        self.assertFalse(self.provider.provisioning_needs_key)
        d = self.provider.describe()
        self.assertEqual([f.name for f in d.provisioning.login.fields], ["username", "password", "otp", "hf_token"])
        self.assertIsNone(d.provisioning.credential)

    def test_shared_sides_are_unchanged(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "config/default_presets/remote-mpcdf.json").read_text())
        preset = HardwarePreset.model_validate({"name": "remote-mpcdf", **data})
        llm = get_providers(preset)["llm"]
        self.assertEqual(llm.scope, "shared")
        self.assertFalse(llm.supports_provisioning)
        self.assertTrue(llm.provisioning_needs_key)

    def test_unknown_option_rejected(self):
        from backend.providers import ProviderConfigError

        with self.assertRaises(ProviderConfigError):
            get_providers(rerank_preset(gpu="x"))

    def test_credentials_come_from_the_callers_session(self):
        req = RerankRequest(user_id=7)
        self.assertIsNone(self.provider.rerank_credentials(req))
        self.fake.creds[(7, "zotero-rag-rerank")] = EndpointCredentials("https://ep/v1", "k", "m")
        creds = self.provider.rerank_credentials(req)
        self.assertEqual((creds.base_url, creds.api_key), ("https://ep/v1", "k"))
        self.assertIsNone(self.provider.rerank_credentials(RerankRequest(user_id=8)))
        self.provider.rerank_activity(req)
        self.assertEqual(self.fake.touched, [(7, "zotero-rag-rerank")])

    def test_health_follows_the_callers_endpoint(self):
        from backend.providers import Credentials

        self.assertEqual(self.provider.health(Credentials(user_id=7)).status, "unreachable")
        self.fake.creds[(7, "zotero-rag-rerank")] = EndpointCredentials("u", "k", "m")
        self.assertEqual(self.provider.health(Credentials(user_id=7)).status, "ready")

    def test_provision_requires_login_and_hf_token(self):
        from backend.providers import ProvisionContext
        from backend.providers.types import ProvisionError

        ctx = ProvisionContext(side="rerank", preset=self.provider.preset, credential=None,
                               data_path=Path("."), extra={"user_id": 7})
        self.fake.state = {"state": "login_required"}
        with self.assertRaisesRegex(ProvisionError, "Log in"):
            self.provider.provision(ctx, lambda m: None)
        self.fake.state = {"state": "ok", "hf_token_set": False}
        with self.assertRaisesRegex(ProvisionError, "Hugging Face"):
            self.provider.provision(ctx, lambda m: None)

    @unittest.skipUnless(HAVE_LIB, "mpcdf-llm not installed")
    def test_library_preset_uses_model_and_options(self):
        p = self.provider.library_preset()
        self.assertEqual((p.name, p.model, p.gpus, p.extra_args), ("zotero-rag-rerank", "BAAI/bge-reranker-v2-m3", 2, ["--enforce-eager"]))

    def test_missing_library_is_reported(self):
        with patch.object(mpcdf_sessions, "available", return_value=False):
            self.assertIn("not installed", self.provider.unavailable_hint())
            with self.assertRaises(RerankerUnavailable):
                self.provider.rerank_credentials(RerankRequest(user_id=7))


class SessionApiTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from backend.config.presets import ensure_default_presets
        from backend.config.settings import reset_settings
        from backend.main import app

        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        data = json.loads((self.settings.data_path / "presets" / "remote-kisski.json").read_text())
        data.pop("name", None)
        data["rerank"] = {"model_type": "remote", "model_name": "BAAI/bge-reranker-v2-m3",
                          "provider": {"id": "mpcdf", "scope": "user"}}
        (self.settings.data_path / "presets" / "kisski-mpcdf-rerank.json").write_text(json.dumps(data))
        self.settings.model_preset = "kisski-mpcdf-rerank"
        self.fake = FakeSessions()
        for p in (patch.object(mpcdf_sessions, "available", return_value=True),
                  patch.object(mpcdf_sessions, "get_sessions", return_value=self.fake)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    def test_login_status_and_descriptor(self):
        r = self.client.get("/api/config/providers")
        self.assertEqual(r.status_code, 200, r.text)
        login = r.json()["sides"]["rerank"]["provider"]["provisioning"]["login"]
        self.assertEqual(login["fields"][1]["type"], "password")
        r = self.client.post("/api/config/session/login", json={
            "side": "rerank", "values": {"username": "alice", "password": "pw", "otp": "123456"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "ok")
        self.assertEqual(self.fake.logins, [(None, "alice")])  # loopback: the single local user
        self.assertEqual(self.client.get("/api/config/session", params={"side": "rerank"}).json()["state"], "ok")

    def test_side_without_login_is_rejected(self):
        r = self.client.post("/api/config/session/login", json={"side": "llm", "values": {}})
        self.assertEqual(r.status_code, 400)

    def test_rejected_login_does_not_echo_values(self):
        class LoginFailed(Exception):
            pass

        self.fake.login = AsyncMock(side_effect=LoginFailed("bad pw secret-typed"))
        r = self.client.post("/api/config/session/login", json={
            "side": "rerank", "values": {"username": "a", "password": "secret-typed", "otp": "1"}})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("secret-typed", r.text)

    def test_provision_needs_no_key_for_a_session_side(self):
        with patch("backend.services.provisioning.run_job", new=AsyncMock()):
            r = self.client.post("/api/config/provision", json={"sides": ["rerank"]})
        self.assertIn(r.status_code, (200, 202), r.text)


if __name__ == "__main__":
    unittest.main()
