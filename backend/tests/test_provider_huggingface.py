"""HuggingFaceProvider with a faked HTTP client (no live calls: endpoints cost money)."""

import time
import unittest
from pathlib import Path

from backend.providers import Credentials, ProviderConfigError, ProvisionContext, ProvisionError, ProvisionTimeout, get_providers
from backend.providers import huggingface as hf
from backend.tests.provider_fakes import FakeHTTP, FakeResponse
from backend.tests.test_providers_registry import make_preset

TOKEN = "hf_abc123"
EP = r"/endpoint/acme/zotero-rag-(embedding|llm)$"


def hf_preset(options=None, scope="user", llm_options=None):
    preset = make_preset()
    for side, opts in ((preset.embedding, options or {}), (preset.llm, llm_options if llm_options is not None else (options or {}))):
        side.provider.id = "huggingface"
        side.provider.scope = scope
        side.provider.options = {"namespace": "acme", **opts}
        side.model_kwargs = {"api_key_env": "HF_TOKEN"} if scope == "user" else {"shared_api_key_env": "HF_TOKEN"}
    preset.embedding.model_name = "intfloat/multilingual-e5-large-instruct"
    preset.embedding.batch_size = 64
    preset.llm.model_names = ["Qwen/Qwen2.5-7B-Instruct"]
    return preset


def provider(side="llm", options=None, http=None, scope="user"):
    p = get_providers(hf_preset(options, scope))[side]
    p._http_client = http or FakeHTTP()
    return p


def ctx(side="llm", **kw):
    return ProvisionContext(side=side, preset=hf_preset(), credential=kw.pop("credential", TOKEN), data_path=Path("."), **kw)


def endpoint(state, side="llm", url="https://ep.endpoints.huggingface.cloud", **status):
    repo = "intfloat/multilingual-e5-large-instruct" if side == "embedding" else "Qwen/Qwen2.5-7B-Instruct"
    instance = "nvidia-t4" if side == "embedding" else "nvidia-a10g"
    return {"name": f"zotero-rag-{side}", "model": {"repository": repo}, "compute": {"instanceType": instance},
            "provider": {"region": "eu-west-1"}, "status": {"state": state, "url": url, **status}}


class NoWait:
    """Make the polling loops instantaneous and clock-driven."""

    def setUp(self):
        self.now = 0.0
        self._old = (hf._sleep, hf._monotonic)
        hf._sleep = lambda s: setattr(self, "now", self.now + s)
        hf._monotonic = lambda: self.now
        self.addCleanup(lambda: setattr(hf, "_sleep", self._old[0]))
        self.addCleanup(lambda: setattr(hf, "_monotonic", self._old[1]))


class TestHealth(unittest.TestCase):
    def check(self, state, expected, **status):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint(state, **status)))
        h = provider(http=http).health(Credentials(api_key=TOKEN))
        self.assertEqual(h.status, expected, state)
        return h

    def test_every_state_maps_to_a_vendor_neutral_status(self):
        for state, expected in [("running", "ready"), ("scaledToZero", "cold"), ("pending", "cold"),
                                ("initializing", "cold"), ("updating", "cold"), ("paused", "paused"),
                                ("failed", "unreachable"), ("updateFailed", "unreachable"), ("weird", "unreachable")]:
            self.check(state, expected)

    def test_failure_message_is_shown_and_scale_to_zero_is_explained(self):
        self.assertIn("out of memory", self.check("failed", "unreachable", message="out of memory").detail)
        self.assertIn("scaled to zero", self.check("scaledToZero", "cold").detail)

    def test_absent_endpoint_is_not_provisioned(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(404, {"error": "not found"}))
        h = provider(http=http).health(Credentials(api_key=TOKEN))
        self.assertEqual((h.status, h.detail), ("unreachable", "not provisioned"))

    def test_api_errors_and_missing_key_never_raise(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(500, {"error": "x"}))
        self.assertEqual(provider(http=http).health(Credentials(api_key=TOKEN)).status, "unreachable")
        self.assertEqual(provider().health(Credentials()).detail, "not configured")


