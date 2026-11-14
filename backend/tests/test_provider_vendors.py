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


class TestGoogle(unittest.TestCase):
    def test_key_metadata(self):
        provider = get_providers(preset_with("google"))["llm"]
        self.assertEqual(provider.default_key_env(), "GEMINI_API_KEY")
        self.assertEqual(provider.key_docs_url("GEMINI_API_KEY"), "https://aistudio.google.com/apikey")
        self.assertIsNone(provider.key_docs_url("OTHER"))
        self.assertEqual(provider.llm_api, "openai")

    def test_serves_both_sides_and_rejects_shared_scope(self):
        for side in ("embedding", "llm"):
            get_providers(preset_with("google", side))
        with self.assertRaises(ProviderConfigError):
            get_providers(preset_with("google", scope="shared", kwargs={"shared_api_key_env": "GEMINI_API_KEY"}))


class TestGoogleLiveModels(unittest.TestCase):
    BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

    def _provider(self, http, side="llm"):
        provider = get_providers(preset_with("google"))[side]
        provider._http_client = http
        return provider

    def test_lists_only_flash_text_models_newest_first_without_prefix(self):
        data = [{"id": f"models/{i}"} for i in (
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash",
            "gemini-2.5-flash-preview-tts", "gemini-2.5-flash-image", "gemini-3.1-flash-live-preview",
            "gemini-embedding-001", "gemini-3.1-flash-lite",
        )]
        http = FakeHTTP({("GET", r"/openai/models$"): FakeResponse(200, {"data": data})})
        models = self._provider(http).live_models(self.BASE + "/", "k")
        self.assertEqual([m.id for m in models],
                         ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"])
        self.assertEqual(http.calls[0][:2], ("GET", self.BASE + "/models"))

    def test_failures_return_none_and_embedding_side_has_no_list(self):
        for response in (FakeResponse(403, {}), FakeResponse(200, None), ConnectionError("down")):
            http = FakeHTTP({("GET", r"/models$"): response})
            self.assertIsNone(self._provider(http).live_models(self.BASE, "k"))
        http = FakeHTTP()
        self.assertIsNone(self._provider(http, "embedding").live_models(self.BASE, "k"))
        self.assertEqual(http.calls, [])


class TestBundledPresetsPickUpProviderDefaults(unittest.TestCase):
    """get_preset() runs each provider's apply_defaults(), so the services read model_kwargs only."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_remote_openai_gets_its_key_name_and_portal_link_from_the_provider(self):
        from backend.config.presets import get_preset
        from backend.services.embeddings import RemoteEmbeddingService
        from backend.services.llm import RemoteLLMService
        preset = get_preset("remote-openai", self.data_path)
        for side in (preset.embedding, preset.llm):
            self.assertEqual(side.model_kwargs["api_key_env"], "OPENAI_API_KEY")
        (emb_field,) = RemoteEmbeddingService.required_client_fields(preset.embedding)
        (llm_field,) = RemoteLLMService.required_client_fields_for_config(preset.llm)
        for field in (emb_field, llm_field):
            self.assertEqual(field["key_name"], "OPENAI_API_KEY")
            self.assertEqual(field["header_name"], "X-Openai-Api-Key")
            self.assertEqual(field["docs_url"], "https://platform.openai.com/api-keys")

    def test_remote_kisski_keeps_its_key_and_gets_the_portal_link(self):
        from backend.config.presets import get_preset
        from backend.services.llm import RemoteLLMService
        preset = get_preset("remote-kisski", self.data_path)
        (field,) = RemoteLLMService.required_client_fields_for_config(preset.llm)
        self.assertEqual((field["key_name"], field["kind"]), ("KISSKI_API_KEY", "api_key"))
        self.assertEqual(field["docs_url"], "https://saia.gwdg.de/dashboard")

    def test_every_bundled_preset_has_valid_providers(self):
        from backend.config.presets import DEFAULT_PRESETS_DIR, list_presets
        self.assertEqual(
            set(list_presets(self.data_path)), {p.stem for p in DEFAULT_PRESETS_DIR.glob("*.json")}
        )


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
    """Per-user only; session behaviour is tested in test_mpcdf_sessions."""

    def test_scope_is_user_only(self):
        provider = get_providers(preset_with("mpcdf", kwargs={}))["llm"]
        self.assertEqual(provider.scope, "user")
        self.assertTrue(provider.uses_session_endpoints)
        self.assertFalse(provider.provisioning_needs_key)
        for scope in ("shared", "managed"):
            with self.assertRaises(ProviderConfigError):
                get_providers(preset_with("mpcdf", scope=scope, kwargs={}))

    def test_provisions_and_suspends(self):
        provider = get_providers(preset_with("mpcdf", kwargs={}))["llm"]
        self.assertTrue(provider.supports_provisioning and provider.supports_suspend)
        self.assertIsNotNone(provider.describe().provisioning.login)


if __name__ == "__main__":
    unittest.main()
