"""RunPodProvider: ported from the former provisioning script's tests, per side."""

import unittest
from unittest.mock import patch

from backend.plugins.runpod import provider as runpod_module
from backend.plugins.runpod.provider import endpoint_base_url
from backend.plugins.runpod.testing import LLM_URL_ENV, ctx, provider, runpod_preset
from backend.providers import Credentials, ProvisionError, ProvisionTimeout, get_providers
from backend.providers.testing import FakeHTTP, FakeResponse

#: GET /v2/catalog/gpus: two pools of the real catalog (abridged).
CATALOG = {"gpus": [
    {"id": "NVIDIA GeForce RTX 3090", "name": "RTX 3090", "pool": "AMPERE_24"},
    {"id": "NVIDIA L4", "name": "L4", "pool": "AMPERE_24"},
    {"id": "NVIDIA RTX A5000", "name": "RTX A5000", "pool": "AMPERE_24"},
    {"id": "NVIDIA A40", "name": "A40", "pool": "AMPERE_48"},
    {"id": "NVIDIA RTX A6000", "name": "RTX A6000", "pool": "AMPERE_48"},
    {"id": "NVIDIA B200", "name": "B200", "pool": None},
]}
#: The ``gpu`` of an endpoint on an RTX A5000 only (as RunPod migrated the v1 ones).
A5000 = {"pools": ["AMPERE_24"], "excludedTypes": ["NVIDIA GeForce RTX 3090", "NVIDIA L4"], "count": 1}


def listing(key, *items, next_cursor=None):
    """One page of a v2 listing: ``{key: [...], "pagination": {...}}``."""
    return FakeResponse(200, {key: list(items), "pagination": {"nextCursor": next_cursor, "hasNextPage": next_cursor is not None}})


def endpoint(p, id="ep1", workers_max=1, **over):
    """An endpoint as GET /v2/serverless reports it, matching provider ``p``'s settings."""
    body = {"id": id, "name": p.resource_name, "type": "QUEUE", "image": runpod_module.DEFAULT_IMAGE,
            "env": p.worker_env(), "disk": runpod_module.CONTAINER_DISK_GB[p.side], "gpu": dict(A5000),
            "workers": {"min": 0, "max": workers_max, "idleTimeout": 60}}
    body.update(over)
    return body


def empty_account(http, **extra):
    http.add("GET", r"/v2/serverless$", listing("endpoints"))
    http.add("GET", r"/v2/templates$", listing("templates"))
    http.add("GET", r"/v2/catalog/gpus$", FakeResponse(200, CATALOG))
    http.add("POST", r"/v2/serverless$", FakeResponse(201, {"id": "ep1", "name": "n"}))
    http.add("POST", r"/purge-queue$", FakeResponse(200, {}))
    for (m, pat), resp in extra.items():
        http.add(m, pat, resp)