class TestEndpointUrl(unittest.TestCase):
    def test_found_adds_v1_and_works_while_paused(self):
        for state in ("running", "paused", "scaledToZero"):
            http = FakeHTTP()
            http.add("GET", EP, FakeResponse(200, endpoint(state, url="https://x.endpoints.huggingface.cloud/")))
            self.assertEqual(provider(http=http).endpoint_url(TOKEN), "https://x.endpoints.huggingface.cloud/v1")

    def test_absent_or_failing_is_none(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(404, {}))
        self.assertIsNone(provider(http=http).endpoint_url(TOKEN))
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(401, {}))
        self.assertIsNone(provider(http=http).endpoint_url(TOKEN))

    def test_namespace_defaults_to_the_token_owner(self):
        http = FakeHTTP()
        http.add("GET", r"whoami-v2$", FakeResponse(200, {"name": "alice"}))
        http.add("GET", r"/endpoint/alice/zotero-rag-llm$", FakeResponse(200, endpoint("running")))
        p = get_providers(hf_preset({"namespace": None}))["llm"]
        p._http_client = http
        self.assertTrue(p.endpoint_url(TOKEN))
        self.assertEqual(len(http.calls_matching("GET", "whoami")), 1)
        p.endpoint_url(TOKEN)
        self.assertEqual(len(http.calls_matching("GET", "whoami")), 1)  # cached per token


class TestClassify(unittest.TestCase):
    def test_paused_400_cold_503_and_the_rest(self):
        p = provider()
        self.assertEqual(p.classify_http_error(400, '{"error":"Bad Request: The endpoint is paused, ask a maintainer to restart it"}'), "paused")
        self.assertEqual(p.classify_http_error(503, '{"code":"SERVICE_UNAVAILABLE"}'), "cold")
        self.assertIsNone(p.classify_http_error(400, "context length exceeded"))
        self.assertIsNone(p.classify_http_error(401, "unauthorized"))  # a wrong token never wakes anything


