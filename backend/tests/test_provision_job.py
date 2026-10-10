"""Tests for the provisioning job runner and the /api/config/provision endpoints."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.config.presets import ensure_default_presets, get_preset
from backend.config.settings import get_settings, reset_settings
from backend.providers import Provider, ProvisionContext, ProvisionError
from backend.tests.runpod_variants import write_managed_runpod_preset
from backend.providers.runpod import RunPodProvider
from backend.services import provisioning
from backend.services.admin_settings_store import get_remote_config_value, update_remote_config
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
from backend.zotero.group_roles import reset_admin_role_cache


class _FakeProvider(Provider):
    """Not registered (no own ``id``): a stand-in handed straight to the runner."""


def _job(side, provision, tmp):
    provider = _FakeProvider(side, preset=None)
    provider.provision = provision
    ctx = ProvisionContext(side=side, preset=None, credential="k", data_path=Path(tmp))
    return provisioning.SideJob(side=side, provider=provider, ctx=ctx)


class RunJobTest(unittest.TestCase):
    def setUp(self):
        provisioning.reset_job_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        provisioning.reset_job_state()

    def run_jobs(self, jobs):
        provisioning.mark_running([j.side for j in jobs])
        asyncio.run(provisioning.run_job(jobs, data_path=self.data_path))
        return provisioning.get_job_state()

    def test_success_applies_each_sides_values_and_records_progress(self):
        def ok(url_env, url):
            def provision(ctx, progress):
                progress("creating")
                progress("ready")
                return {url_env: url}
            return provision

        state = self.run_jobs([
            _job("embedding", ok("EMB_URL", "https://e/v1"), self.tmp.name),
            _job("llm", ok("LLM_URL", "https://l/v1"), self.tmp.name),
        ])
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["sides"]["embedding"], {"status": "succeeded", "message": None})
        self.assertEqual(state["sides"]["llm"]["status"], "succeeded")
        self.assertEqual(state["progress"], ["embedding: creating", "embedding: ready", "llm: creating", "llm: ready"])
        self.assertEqual(get_remote_config_value("EMB_URL", data_path=self.data_path), "https://e/v1")
        self.assertEqual(get_remote_config_value("LLM_URL", data_path=self.data_path), "https://l/v1")

    def test_a_side_that_exceeds_the_deadline_is_marked_failed_and_the_job_fails(self):
        from backend.providers import ProvisionTimeout

        def too_slow(ctx, progress):
            raise ProvisionTimeout("llm job exceeded its deadline")

        state = self.run_jobs([_job("llm", too_slow, self.tmp.name)])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["sides"]["llm"]["status"], "failed")
        self.assertIn("deadline", state["sides"]["llm"]["message"])

    def test_a_failed_side_is_recorded_and_the_other_side_still_runs_and_is_kept(self):
        def boom(ctx, progress):
            raise ProvisionError("no capacity")

        state = self.run_jobs([
            _job("embedding", boom, self.tmp.name),
            _job("llm", lambda ctx, progress: {"LLM_URL": "https://l/v1"}, self.tmp.name),
        ])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["sides"]["embedding"], {"status": "failed", "message": "no capacity"})
        self.assertEqual(state["sides"]["llm"]["status"], "succeeded")
        self.assertIn("embedding: no capacity", state["message"])
        self.assertEqual(get_remote_config_value("LLM_URL", data_path=self.data_path), "https://l/v1")

    def test_a_provider_that_returns_nothing_stores_nothing(self):
        state = self.run_jobs([_job("llm", lambda ctx, progress: {}, self.tmp.name)])
        self.assertEqual(state["status"], "succeeded")

    def test_every_side_gets_the_job_deadline(self):
        seen = []

        def spy(ctx, progress):
            seen.append(ctx.deadline)
            return {}

        provisioning.mark_running(["llm"])
        asyncio.run(provisioning.run_job([_job("llm", spy, self.tmp.name)], data_path=self.data_path, timeout_seconds=60))
        self.assertIsNotNone(seen[0])
        self.assertAlmostEqual(seen[0] - time.monotonic(), 60, delta=5)

    def test_unexpected_exceptions_do_not_leave_the_job_running(self):
        def crash(ctx, progress):
            raise RuntimeError("kaboom")

        state = self.run_jobs([_job("llm", crash, self.tmp.name)])
        self.assertEqual(state["status"], "failed")
        self.assertIn("kaboom", state["message"])

    def test_progress_is_bounded(self):
        def chatty(ctx, progress):
            for i in range(provisioning.MAX_PROGRESS_LINES + 50):
                progress(f"step {i}")
            return {}

        state = self.run_jobs([_job("llm", chatty, self.tmp.name)])
        self.assertEqual(len(state["progress"]), provisioning.MAX_PROGRESS_LINES)
        self.assertTrue(state["progress"][-1].endswith(f"step {provisioning.MAX_PROGRESS_LINES + 49}"))


class ProvisionEndpointsTest(unittest.TestCase):
    EMB_URL, LLM_URL = "RUNPOD_EMBEDDING_BASE_URL", "RUNPOD_LLM_BASE_URL"

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
        s.model_preset = write_managed_runpod_preset(s.data_path)
        self.app = app
        # Entered as a context manager so one event loop outlives the POST: the
        # provisioning task runs on it after the 202 response, as under uvicorn.
        # A bare TestClient tears the loop down per request and can cancel the
        # task mid-run, which made this suite flaky.
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.calls = []

        def fake_provision(this, ctx, progress):
            self.calls.append((this.side, ctx.credential))
            progress("working")
            env = this.side_config.model_kwargs["shared_base_url_env"]
            return {env: f"https://api.runpod.ai/v2/{this.side}/openai/v1"}

        patcher = patch.object(RunPodProvider, "provision", fake_provision)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def _store_key(self, value="rpa_STORED"):
        update_remote_config({"RUNPOD_API_KEY": value}, data_path=get_settings().data_path)

    def _wait(self):
        status = {}
        for _ in range(600):  # 30 s: CI runners are slow to spin up the worker thread
            status = self.client.get("/api/config/provision/status").json()
            if status["status"] != "running":
                return status
            time.sleep(0.05)
        return status

    def _provision(self, body=None):
        self._override_admin()
        r = self.client.post("/api/config/provision", json=body) if body is not None else self.client.post("/api/config/provision")
        return r, (self._wait() if r.status_code == 202 else None)

    def test_requires_admin(self):
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post("/api/config/provision", headers={"X-Zotero-API-Key": "K"})
        self.assertEqual(r.status_code, 403)

    def test_400_when_no_side_can_be_provisioned(self):
        self._override_admin()
        get_settings().model_preset = "remote-kisski"
        r = self.client.post("/api/config/provision")
        self.assertEqual(r.status_code, 400)
        self.assertIn("no side that can be provisioned", r.json()["detail"])

    def test_409_when_already_running(self):
        self._override_admin()
        self._store_key()
        provisioning.mark_running(["embedding", "llm"])
        self.assertEqual(self.client.post("/api/config/provision").status_code, 409)

    def test_400_when_no_key_is_available_for_a_side(self):
        r, _ = self._provision({})
        self.assertEqual(r.status_code, 400)
        self.assertIn("RUNPOD_API_KEY", r.json()["detail"])
        self.assertEqual(self.calls, [])

    def test_success_runs_both_sides_in_order_and_applies_the_urls(self):
        self._store_key()
        r, status = self._provision()
        self.assertEqual(r.status_code, 202)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(self.calls, [("embedding", "rpa_STORED"), ("llm", "rpa_STORED")])
        self.assertEqual(status["sides"]["embedding"]["status"], "succeeded")
        self.assertEqual(status["progress"], ["embedding: working", "llm: working"])
        data_path = get_settings().data_path
        self.assertEqual(get_remote_config_value(self.EMB_URL, data_path=data_path),
                         "https://api.runpod.ai/v2/embedding/openai/v1")
        self.assertEqual(get_remote_config_value(self.LLM_URL, data_path=data_path),
                         "https://api.runpod.ai/v2/llm/openai/v1")

    def test_a_supplied_key_is_used_for_this_run_only_and_never_stored(self):
        r, status = self._provision({"keys": {"RUNPOD_API_KEY": "rpa_FULL"}})
        self.assertEqual(r.status_code, 202)
        self.assertEqual({c[1] for c in self.calls}, {"rpa_FULL"})
        self.assertIsNone(get_remote_config_value("RUNPOD_API_KEY", data_path=get_settings().data_path))

    def test_a_supplied_key_does_not_replace_a_stored_key(self):
        self._store_key("rpa_RESTRICTED")
        self._provision({"keys": {"RUNPOD_API_KEY": "rpa_FULL"}})
        self.assertEqual({c[1] for c in self.calls}, {"rpa_FULL"})
        self.assertEqual(get_remote_config_value("RUNPOD_API_KEY", data_path=get_settings().data_path), "rpa_RESTRICTED")

    def test_rejects_a_malformed_supplied_key(self):
        r, _ = self._provision({"keys": {"RUNPOD_API_KEY": "not-a-runpod-key"}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("expected format", r.json()["detail"])
        self.assertEqual(self.calls, [])
        self.assertEqual(provisioning.get_job_state()["status"], "idle")

    def test_sides_runs_only_the_requested_side(self):
        self._store_key()
        r, status = self._provision({"sides": ["llm"]})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.calls, [("llm", "rpa_STORED")])
        self.assertEqual(set(status["sides"]), {"llm"})
        self.assertIsNone(get_remote_config_value(self.EMB_URL, data_path=get_settings().data_path))

    def test_unknown_and_unprovisionable_sides_are_rejected(self):
        self._store_key()
        self._override_admin()
        self.assertEqual(self.client.post("/api/config/provision", json={"sides": ["nope"]}).status_code, 400)
        get_settings().model_preset = "remote-kisski"
        r = self.client.post("/api/config/provision", json={"sides": ["llm"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot be provisioned", r.json()["detail"])

    def test_a_failed_side_is_reported_and_a_retry_of_just_that_side_works(self):
        self._store_key()
        outcomes = {"embedding": ProvisionError("no capacity")}

        def flaky(this, ctx, progress):
            self.calls.append(this.side)
            err = outcomes.get(this.side)
            if err:
                raise err
            return {this.side_config.model_kwargs["shared_base_url_env"]: f"https://{this.side}/v1"}

        with patch.object(RunPodProvider, "provision", flaky):
            _, status = self._provision()
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["sides"]["embedding"], {"status": "failed", "message": "no capacity"})
            self.assertEqual(status["sides"]["llm"]["status"], "succeeded")
            outcomes.clear()
            self.calls.clear()
            _, status = self._provision({"sides": ["embedding"]})
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(self.calls, ["embedding"])
        data_path = get_settings().data_path
        self.assertEqual(get_remote_config_value(self.EMB_URL, data_path=data_path), "https://embedding/v1")
        self.assertEqual(get_remote_config_value(self.LLM_URL, data_path=data_path), "https://llm/v1")

    def test_required_keys_omit_the_provisioned_base_urls_but_keep_the_key(self):
        keys = {k["key_name"]: k for k in self.client.get("/api/required-keys").json()["keys"]}
        self.assertIn("RUNPOD_API_KEY", keys)
        self.assertNotIn(self.EMB_URL, keys)
        self.assertNotIn(self.LLM_URL, keys)


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

    def test_null_for_preset_whose_providers_have_no_health_concept(self):
        get_settings().model_preset = "remote-kisski"
        r = self.client.get("/api/config/health")
        self.assertEqual(r.json(), {"embedding": None, "llm": None})
        self.assertFalse(self.client.get("/api/config").json()["provisionable"])

    def test_runpod_reports_statuses(self):
        get_settings().model_preset = write_managed_runpod_preset(get_settings().data_path)
        update_remote_config({
            "RUNPOD_API_KEY": "k",
            "RUNPOD_EMBEDDING_BASE_URL": "https://api.runpod.ai/v2/e1/openai/v1",
            "RUNPOD_LLM_BASE_URL": "https://api.runpod.ai/v2/l1/openai/v1",
        }, data_path=get_settings().data_path)
        from backend.providers import Health

        def fake(self, creds):
            return Health(status="ready" if "e1" in creds.base_url else "cold", detail="d")
        with patch.object(RunPodProvider, "health", fake):
            r = self.client.get("/api/config/health")
        self.assertEqual(r.json()["embedding"]["status"], "ready")
        self.assertEqual(r.json()["llm"]["status"], "cold")
        self.assertTrue(self.client.get("/api/config").json()["provisionable"])

    def test_unconfigured_side_is_unreachable_not_null(self):
        get_settings().model_preset = write_managed_runpod_preset(get_settings().data_path)
        with patch.dict("os.environ", {}, clear=True):
            r = self.client.get("/api/config/health")
        self.assertEqual(r.json()["embedding"], {"status": "unreachable", "detail": "not configured"})

    def test_mpcdf_reports_an_expired_job(self):
        get_settings().model_preset = "remote-mpcdf"
        update_remote_config({
            "MPCDF_EMBEDDING_API_KEY": "k", "MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/job1",
            "MPCDF_LLM_API_KEY": "k", "MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/job2",
        }, data_path=get_settings().data_path)
        from backend.tests.provider_fakes import FakeHTTP, FakeResponse
        from backend.providers.mpcdf import MpcdfProvider
        fake = FakeHTTP({("GET", r"/job1/v1/models$"): FakeResponse(200, {}),
                         ("GET", r"/job2/v1/models$"): FakeResponse(404, {})})
        original = MpcdfProvider.__init__

        def init(this, *a, **kw):
            original(this, *a, **kw)
            this._http_client = fake
        with patch.object(MpcdfProvider, "__init__", init):
            r = self.client.get("/api/config/health").json()
        self.assertEqual(r["embedding"]["status"], "ready")
        self.assertEqual(r["llm"]["status"], "unreachable")
        self.assertIn("job expired or not started", r["llm"]["detail"])


if __name__ == "__main__":
    unittest.main()


class UserScopeProvisionTest(unittest.TestCase):
    """The bundled ``runpod`` preset (scope ``user``): the caller's own key, the caller's own job slot."""

    KEY_HEADER = "X-Runpod-Api-Key"

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
        s.api_host = "rag.example.com"  # not loopback: identities are required
        s.authorized_group_id = 999
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.calls = []

        def fake_provision(this, ctx, progress):
            self.calls.append((this.side, ctx.credential))
            progress("working")
            return {}

        patcher = patch.object(RunPodProvider, "provision", fake_provision)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()
        provisioning.reset_job_state()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _as(self, user_id):
        identity = ZoteroIdentity(user_id=user_id, username=f"u{user_id}", targets=[f"users/{user_id}"])
        return patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity))

    def _post(self, user_id, key="rpa_USER", body=None):
        headers = {"X-Zotero-API-Key": "Z"}
        if key:
            headers[self.KEY_HEADER] = key
        with self._as(user_id), patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            return self.client.post("/api/config/provision", headers=headers, json=body or {})

    def _status(self, user_id):
        with self._as(user_id):
            return self.client.get("/api/config/provision/status", headers={"X-Zotero-API-Key": "Z"}).json()

    def _wait(self, user_id):
        for _ in range(600):
            status = self._status(user_id)
            if status["status"] != "running":
                return status
            time.sleep(0.05)
        return status

    def test_a_non_admin_user_can_provision_on_their_own_key(self):
        r = self._post(7)
        self.assertEqual(r.status_code, 202, r.text)
        status = self._wait(7)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(self.calls, [("embedding", "rpa_USER"), ("llm", "rpa_USER")])

    def test_without_the_users_key_it_is_a_400_naming_the_key(self):
        r = self._post(7, key=None)
        self.assertEqual(r.status_code, 400)
        self.assertIn("RUNPOD_API_KEY", r.json()["detail"])

    def test_a_one_time_key_in_the_body_wins_over_the_header(self):
        self._post(7, key="rpa_HEADER", body={"keys": {"RUNPOD_API_KEY": "rpa_ONETIME"}})
        self._wait(7)
        self.assertEqual({c for _, c in self.calls}, {"rpa_ONETIME"})

    def test_unauthenticated_callers_are_refused(self):
        r = self.client.post("/api/config/provision", headers={self.KEY_HEADER: "rpa_X"})
        self.assertIn(r.status_code, (401, 403))
        self.assertEqual(self.calls, [])

    def test_job_slots_are_per_user(self):
        provisioning.mark_running(["embedding", "llm"], provisioning.user_slot(7))
        self.assertEqual(self._post(7).status_code, 409)       # same user: already running
        self.assertEqual(self._post(8).status_code, 202)       # another user is unaffected
        self.assertEqual(self._wait(8)["status"], "succeeded")

    def test_status_is_the_callers_own_job(self):
        self._post(7)
        self._wait(7)
        self.assertEqual(self._status(7)["status"], "succeeded")
        self.assertEqual(self._status(8)["status"], "idle")

    def test_health_uses_the_callers_key_and_derived_endpoint(self):
        from backend.providers import Health
        seen = []

        def endpoint_url(this, key):
            seen.append(("lookup", key))
            return f"https://api.runpod.ai/v2/{this.side}-{key}/openai/v1"

        def health(this, creds):
            seen.append(("health", creds.api_key, creds.base_url))
            return Health(status="ready", detail="")

        with patch.object(RunPodProvider, "endpoint_url", endpoint_url), \
             patch.object(RunPodProvider, "health", health), self._as(7):
            from backend.services.endpoint_cache import endpoint_cache
            endpoint_cache.clear()
            r = self.client.get("/api/config/health", headers={"X-Zotero-API-Key": "Z", self.KEY_HEADER: "alice"})
        self.assertEqual(r.json()["llm"]["status"], "ready")
        self.assertIn(("health", "alice", "https://api.runpod.ai/v2/llm-alice/openai/v1"), seen)

    def test_health_without_a_provisioned_endpoint_says_so(self):
        with patch.object(RunPodProvider, "endpoint_url", lambda this, key: None), self._as(7):
            from backend.services.endpoint_cache import endpoint_cache
            endpoint_cache.clear()
            r = self.client.get("/api/config/health", headers={"X-Zotero-API-Key": "Z", self.KEY_HEADER: "bob"})
        self.assertEqual(r.json()["llm"], {"status": "unreachable", "detail": "not provisioned"})

    def test_health_without_any_key_is_not_configured(self):
        with self._as(7):
            r = self.client.get("/api/config/health", headers={"X-Zotero-API-Key": "Z"})
        self.assertEqual(r.json()["llm"]["status"], "unreachable")
