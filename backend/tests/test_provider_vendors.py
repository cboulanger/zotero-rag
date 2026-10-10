"""OpenAI, Anthropic and MPCDF providers (small vendor classes over the generic base)."""

import unittest

from backend.providers import Credentials, ProviderConfigError, get_providers
from backend.tests.provider_fakes import FakeHTTP, FakeResponse
from backend.tests.test_providers_registry import make_preset


def preset_with(provider_id, side="llm", scope=None, kwargs=None):
    preset = make_preset()
    cfg = preset.llm if side == "llm" else preset.embedding
    cfg.provider.id = provider_id
    cfg.provider.scope = scope
    cfg.model_kwargs = kwargs if kwargs is not None else {"api_key_env": "SIDE_KEY"}
    other = preset.embedding if side == "llm" else preset.llm
    other.model_kwargs = {"api_key_env": "OTHER_KEY"}
    return preset


class TestOpenAI(unittest.TestCase):
    def test_key_metadata(self):
        provider = get_providers(preset_with("openai"))["llm"]
        self.assertEqual(provider.default_key_env(), "OPENAI_API_KEY")
        self.assertEqual(provider.key_docs_url("OPENAI_API_KEY"), "https://platform.openai.com/api-keys")
        self.assertIsNone(provider.key_docs_url("OTHER"))
        self.assertEqual(provider.llm_api, "openai")

    def test_serves_both_sides_and_allows_user_and_managed(self):
        for side in ("embedding", "llm"):
            get_providers(preset_with("openai", side))
        get_providers(preset_with("openai", scope="managed", kwargs={"shared_api_key_env": "OPENAI_API_KEY"}))
        with self.assertRaises(ProviderConfigError):
            get_providers(preset_with("openai", scope="shared", kwargs={"shared_api_key_env": "OPENAI_API_KEY"}))


class TestAnthropic(unittest.TestCase):
    def test_protocol_and_key_metadata(self):
        provider = get_providers(preset_with("anthropic"))["llm"]
        self.assertEqual(provider.llm_api, "anthropic")
        self.assertEqual(provider.default_key_env(), "ANTHROPIC_API_KEY")
        self.assertEqual(provider.key_docs_url("ANTHROPIC_API_KEY"), "https://console.anthropic.com/settings/keys")

    def test_embedding_side_is_rejected(self):
        with self.assertRaisesRegex(ProviderConfigError, "does not support the embedding side"):
            get_providers(preset_with("anthropic", side="embedding"))

    def test_mixed_with_another_providers_embedding_side(self):
        preset = preset_with("anthropic")
        preset.embedding.provider.id = "openai"
        providers = get_providers(preset)
        self.assertEqual((providers["embedding"].id, providers["llm"].id), ("openai", "anthropic"))


class TestMpcdf(unittest.TestCase):
    URL = "https://llm.mpcdf.mpg.de/job-123"

    def provider(self, http):
        kwargs = {"shared_api_key_env": "MPCDF_LLM_API_KEY", "shared_base_url_env": "MPCDF_LLM_BASE_URL"}
        provider = get_providers(preset_with("mpcdf", kwargs=kwargs))["llm"]
        provider._http_client = http
        return provider

    def creds(self):
        return Credentials(api_key="secret", base_url=self.URL)

    def test_scope_is_shared_only_and_default(self):
        provider = self.provider(FakeHTTP())
        self.assertEqual(provider.scope, "shared")
        with self.assertRaises(ProviderConfigError):
            get_providers(preset_with("mpcdf", scope="user"))

    def test_does_not_provision(self):
        self.assertFalse(self.provider(FakeHTTP()).supports_provisioning)

    def test_ready_on_200_and_appends_v1_models(self):
        http = FakeHTTP({("GET", r"/models$"): FakeResponse(200, {"data": []})})
        health = self.provider(http).health(self.creds())
        self.assertEqual(health.status, "ready")
        self.assertEqual(http.calls[0][1], self.URL + "/v1/models")

    def test_url_that_already_ends_in_v1_is_not_doubled(self):
        http = FakeHTTP({("GET", r"/models$"): FakeResponse(200, {})})
        self.provider(http).health(Credentials(api_key="k", base_url=self.URL + "/v1"))
        self.assertEqual(http.calls[0][1], self.URL + "/v1/models")

    def test_rejected_key(self):
        for code in (401, 403):
            http = FakeHTTP({("GET", r"/models$"): FakeResponse(code, {})})
            health = self.provider(http).health(self.creds())
            self.assertEqual(health.status, "unreachable")
            self.assertIn("key rejected", health.detail)

    def test_expired_job(self):
        for response in (FakeResponse(404, {}), FakeResponse(405, {}), TimeoutError("t"), ConnectionError("c")):
            http = FakeHTTP({("GET", r"/models$"): response})
            health = self.provider(http).health(self.creds())
            self.assertEqual(health.status, "unreachable")
            self.assertIn("job expired or not started", health.detail)

    def test_other_server_error(self):
        http = FakeHTTP({("GET", r"/models$"): FakeResponse(500, {})})
        health = self.provider(http).health(self.creds())
        self.assertEqual((health.status, health.detail), ("unreachable", "HTTP 500"))

    def test_never_reports_cold(self):
        for response in (FakeResponse(200, {}), FakeResponse(503, {}), ConnectionError("x")):
            http = FakeHTTP({("GET", r"/models$"): response})
            self.assertNotEqual(self.provider(http).health(self.creds()).status, "cold")

    def test_not_configured_without_url_or_key(self):
        provider = self.provider(FakeHTTP())
        for creds in (Credentials(), Credentials(api_key="k"), Credentials(base_url=self.URL)):
            self.assertEqual(provider.health(creds).detail, "not configured")

    def test_hint_and_portal(self):
        provider = self.provider(FakeHTTP())
        self.assertIn("Start a new job", provider.unavailable_hint())
        self.assertIn("llm.mpcdf.mpg.de", provider.unavailable_hint())
        self.assertEqual(provider.key_docs_url("MPCDF_LLM_API_KEY"), "https://llm.mpcdf.mpg.de")


if __name__ == "__main__":
    unittest.main()