class TestProvision(NoWait, unittest.TestCase):
    def setUp(self):
        NoWait.setUp(self)
        self.msgs = []

    def run_provision(self, side, http, **kw):
        p = provider(side, http=http)
        return p.provision(ctx(side, **kw), self.msgs.append)

    def test_absent_creates_waits_warms_up_and_returns_nothing_to_store(self):
        http = FakeHTTP()
        states = [FakeResponse(404, {}), FakeResponse(200, endpoint("initializing")), FakeResponse(200, endpoint("running"))]
        http.add("GET", EP, states)
        http.add("POST", r"/endpoint/acme$", FakeResponse(200, {}))
        http.add("POST", r"/v1/chat/completions$", FakeResponse(200, {"choices": []}))
        self.assertEqual(self.run_provision("llm", http), {})
        (_, _, body), = http.calls_matching("POST", r"/endpoint/acme$")
        self.assertEqual((body["name"], body["type"]), ("zotero-rag-llm", "authenticated"))
        self.assertEqual(body["compute"]["scaling"], {"minReplica": 0, "maxReplica": 1, "scaleToZeroTimeout": 15})
        self.assertEqual(body["compute"]["instanceType"], "nvidia-a10g")
        self.assertIn("vLLM", body["model"]["image"])
        self.assertTrue(any("Creating" in m for m in self.msgs))
        self.assertTrue(any("initializing" in m for m in self.msgs))
        self.assertTrue(any("Warming up" in m for m in self.msgs))
        self.assertEqual(len(http.calls_matching("POST", "chat/completions")), 1)

    def test_embedding_payload_uses_tei_with_a_raised_client_batch_size(self):
        http = FakeHTTP()
        http.add("GET", EP, [FakeResponse(404, {}), FakeResponse(200, endpoint("running", "embedding"))])
        http.add("POST", r"/endpoint/acme$", FakeResponse(200, {}))
        http.add("POST", r"/v1/embeddings$", FakeResponse(200, {"data": []}))
        self.run_provision("embedding", http)
        (_, _, body), = http.calls_matching("POST", r"/endpoint/acme$")
        self.assertEqual(body["model"]["task"], "sentence-embeddings")
        self.assertEqual(body["model"]["image"]["tei"]["url"], "ghcr.io/huggingface/text-embeddings-inference:turing-1.8")
        self.assertEqual(body["model"]["env"], {"MAX_CLIENT_BATCH_SIZE": "128"})
        self.assertEqual(body["compute"]["instanceType"], "nvidia-t4")

    def test_batch_size_above_the_floor_is_honoured(self):
        preset = hf_preset()
        preset.embedding.batch_size = 256
        p = get_providers(preset)["embedding"]
        self.assertEqual(p._env(), {"MAX_CLIENT_BATCH_SIZE": "256"})

    def test_paused_and_scaled_to_zero_are_resumed_running_is_left_alone(self):
        for state, resumes in (("paused", True), ("scaledToZero", True), ("running", False)):
            http = FakeHTTP()
            http.add("GET", EP, [FakeResponse(200, endpoint(state)), FakeResponse(200, endpoint("running"))])
            http.add("POST", r"/resume$", FakeResponse(200, {}))
            http.add("POST", r"/chat/completions$", FakeResponse(200, {}))
            self.run_provision("llm", http)
            self.assertEqual(len(http.calls_matching("POST", "/resume")), 1 if resumes else 0, state)
            self.assertEqual(http.calls_matching("POST", r"/endpoint/acme$"), [])

    def test_failed_endpoint_is_reported_with_its_message(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint("failed", message="bad image")))
        with self.assertRaisesRegex(ProvisionError, "bad image"):
            self.run_provision("llm", http)

    def test_a_start_that_fails_while_waiting_is_reported(self):
        http = FakeHTTP()
        http.add("GET", EP, [FakeResponse(404, {}), FakeResponse(200, endpoint("initializing")),
                             FakeResponse(200, endpoint("failed", message="quota"))])
        http.add("POST", r"/endpoint/acme$", FakeResponse(200, {}))
        with self.assertRaisesRegex(ProvisionError, "failed to start: quota"):
            self.run_provision("llm", http)

    def test_missing_payment_method_gets_a_clear_message(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(404, {}))
        http.add("POST", r"/endpoint/acme$", FakeResponse(403, {"error": "Payment method required for namespace acme"}))
        with self.assertRaisesRegex(ProvisionError, "payment method"):
            self.run_provision("llm", http)

    def test_a_differing_config_is_kept_unless_recreate_which_deletes_first(self):
        other = endpoint("running")
        other["compute"]["instanceType"] = "nvidia-l4"
        http = FakeHTTP()
        http.add("GET", EP, [FakeResponse(200, other), FakeResponse(200, other), FakeResponse(200, endpoint("running"))])
        http.add("POST", r"/chat/completions$", FakeResponse(200, {}))
        self.run_provision("llm", http)
        self.assertEqual(http.calls_matching("DELETE", EP), [])

        http = FakeHTTP()
        http.add("GET", EP, [FakeResponse(200, other), FakeResponse(404, {}), FakeResponse(200, endpoint("running"))])
        http.add("DELETE", EP, FakeResponse(202, {}))
        http.add("POST", r"/endpoint/acme$", FakeResponse(200, {}))
        http.add("POST", r"/chat/completions$", FakeResponse(200, {}))
        self.run_provision("llm", http, recreate=True)
        self.assertEqual(len(http.calls_matching("DELETE", EP)), 1)
        self.assertEqual(len(http.calls_matching("POST", r"/endpoint/acme$")), 1)

    def test_deadline_and_wait_bound_are_respected(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint("initializing")))
        with self.assertRaisesRegex(ProvisionError, "did not start within"):
            self.run_provision("llm", http)
        c = ctx("llm")
        c.deadline = time.monotonic() - 1  # ProvisionContext uses the real clock
        with self.assertRaises(ProvisionTimeout):
            provider(http=http).provision(c, lambda m: None)

    def test_skip_warmup_and_a_failing_warmup_do_not_fail_the_job(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint("running")))
        self.run_provision("llm", http, skip_warmup=True)
        self.assertEqual(http.calls_matching("POST", "chat/completions"), [])
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint("running")))
        http.add("POST", r"/chat/completions$", FakeResponse(500, {"error": "x"}))
        self.assertEqual(self.run_provision("llm", http), {})  # gives up after the bound, does not raise

    def test_no_token_is_an_error(self):
        with self.assertRaisesRegex(ProvisionError, "token"):
            provider().provision(ctx("llm", credential=None), lambda m: None)


