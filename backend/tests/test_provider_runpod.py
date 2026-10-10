"""RunPodProvider: ported from the former provisioning script's tests, per side."""

import unittest
from pathlib import Path
from unittest.mock import patch

from backend.providers import Credentials, ProvisionContext, ProvisionError, ProvisionTimeout, get_providers
from backend.providers import runpod as runpod_module
from backend.providers.runpod import RunPodProvider, endpoint_base_url
from backend.tests.provider_fakes import FakeHTTP, FakeResponse
from backend.tests.test_providers_registry import make_preset

EMB_URL_ENV, LLM_URL_ENV = "RUNPOD_EMBEDDING_BASE_URL", "RUNPOD_LLM_BASE_URL"


def runpod_preset(options=None, scope="managed"):
    """A runpod preset in the shared-field shape (PR 1 keeps the shared store)."""
    preset = make_preset()
    for side, url_env in ((preset.embedding, EMB_URL_ENV), (preset.llm, LLM_URL_ENV)):
        side.provider.id = "runpod"
        side.provider.scope = scope
        side.provider.options = options or {}
        side.model_kwargs = {"shared_api_key_env": "RUNPOD_API_KEY", "shared_base_url_env": url_env}
    preset.embedding.model_name = "intfloat/multilingual-e5-large-instruct"
    preset.llm.model_names = ["Qwen/Qwen2.5-7B-Instruct"]
    return preset


def provider(side="llm", options=None, http=None, scope="managed"):
    p = get_providers(runpod_preset(options, scope))[side]
    p._http_client = http or FakeHTTP()
    return p


def ctx(side="llm", **kw):
    return ProvisionContext(side=side, preset=runpod_preset(), credential=kw.pop("credential", "rpa_key"),
                            data_path=Path("."), **kw)


def empty_account(http, **extra):
    http.add("GET", r"/templates$", FakeResponse(200, []))
    http.add("GET", r"/endpoints$", FakeResponse(200, []))
    http.add("POST", r"/templates$", FakeResponse(200, {"id": "tpl1", "name": "n"}))
    http.add("POST", r"/endpoints$", FakeResponse(200, {"id": "ep1", "name": "n"}))
    http.add("POST", r"/purge-queue$", FakeResponse(200, {}))
    for (m, pat), resp in extra.items():
        http.add(m, pat, resp)