class TestNames(unittest.TestCase):
    def test_resource_names_and_model_per_side(self):
        emb, llm = provider("embedding"), provider("llm")
        self.assertEqual((emb.resource_name, llm.resource_name), ("zotero-rag-embedding", "zotero-rag-llm"))
        self.assertEqual(emb.model, "intfloat/multilingual-e5-large-instruct")
        self.assertEqual(llm.model, "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(emb.worker_env(), {"MODEL_NAME": emb.model, "MAX_MODEL_LEN": "512"})
        self.assertEqual(llm.worker_env(), {"MODEL_NAME": llm.model})

    def test_name_option_renames_the_endpoint(self):
        """A second preset on the same account (e.g. a larger LLM) needs its own endpoint."""
        llm = provider("llm", options={"name": "zotero-rag-llm-gemma"})
        self.assertEqual(llm.resource_name, "zotero-rag-llm-gemma")

    def test_max_model_len_option_reaches_the_llm_worker(self):
        """Without it vLLM sizes the KV cache for the model's full context, which does
        not fit a 31B model on a 48 GB GPU."""
        llm = provider("llm", options={"max_model_len": 16384})
        self.assertEqual(llm.worker_env(), {"MODEL_NAME": llm.model, "MAX_MODEL_LEN": "16384"})

    def test_options_defaults_and_validation(self):
        o = provider().options
        self.assertEqual((o.gpu, o.workers_max, o.idle_timeout, o.data_centers), ("NVIDIA RTX A5000", 1, 60, None))
        from backend.providers import ProviderConfigError
        for bad in ({"workers_max": 0}, {"idle_timeout": 0}, {"gpus": "x"}):
            with self.assertRaises(ProviderConfigError):
                get_providers(runpod_preset(bad))


class TestGpu(unittest.TestCase):
    def spec(self, gpu):
        http = FakeHTTP({("GET", r"/v2/catalog/gpus$"): FakeResponse(200, CATALOG)})
        return provider("llm", {"gpu": gpu}, http)._gpu_spec("k")

    def test_a_gpu_type_is_its_pool_with_the_pools_other_types_excluded(self):
        self.assertEqual(self.spec("NVIDIA RTX A5000"), A5000)
        self.assertEqual(self.spec("NVIDIA RTX A6000"), {"pools": ["AMPERE_48"], "excludedTypes": ["NVIDIA A40"], "count": 1})

    def test_the_catalogs_short_name_works_too(self):
        self.assertEqual(self.spec("RTX A5000"), A5000)

    def test_an_unknown_or_poolless_gpu_type_is_a_provision_error_naming_it(self):
        for gpu in ("NVIDIA RTX 9999", "NVIDIA B200"):
            with self.assertRaisesRegex(ProvisionError, gpu):
                self.spec(gpu)


class TestEndpoints(unittest.TestCase):
    def test_creates_the_endpoint_with_its_worker_settings_inline(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", {"workers_max": 2, "idle_timeout": 30, "data_centers": ["EU-RO-1"]}, http)
        self.assertEqual(p._ensure_endpoint("k", ctx())["id"], "ep1")
        (_, _, body), = http.calls_matching("POST", r"/v2/serverless$")
        self.assertEqual(body, {
            "name": "zotero-rag-llm", "type": "QUEUE", "image": runpod_module.DEFAULT_IMAGE,
            "env": {"MODEL_NAME": "Qwen/Qwen2.5-7B-Instruct"}, "disk": 40, "gpu": A5000,
            # Scale to zero: no worker runs (or bills) until a request arrives.
            "workers": {"min": 0, "max": 2, "idleTimeout": 30},
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4}, "flashboot": "FLASHBOOT",
            "dataCenterIds": ["EU-RO-1"],
        })
        self.assertEqual(http.calls_matching("POST", r"/templates"), [])

    def test_omits_data_centers_when_unset(self):
        http = FakeHTTP(); empty_account(http)
        provider("embedding", http=http)._ensure_endpoint("k", ctx("embedding"))
        (_, _, body), = http.calls_matching("POST", r"/v2/serverless$")
        self.assertNotIn("dataCenterIds", body)
        self.assertEqual(body["disk"], 20)

    def test_reuses_matching_endpoint(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        http.add("GET", r"/v2/serverless$", listing("endpoints", endpoint(p, "e1")))
        self.assertEqual(p._ensure_endpoint("k", ctx())["id"], "e1")
        self.assertEqual([c for c in http.calls if c[0] != "GET"], [])

    def test_finds_an_endpoint_on_a_later_page(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        http.add("GET", r"/v2/serverless$", listing("endpoints", {"id": "x", "name": "other"}, next_cursor="c2"))
        http.add("GET", r"/v2/serverless\?cursor=c2$", listing("endpoints", endpoint(p, "e2")))
        self.assertEqual(p._ensure_endpoint("k", ctx())["id"], "e2")

    def test_a_differing_endpoint_is_kept_with_a_warning_unless_recreate(self):
        for differs in ({"image": "old:image"}, {"env": {"MODEL_NAME": "other"}},
                        {"gpu": {"pools": ["AMPERE_48"], "excludedTypes": [], "count": 1}},
                        {"gpu": {"pools": ["AMPERE_24"], "excludedTypes": ["NVIDIA RTX A5000"], "count": 1}}):
            http = FakeHTTP(); empty_account(http)
            p = provider("llm", http=http)
            http.add("GET", r"/v2/serverless$", listing("endpoints", endpoint(p, "e1", **differs)))
            with self.assertLogs(runpod_module.logger, level="WARNING") as logs:
                self.assertEqual(p._ensure_endpoint("k", ctx())["id"], "e1")
            self.assertIn("differs", logs.output[0])
            self.assertEqual(http.calls_matching("DELETE", r"."), [], differs)

            http.add("DELETE", r"/v2/serverless/e1$", FakeResponse(204, None))
            self.assertEqual(p._ensure_endpoint("k", ctx(recreate=True))["id"], "ep1")
            self.assertTrue(http.calls_matching("DELETE", r"/v2/serverless/e1$"), differs)


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
    def test_provision_creates_the_endpoint_and_returns_the_shared_url(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        messages = []
        result = p.provision(ctx("llm", skip_warmup=True), messages.append)
        self.assertEqual(result, {LLM_URL_ENV: "https://api.runpod.ai/v2/ep1/openai/v1"})
        self.assertTrue(any("ready" in m for m in messages))
        self.assertEqual(len(http.calls_matching("POST", r"/v2/serverless$")), 1)

    def test_provision_is_idempotent_a_second_run_creates_nothing(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        http.add("GET", r"/v2/serverless$", listing("endpoints", endpoint(p)))
        result = p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        self.assertEqual(result, {LLM_URL_ENV: "https://api.runpod.ai/v2/ep1/openai/v1"})
        self.assertEqual([c for c in http.calls if c[0] != "GET"], [])

    def test_provision_returns_nothing_to_store_without_a_shared_url_field(self):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        p.side_config.model_kwargs.pop("shared_base_url_env")
        self.assertEqual(p.provision(ctx("llm", skip_warmup=True), lambda m: None), {})

    def test_provision_without_a_key_raises(self):
        with self.assertRaises(ProvisionError):
            provider("llm").provision(ctx("llm", credential=None), lambda m: None)

    def test_api_errors_become_provision_errors(self):
        http = FakeHTTP({("GET", r"/v2/serverless$"): FakeResponse(401, None, text="invalid API key")})
        with self.assertRaisesRegex(ProvisionError, "401"):
            provider("llm", http=http).provision(ctx("llm", skip_warmup=True), lambda m: None)

    def test_an_unreachable_api_is_a_provision_error_naming_it(self):
        http = FakeHTTP({("GET", r"/v2/serverless$"): OSError("[Errno 8] nodename nor servname provided, or not known")})
        with self.assertRaisesRegex(ProvisionError, r"Could not reach the RunPod API \(api\.runpod\.io\)"):
            provider("llm", http=http).provision(ctx("llm", skip_warmup=True), lambda m: None)

    def test_a_204_with_no_body_is_fine(self):
        http = FakeHTTP({("DELETE", r"/v2/serverless/e1$"): FakeResponse(204, None)})
        self.assertIsNone(provider("llm", http=http)._request("k", "DELETE", "/serverless/e1"))

    def test_teardown_deletes_this_sides_endpoint_and_a_leftover_template(self):
        http = FakeHTTP({
            ("GET", r"/v2/serverless$"): listing("endpoints", {"id": "e1", "name": "zotero-rag-llm"},
                                                 {"id": "e2", "name": "zotero-rag-embedding"}),
            ("GET", r"/v2/templates$"): listing("templates", {"id": "t1", "name": "zotero-rag-llm"}),
            ("DELETE", r"/v2/serverless/e1$"): FakeResponse(204, None),
            ("DELETE", r"/v2/templates/t1$"): FakeResponse(204, None),
        })
        provider("llm", http=http).teardown(ctx("llm"))
        self.assertEqual([c[1].rsplit("/", 2)[-2:] for c in http.calls if c[0] == "DELETE"],
                         [["serverless", "e1"], ["templates", "t1"]])

    def test_teardown_of_absent_resources_is_a_no_op(self):
        http = FakeHTTP({("GET", r"/v2/serverless$"): listing("endpoints"), ("GET", r"/v2/templates$"): listing("templates")})
        provider("llm", http=http).teardown(ctx("llm"))
        self.assertEqual(http.calls_matching("DELETE", r"."), [])


class TestPauseResume(unittest.TestCase):
    def _live(self, workers_max, **over):
        http = FakeHTTP(); empty_account(http)
        p = provider("llm", http=http)
        http.add("GET", r"/v2/serverless$", listing("endpoints", endpoint(p, workers_max=workers_max, **over)))
        http.add("PATCH", r"/v2/serverless/ep1$", FakeResponse(200, endpoint(p)))
        return p, http

    def test_suspend_sets_max_workers_to_zero(self):
        p, http = self._live(1)
        msgs = []
        p.suspend(ctx("llm"), msgs.append)
        (_, _, body), = http.calls_matching("PATCH", r"/v2/serverless/ep1$")
        self.assertEqual(body, {"workers": {"min": 0, "max": 0}})
        self.assertTrue(msgs)

    def test_suspend_without_endpoint_raises(self):
        http = FakeHTTP({("GET", r"/v2/serverless$"): listing("endpoints")})
        with self.assertRaises(ProvisionError):
            provider("llm", http=http).suspend(ctx("llm"), lambda m: None)

    def test_provision_resumes_a_paused_endpoint(self):
        p, http = self._live(0)
        p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        (_, _, body), = http.calls_matching("PATCH", r"/v2/serverless/ep1$")
        self.assertEqual(body, {"workers": {"min": 0, "max": 1, "idleTimeout": 60}})

    def test_provision_resumes_a_paused_endpoint_whose_config_differs(self):
        p, http = self._live(0, image="old:image")  # kept as the owner set it up, but resumed
        with self.assertLogs(runpod_module.logger, level="WARNING"):
            p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        self.assertEqual(len(http.calls_matching("PATCH", r"/v2/serverless/ep1$")), 1)
        self.assertEqual(http.calls_matching("DELETE", r"."), [])

    def test_provision_leaves_a_running_endpoint_alone(self):
        p, http = self._live(1)
        p.provision(ctx("llm", skip_warmup=True), lambda m: None)
        self.assertEqual(http.calls_matching("PATCH", r"."), [])


class TestHealth(unittest.TestCase):
    URL = "https://api.runpod.ai/v2/abc123/openai/v1"

    def check(self, response):
        http = FakeHTTP({("GET", r"/health$"): response})
        return provider(http=http).health(Credentials(api_key="k", base_url=self.URL)), http

    def test_ready_with_ready_or_running_workers(self):
        for workers, jobs in (({"ready": 1}, {}), ({"running": 2}, {}), ({"running": 1}, {"inProgress": 1, "inQueue": 2})):
            h, http = self.check(FakeResponse(200, {"workers": workers, "jobs": jobs}))
            self.assertEqual(h.status, "ready", (workers, jobs))
        self.assertEqual(http.calls[-1][1], "https://api.runpod.ai/v2/abc123/health")  # after the management check

    def test_a_running_worker_that_has_not_picked_up_the_queued_jobs_is_still_starting(self):
        """Seen live: the worker container is 'running' while the model loads and jobs wait in the queue."""
        h = self.check(FakeResponse(200, {"workers": {"running": 1, "ready": 0, "idle": 0}, "jobs": {"inQueue": 2, "inProgress": 0}}))[0]
        self.assertEqual(h.status, "cold")
        self.assertIn("2 job(s) waiting", h.detail)
        self.assertTrue(h.waking)

    def test_cold_when_scaled_to_zero_or_initializing(self):
        idle = self.check(FakeResponse(200, {"workers": {}, "jobs": {}}))[0]
        self.assertEqual((idle.status, idle.waking), ("cold", False))
        h = self.check(FakeResponse(200, {"workers": {"initializing": 1}}))[0]
        self.assertEqual((h.status, h.detail, h.waking), ("cold", "1 worker(s) initializing", True))

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


class TestPaused(unittest.TestCase):
    BASE = "https://api.runpod.ai/v2/ep1/openai/v1"

    def _http(self, workers_max):
        http = FakeHTTP()
        http.add("GET", r"/v2/serverless/ep1$", FakeResponse(200, {"id": "ep1", "workers": {"min": 0, "max": workers_max}}))
        http.add("GET", r"/v2/serverless$", listing("endpoints", {"id": "ep1", "name": "zotero-rag-llm",
                                                                  "workers": {"min": 0, "max": workers_max}}))
        http.add("GET", r"/ep1/health$", FakeResponse(200, {"workers": {"idle": 0, "ready": 1, "running": 0}, "jobs": {}}))
        return http

    def test_health_reports_paused_from_the_management_api_even_if_the_data_plane_looks_ready(self):
        h = provider("llm", http=self._http(0)).health(Credentials(api_key="k", base_url=self.BASE))
        self.assertEqual(h.status, "paused")
        self.assertEqual(provider("llm", http=self._http(1)).health(Credentials(api_key="k", base_url=self.BASE)).status, "ready")

    def test_a_key_that_cannot_read_the_management_api_falls_back_to_the_data_plane(self):
        http = FakeHTTP()
        http.add("GET", r"/v2/serverless/ep1$", FakeResponse(403, {"error": "forbidden"}))
        http.add("GET", r"/ep1/health$", FakeResponse(200, {"workers": {"ready": 1}, "jobs": {}}))
        self.assertEqual(provider("llm", http=http).health(Credentials(api_key="k", base_url=self.BASE)).status, "ready")

    def test_is_paused_follows_max_workers(self):
        self.assertTrue(provider("llm", http=self._http(0)).is_paused("k"))
        self.assertFalse(provider("llm", http=self._http(1)).is_paused("k"))
        self.assertFalse(provider("llm", http=FakeHTTP()).is_paused("k"))  # unreachable: not paused


class TestUserScopeOnly(unittest.TestCase):
    def test_managed_scope_is_rejected(self):
        """RunPod's terms do not allow sharing an account: only each user's own key."""
        from backend.providers import ProviderConfigError

        preset = runpod_preset(scope="user")
        preset.llm.provider.scope = "managed"
        preset.llm.model_kwargs = {"shared_api_key_env": "RUNPOD_API_KEY"}
        with self.assertRaisesRegex(ProviderConfigError, "does not allow scope 'managed'"):
            get_providers(preset)