class TestSuspendAndTeardown(unittest.TestCase):
    def test_pause_is_idempotent(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint("running")))
        http.add("POST", r"/pause$", FakeResponse(200, {}))
        provider(http=http).suspend(ctx("llm"), lambda m: None)
        self.assertEqual(len(http.calls_matching("POST", "/pause")), 1)
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(200, endpoint("paused")))
        provider(http=http).suspend(ctx("llm"), lambda m: None)
        self.assertEqual(http.calls_matching("POST", "/pause"), [])

    def test_pausing_an_absent_endpoint_is_an_error(self):
        http = FakeHTTP()
        http.add("GET", EP, FakeResponse(404, {}))
        with self.assertRaises(ProvisionError):
            provider(http=http).suspend(ctx("llm"), lambda m: None)

    def test_teardown_deletes_and_an_absent_endpoint_is_a_no_op(self):
        http = FakeHTTP()
        http.add("DELETE", EP, FakeResponse(202, {}))
        provider(http=http).teardown(ctx("llm"))
        self.assertEqual(len(http.calls_matching("DELETE", EP)), 1)
        http = FakeHTTP()
        http.add("DELETE", EP, FakeResponse(404, {}))
        provider(http=http).teardown(ctx("llm"))


class TestConfigAndDescription(unittest.TestCase):
    def test_apply_defaults_adds_the_token_pattern_for_the_scope(self):
        preset = hf_preset()
        for p in get_providers(preset).values():
            p.apply_defaults()
        self.assertEqual(preset.llm.model_kwargs["api_key_pattern"], hf.TOKEN_PATTERN)
        managed = hf_preset(scope="managed")
        for p in get_providers(managed).values():
            p.apply_defaults()
        self.assertEqual(managed.llm.model_kwargs["shared_api_key_pattern"], hf.TOKEN_PATTERN)
        self.assertEqual(preset.llm.model_kwargs["key_docs_urls"]["HF_TOKEN"], hf.TOKENS_PAGE)

    def test_descriptor_declares_the_token_pause_and_a_hint(self):
        d = provider().describe()
        self.assertEqual((d.id, d.supports_provisioning, d.supports_suspend), ("huggingface", True, True))
        self.assertEqual(d.provisioning.credential.env, "HF_TOKEN")
        self.assertIn("billing", d.provisioning.credential.help)
        self.assertIn("several minutes", d.provisioning.hint)

    def test_unknown_tei_instance_needs_an_explicit_image(self):
        with self.assertRaisesRegex(ProviderConfigError, "image"):
            get_providers(hf_preset({"instance": "nvidia-h100"}, llm_options={}))
        ok = get_providers(hf_preset({"instance": "nvidia-h100", "image": "example/tei:1"}, llm_options={}))["embedding"]
        self.assertEqual(ok._image()["tei"]["url"], "example/tei:1")

    def test_scale_to_zero_minimum_is_enforced(self):
        with self.assertRaises(ProviderConfigError):
            get_providers(hf_preset({"scale_to_zero_timeout_min": 10}))

    def test_bundled_preset_loads_with_a_provider_on_each_side(self):
        import json
        from backend.config.presets import HardwarePreset
        raw = json.loads((Path(__file__).resolve().parents[1] / "config" / "default_presets" / "huggingface.json").read_text())
        providers = get_providers(HardwarePreset(name="huggingface", **raw))
        self.assertEqual({p.id for p in providers.values()}, {"huggingface"})
        self.assertEqual((providers["embedding"].engine, providers["llm"].engine), ("tei", "vllm"))


if __name__ == "__main__":
    unittest.main()