class TestNamesAndTemplates(unittest.TestCase):
    def test_resource_names_and_model_per_side(self):
        emb, llm = provider("embedding"), provider("llm")
        self.assertEqual((emb.resource_name, llm.resource_name), ("zotero-rag-embedding", "zotero-rag-llm"))
        self.assertEqual(emb.model, "intfloat/multilingual-e5-large-instruct")
        self.assertEqual(llm.model, "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(emb.template_env(), {"MODEL_NAME": emb.model, "MAX_MODEL_LEN": "512"})
        self.assertEqual(llm.template_env(), {"MODEL_NAME": llm.model})

    def test_options_defaults_and_validation(self):
        o = provider().options
        self.assertEqual((o.gpu, o.workers_max, o.idle_timeout, o.data_centers), ("NVIDIA RTX A5000", 1, 60, None))
        from backend.providers import ProviderConfigError
        for bad in ({"workers_max": 0}, {"idle_timeout": 0}, {"gpus": "x"}):
            with self.assertRaises(ProviderConfigError):
                get_providers(runpod_preset(bad))

    def test_creates_template_when_missing_with_side_specific_disk(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("embedding", http=http)
        self.assertEqual(p._ensure_template("k", ctx("embedding"))["id"], "tpl1")
        (_, _, body), = http.calls_matching("POST", r"/templates$")
        self.assertEqual(body["containerDiskInGb"], 20)
        self.assertEqual(body["imageName"], runpod_module.DEFAULT_IMAGE)
        self.assertTrue(body["isServerless"])

    def test_reuses_matching_existing_template(self):
        http = FakeHTTP()
        p = provider("llm", http=http)
        http.add("GET", r"/templates$", FakeResponse(200, [
            {"id": "t9", "name": "zotero-rag-llm", "imageName": runpod_module.DEFAULT_IMAGE, "env": p.template_env()}]))
        self.assertEqual(p._ensure_template("k", ctx())["id"], "t9")
        self.assertEqual(http.calls_matching("POST", r"/templates"), [])

    def test_keeps_mismatched_template_with_a_warning_unless_recreate(self):
        http = FakeHTTP()
        p = provider("llm", http=http)
        http.add("GET", r"/templates$", FakeResponse(200, [
            {"id": "t9", "name": "zotero-rag-llm", "imageName": "old:image", "env": {}}]))
        with self.assertLogs(runpod_module.logger, level="WARNING") as logs:
            self.assertEqual(p._ensure_template("k", ctx())["id"], "t9")
        self.assertIn("differs", logs.output[0])
        self.assertEqual(http.calls_matching("DELETE", r"."), [])

    def test_recreate_deletes_the_referencing_endpoint_before_the_template(self):
        http = FakeHTTP()
        p = provider("llm", http=http)
        http.add("GET", r"/templates$", FakeResponse(200, [
            {"id": "t9", "name": "zotero-rag-llm", "imageName": "old:image", "env": {}}]))
        http.add("GET", r"/endpoints$", FakeResponse(200, [{"id": "e9", "name": "zotero-rag-llm"}]))
        http.add("DELETE", r"/endpoints/e9$", FakeResponse(204, None))
        http.add("DELETE", r"/templates/t9$", FakeResponse(204, None))
        http.add("POST", r"/templates$", FakeResponse(200, {"id": "t10"}))
        self.assertEqual(p._ensure_template("k", ctx(recreate=True))["id"], "t10")
        deletes = [c[1] for c in http.calls if c[0] == "DELETE"]
        self.assertTrue(deletes[0].endswith("/endpoints/e9") and deletes[1].endswith("/templates/t9"))


class TestEndpoints(unittest.TestCase):
    def test_creates_with_at_least_one_worker_and_scale_to_zero(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", {"workers_max": 2, "idle_timeout": 30, "data_centers": ["EU-RO-1"]}, http)
        self.assertEqual(p._ensure_endpoint("k", ctx(), "tpl1")["id"], "ep1")
        (_, _, body), = http.calls_matching("POST", r"/endpoints$")
        self.assertEqual(body["templateId"], "tpl1")
        self.assertEqual((body["workersMin"], body["workersMax"], body["idleTimeout"]), (0, 2, 30))
        self.assertEqual(body["gpuTypeIds"], ["NVIDIA RTX A5000"])
        self.assertEqual(body["dataCenterIds"], ["EU-RO-1"])

    def test_omits_data_centers_when_unset(self):
        http = FakeHTTP(); empty_account(http)
        provider("llm", http=http)._ensure_endpoint("k", ctx(), "tpl1")
        (_, _, body), = http.calls_matching("POST", r"/endpoints$")
        self.assertNotIn("dataCenterIds", body)

    def test_reuses_matching_endpoint(self):
        http = FakeHTTP()
        http.add("GET", r"/endpoints$", FakeResponse(200, [
            {"id": "e1", "name": "zotero-rag-llm", "templateId": "tpl1", "gpuTypeIds": ["NVIDIA RTX A5000"]}]))
        self.assertEqual(provider("llm", http=http)._ensure_endpoint("k", ctx(), "tpl1")["id"], "e1")
        self.assertEqual(http.calls_matching("POST", r"."), [])

    def test_gpu_mismatch_warns_and_keeps_unless_recreate(self):
        existing = {"id": "e1", "name": "zotero-rag-llm", "templateId": "tpl1", "gpuTypeIds": ["NVIDIA A100"]}
        http = FakeHTTP({("GET", r"/endpoints$"): FakeResponse(200, [existing])})
        with self.assertLogs(runpod_module.logger, level="WARNING"):
            self.assertEqual(provider("llm", http=http)._ensure_endpoint("k", ctx(), "tpl1")["id"], "e1")
        empty_account(http := FakeHTTP())
        http.add("GET", r"/endpoints$", FakeResponse(200, [existing]))
        http.add("DELETE", r"/endpoints/e1$", FakeResponse(204, None))
        self.assertEqual(provider("llm", http=http)._ensure_endpoint("k", ctx(recreate=True), "tpl1")["id"], "ep1")
        self.assertTrue(http.calls_matching("DELETE", r"/endpoints/e1$"))

    def test_recreate_replaces_an_endpoint_whose_gpus_are_not_reported(self):
        empty_account(http := FakeHTTP())
        http.add("GET", r"/endpoints$", FakeResponse(200, [{"id": "e1", "name": "zotero-rag-llm", "templateId": "tpl1"}]))
        http.add("DELETE", r"/endpoints/e1$", FakeResponse(204, None))
        provider("llm", http=http)._ensure_endpoint("k", ctx(recreate=True), "tpl1")
        self.assertTrue(http.calls_matching("DELETE", r"/endpoints/e1$"))


class TestWarmUp(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self._p = patch.object(runpod_module, "_sleep", self.sleeps.append)
        self._p.start()
        self.addCleanup(self._p.stop)

    def warm(self, p, **kw):
        messages = []
        p._warm_up("k", ctx(p.side), endpoint_base_url("ep1"), "ep1", messages.append)
        return messages

    def test_embedding_posts_to_embeddings_path_with_the_model(self):
        http = FakeHTTP({("POST", r"/embeddings$"): FakeResponse(200, {})})
        p = provider("embedding", http=http)
        self.warm(p)
        (_, url, body), = http.calls
        self.assertEqual(url, "https://api.runpod.ai/v2/ep1/openai/v1/embeddings")
        self.assertEqual(body, {"model": p.model, "input": "ping"})

    def test_llm_posts_a_one_token_chat_completion(self):
        http = FakeHTTP({("POST", r"/chat/completions$"): FakeResponse(200, {})})
        self.warm(provider("llm", http=http))
        self.assertEqual(http.calls[0][2]["max_tokens"], 1)

    def test_retries_a_failure_then_succeeds_purging_the_queue_between_tries(self):
        http = FakeHTTP({
            ("POST", r"/chat/completions$"): [FakeResponse(503, None, text="cold"), FakeResponse(200, {})],
            ("POST", r"/purge-queue$"): FakeResponse(200, {}),
        })
        messages = self.warm(provider("llm", http=http))
        self.assertEqual(len(http.calls_matching("POST", r"/chat/completions$")), 2)
        self.assertEqual(len(http.calls_matching("POST", r"/purge-queue$")), 1)
        self.assertEqual(self.sleeps, [runpod_module.WARMUP_RETRY_INTERVAL_SECONDS])
        self.assertEqual(len(messages), 2)

    def test_retries_a_connection_error(self):
        http = FakeHTTP({
            ("POST", r"/chat/completions$"): [ConnectionError("refused"), FakeResponse(200, {})],
            ("POST", r"/purge-queue$"): FakeResponse(200, {}),
        })
        self.warm(provider("llm", http=http))
        self.assertEqual(len(http.calls_matching("POST", r"/chat/completions$")), 2)

    def test_non_retryable_4xx_stops_at_once_but_429_is_retried(self):
        http = FakeHTTP({("POST", r"/chat/completions$"): FakeResponse(401, None, text="bad key")})
        with self.assertLogs(runpod_module.logger, level="WARNING"):
            self.warm(provider("llm", http=http))
        self.assertEqual(len(http.calls), 1)
        http = FakeHTTP({
            ("POST", r"/chat/completions$"): [FakeResponse(429, None, text="slow down"), FakeResponse(200, {})],
            ("POST", r"/purge-queue$"): FakeResponse(200, {}),
        })
        self.warm(provider("llm", http=http))
        self.assertEqual(len(http.calls_matching("POST", r"/chat/completions$")), 2)

    def test_gives_up_after_the_time_budget_and_warns_without_raising(self):
        clock = iter([0.0, 0.0, 100.0, 100.0, 200.0, 200.0, 300.0, 300.0])
        http = FakeHTTP({("POST", r"/chat/completions$"): FakeResponse(503, None, text="cold"),
                         ("POST", r"/purge-queue$"): FakeResponse(200, {})})
        with patch.object(runpod_module, "_monotonic", lambda: next(clock)):
            with self.assertLogs(runpod_module.logger, level="WARNING") as logs:
                self.warm(provider("llm", http=http))
        self.assertIn("did not warm up", logs.output[0])

    def test_a_failing_purge_does_not_break_the_loop(self):
        http = FakeHTTP({
            ("POST", r"/chat/completions$"): [FakeResponse(503, None, text="cold"), FakeResponse(200, {})],
            ("POST", r"/purge-queue$"): ConnectionError("down"),
        })
        self.warm(provider("llm", http=http))
        self.assertEqual(len(http.calls_matching("POST", r"/chat/completions$")), 2)

    def test_deadline_stops_the_warm_up(self):
        http = FakeHTTP({("POST", r"/chat/completions$"): FakeResponse(503, None, text="cold")})
        past = ctx("llm", deadline=-1.0)
        with self.assertRaises(ProvisionTimeout):
            provider("llm", http=http)._warm_up("k", past, endpoint_base_url("ep1"), "ep1", lambda m: None)


class TestProvisionAndTeardown(unittest.TestCase):
    def test_provision_creates_both_resources_and_returns_the_shared_url(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        messages = []
        result = p.provision(ctx("llm", skip_warmup=True), messages.append)
        self.assertEqual(result, {LLM_URL_ENV: "https://api.runpod.ai/v2/ep1/openai/v1"})
        self.assertTrue(any("ready" in m for m in messages))
        order = [c[0] + " " + c[1].rsplit("/", 1)[-1] for c in http.calls]
        self.assertLess(order.index("POST templates"), order.index("POST endpoints"))

    def test_provision_is_idempotent_a_second_run_creates_nothing(self):
        http = FakeHTTP()
        p = provider("llm", http=http)
        http.add("GET", r"/templates$", FakeResponse(200, [
            {"id": "tpl1", "name": "zotero-rag-llm", "imageName": runpod_module.DEFAULT_IMAGE, "env": p.template_env()}]))
        http.add("GET", r"/endpoints$", FakeResponse(200, [
            {"id": "ep1", "name": "zotero-rag-llm", "templateId": "tpl1", "gpuTypeIds": ["NVIDIA RTX A5000"]}]))
        result = p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        self.assertEqual(result, {LLM_URL_ENV: "https://api.runpod.ai/v2/ep1/openai/v1"})
        self.assertEqual(http.calls_matching("POST", r"."), [])

    def test_provision_returns_nothing_to_store_without_a_shared_url_field(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        p.side_config.model_kwargs.pop("shared_base_url_env")
        self.assertEqual(p.provision(ctx("llm", skip_warmup=True), lambda m: None), {})

    def test_provision_without_a_key_raises(self):
        with self.assertRaises(ProvisionError):
            provider("llm").provision(ctx("llm", credential=None), lambda m: None)

    def test_api_errors_become_provision_errors(self):
        http = FakeHTTP({("GET", r"/templates$"): FakeResponse(401, None, text="invalid API key")})
        with self.assertRaisesRegex(ProvisionError, "401"):
            provider("llm", http=http).provision(ctx("llm", skip_warmup=True), lambda m: None)

    def test_a_204_with_no_body_is_fine(self):
        http = FakeHTTP({("DELETE", r"/endpoints/e1$"): FakeResponse(204, None)})
        self.assertIsNone(provider("llm", http=http)._request("k", "DELETE", "/endpoints/e1"))

    def test_teardown_deletes_this_sides_endpoint_then_template(self):
        http = FakeHTTP({
            ("GET", r"/endpoints$"): FakeResponse(200, [{"id": "e1", "name": "zotero-rag-llm"},
                                                         {"id": "e2", "name": "zotero-rag-embedding"}]),
            ("GET", r"/templates$"): FakeResponse(200, [{"id": "t1", "name": "zotero-rag-llm"}]),
            ("DELETE", r"/endpoints/e1$"): FakeResponse(204, None),
            ("DELETE", r"/templates/t1$"): FakeResponse(204, None),
        })
        provider("llm", http=http).teardown(ctx("llm"))
        self.assertEqual([c[1].rsplit("/", 2)[-2:] for c in http.calls if c[0] == "DELETE"],
                         [["endpoints", "e1"], ["templates", "t1"]])

    def test_teardown_of_absent_resources_is_a_no_op(self):
        http = FakeHTTP({("GET", r"/endpoints$"): FakeResponse(200, []), ("GET", r"/templates$"): FakeResponse(200, [])})
        provider("llm", http=http).teardown(ctx("llm"))
        self.assertEqual(http.calls_matching("DELETE", r"."), [])


class TestPauseResume(unittest.TestCase):
    def _live(self, workers_max):
        http = FakeHTTP()
        p = provider("llm", http=http)
        http.add("GET", r"/templates$", FakeResponse(200, [
            {"id": "tpl1", "name": "zotero-rag-llm", "imageName": runpod_module.DEFAULT_IMAGE, "env": p.template_env()}]))
        http.add("GET", r"/endpoints$", FakeResponse(200, [
            {"id": "ep1", "name": "zotero-rag-llm", "templateId": "tpl1", "gpuTypeIds": ["NVIDIA RTX A5000"],
             "workersMax": workers_max}]))
        http.add("PATCH", r"/endpoints/ep1$", FakeResponse(200, {"id": "ep1", "workersMax": 1}))
        return p, http

    def test_suspend_sets_workers_max_to_zero(self):
        p, http = self._live(1)
        msgs = []
        p.suspend(ctx("llm"), msgs.append)
        (_, _, body), = http.calls_matching("PATCH", r"/endpoints/ep1$")
        self.assertEqual(body, {"workersMax": 0})
        self.assertTrue(msgs)

    def test_suspend_without_endpoint_raises(self):
        http = FakeHTTP()
        http.add("GET", r"/endpoints$", FakeResponse(200, []))
        p = provider("llm", http=http)
        with self.assertRaises(ProvisionError):
            p.suspend(ctx("llm"), lambda m: None)

    def test_provision_resumes_a_paused_endpoint(self):
        p, http = self._live(0)
        p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        (_, _, body), = http.calls_matching("PATCH", r"/endpoints/ep1$")
        self.assertEqual(body, {"workersMax": 1})

    def test_provision_leaves_a_running_endpoint_alone(self):
        p, http = self._live(1)
        p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        self.assertEqual(http.calls_matching("PATCH", r"/endpoints/ep1$"), [])


class TestHealth(unittest.TestCase):
    URL = "https://api.runpod.ai/v2/abc123/openai/v1"

    def check(self, response):
        http = FakeHTTP({("GET", r"/health$"): response})
        return provider(http=http).health(Credentials(api_key="k", base_url=self.URL)), http

    def test_ready_with_ready_or_running_workers(self):
        for workers in ({"ready": 1}, {"running": 2}):
            h, http = self.check(FakeResponse(200, {"workers": workers, "jobs": {}}))
            self.assertEqual(h.status, "ready")
        self.assertEqual(http.calls[0][1], "https://api.runpod.ai/v2/abc123/health")

    def test_cold_when_scaled_to_zero_or_initializing(self):
        self.assertEqual(self.check(FakeResponse(200, {"workers": {}, "jobs": {}}))[0].status, "cold")
        h = self.check(FakeResponse(200, {"workers": {"initializing": 1}}))[0]
        self.assertEqual((h.status, h.detail), ("cold", "1 worker(s) initializing"))

    def test_throttled_when_runpod_has_no_capacity(self):
        h = self.check(FakeResponse(200, {"workers": {"throttled": 1}, "jobs": {"inQueue": 4}}))[0]
        self.assertEqual(h.status, "throttled")
        self.assertIn("4 job(s) queued", h.detail)

    def test_initializing_wins_over_a_queue_when_not_throttled(self):
        h = self.check(FakeResponse(200, {"workers": {"initializing": 1}, "jobs": {"inQueue": 3}}))[0]
        self.assertEqual(h.status, "cold")

    def test_errors_are_unreachable(self):
        h = self.check(FakeResponse(403, {}))[0]
        self.assertEqual(h.status, "unreachable")
        self.assertIn("no access to endpoint abc123", h.detail)
        self.assertEqual(self.check(FakeResponse(500, {}))[0].detail, "HTTP 500")
        self.assertEqual(self.check(TimeoutError("timed out"))[0].status, "unreachable")

    def test_malformed_or_missing_base_url_is_unreachable(self):
        p = provider(http=FakeHTTP())
        self.assertIn("Not a RunPod endpoint URL", p.health(Credentials(api_key="k", base_url="https://x.example")).detail)
        self.assertEqual(p.health(Credentials(api_key="k")).detail, "not provisioned")  # a key but no endpoint
        self.assertEqual(p.health(Credentials()).detail, "not configured")


class TestDefaultsAndErrors(unittest.TestCase):
    def test_apply_defaults_adds_patterns_for_the_shared_scope_and_is_idempotent(self):
        p = provider("llm")
        p.apply_defaults(); p.apply_defaults()
        kw = p.side_config.model_kwargs
        self.assertEqual(kw["shared_api_key_pattern"], r"^rpa_[A-Za-z0-9]+$")
        self.assertIn(r"api\.runpod\.ai", kw["shared_base_url_pattern"])
        self.assertEqual(kw["key_docs_urls"], {"RUNPOD_API_KEY": "https://www.runpod.io/console/user/settings"})

    def test_apply_defaults_uses_api_key_pattern_for_user_scope(self):
        p = get_providers(_user_preset())["llm"]
        p.apply_defaults()
        self.assertEqual(p.side_config.model_kwargs["api_key_pattern"], r"^rpa_[A-Za-z0-9]+$")
        self.assertNotIn("shared_api_key_pattern", p.side_config.model_kwargs)

    def test_a_paused_endpoint_is_classified(self):
        p = provider()
        self.assertEqual(p.classify_http_error(409, '{"code":"ENDPOINT_PAUSED"}'), "paused")
        self.assertIsNone(p.classify_http_error(409, "other"))
        self.assertIsNone(p.classify_http_error(500, "ENDPOINT_PAUSED"))

    def test_describe_asks_for_the_key_and_has_a_hint(self):
        d = provider().describe()
        self.assertTrue(d.supports_provisioning)
        self.assertEqual(d.provisioning.credential.env, "RUNPOD_API_KEY")
        self.assertIn("rpa_", d.provisioning.credential.pattern)
        self.assertIn("RunPod", d.unavailable_hint)


def _user_preset():
    preset = runpod_preset(scope="user")
    for side in (preset.embedding, preset.llm):
        side.model_kwargs = {"api_key_env": "RUNPOD_API_KEY"}
    return preset


if __name__ == "__main__":
    unittest.main()
