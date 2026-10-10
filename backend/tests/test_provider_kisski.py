"""KisskiProvider: live model list, key metadata and the hour/day/month quota headers."""

import unittest

from backend.providers import ProviderConfigError, get_providers
from backend.providers.kisski import KisskiProvider
from backend.tests.provider_fakes import FakeHTTP, FakeResponse
from backend.tests.test_providers_registry import make_preset

BASE = "https://chat-ai.academiccloud.de/v1"

MODELS = {"data": [
    {"id": "big-model", "name": "Big", "input": ["text"], "output": ["text"], "demand": 7},
    {"id": "small-model", "input": ["text", "image"], "output": ["text"], "demand": 0},
    {"id": "mid-model", "input": ["text"], "output": ["text"], "demand": 2},
    {"id": "qwen-coder-32b", "input": ["text"], "output": ["text"], "demand": 0},
    {"id": "devstral-x", "input": ["text"], "output": ["text"], "demand": 0},
    {"id": "image-only", "input": ["image"], "output": ["text"], "demand": 0},
    {"id": "embed-only", "input": ["text"], "output": ["embedding"], "demand": 0},
    {"id": "", "input": ["text"], "output": ["text"]},
    "not-a-dict",
]}


def kisski_preset(llm_options=None, embedding_kisski=True):
    preset = make_preset()
    preset.llm.provider.id = "kisski"
    preset.llm.provider.options = llm_options or {}
    preset.llm.model_kwargs = {"api_key_env": "KISSKI_API_KEY", "base_url": BASE}
    if embedding_kisski:
        preset.embedding.provider.id = "kisski"
        preset.embedding.model_kwargs = {"api_key_env": "KISSKI_API_KEY", "base_url": BASE}
    return preset


def llm_provider(options=None, http=None):
    provider = get_providers(kisski_preset(options))["llm"]
    if http is not None:
        provider._http_client = http
    return provider


class TestLiveModels(unittest.TestCase):
    def test_filters_orders_by_demand_and_labels_availability(self):
        http = FakeHTTP({("POST", r"/v1/models$"): FakeResponse(200, MODELS)})
        models = llm_provider(http=http).live_models(BASE, "key")
        self.assertEqual([m.id for m in models], ["small-model", "mid-model", "big-model"])
        self.assertEqual([m.demand for m in models], [0, 2, 7])
        self.assertEqual([m.availability for m in models], ["available", "busy", "very busy"])

    def test_sends_bearer_key_and_posts_to_base_url_models(self):
        http = FakeHTTP({("POST", r"/models$"): FakeResponse(200, {"data": []})})
        llm_provider(http=http).live_models(BASE + "/", "secret-key")
        method, url, _ = http.calls[0]
        self.assertEqual((method, url), ("POST", BASE + "/models"))

    def test_models_url_option_overrides_the_default(self):
        http = FakeHTTP({("POST", r"^https://other\.example/list$"): FakeResponse(200, {"data": []})})
        provider = llm_provider({"models_url": "https://other.example/list"}, http)
        self.assertEqual(provider.live_models(BASE, "k"), [])
        self.assertEqual(http.calls[0][1], "https://other.example/list")

    def test_failures_return_none_instead_of_raising(self):
        for response in (FakeResponse(500, {"error": "x"}), FakeResponse(200, None), ConnectionError("down")):
            with self.subTest(response=repr(response)):
                http = FakeHTTP({("POST", r"/models$"): response})
                self.assertIsNone(llm_provider(http=http).live_models(BASE, "k"))

    def test_only_the_llm_side_has_a_model_list(self):
        provider = get_providers(kisski_preset())["embedding"]
        provider._http_client = FakeHTTP()
        self.assertIsNone(provider.live_models(BASE, "k"))
        self.assertEqual(provider._http_client.calls, [])


class TestKeyMetadataAndScope(unittest.TestCase):
    def test_key_env_and_portal(self):
        provider = llm_provider()
        self.assertEqual(provider.default_key_env(), "KISSKI_API_KEY")
        self.assertEqual(provider.key_docs_url("KISSKI_API_KEY"), "https://saia.gwdg.de/dashboard")
        self.assertIsNone(provider.key_docs_url("SOMETHING_ELSE"))

    def test_only_user_scope_is_allowed(self):
        self.assertEqual(KisskiProvider.key_scopes, frozenset({"user"}))
        preset = kisski_preset()
        preset.llm.provider.scope = "managed"
        with self.assertRaisesRegex(ProviderConfigError, "does not allow scope 'managed'"):
            get_providers(preset)

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(ProviderConfigError):
            get_providers(kisski_preset({"modelz_url": "x"}))


class TestUsage(unittest.TestCase):
    def test_hour_and_day_headers_become_two_meters(self):
        meters = llm_provider().parse_usage({
            "x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40",
            "x-ratelimit-limit-day": "1000", "x-ratelimit-remaining-day": "990",
        }, as_of="2026-10-10T00:00:00+00:00")
        by_period = {m.period: m for m in meters}
        self.assertEqual(set(by_period), {"hour", "day"})
        self.assertEqual((by_period["hour"].limit, by_period["hour"].remaining), (100, 40))
        self.assertEqual(by_period["day"].id, "requests/day")
        self.assertEqual(by_period["hour"].unit, "requests")
        self.assertEqual(by_period["hour"].side, "llm")

    def test_month_quota_becomes_a_meter_so_an_exhausted_month_is_visible(self):
        meters = llm_provider().parse_usage({
            "x-ratelimit-limit-month": "10000", "x-ratelimit-remaining-month": "0",
        }, as_of="2026-10-10T00:00:00+00:00")
        (month,) = meters
        self.assertEqual((month.id, month.limit, month.remaining), ("requests/month", 10000, 0))

    def test_partial_and_non_numeric_headers_are_skipped(self):
        provider = llm_provider()
        self.assertEqual(provider.parse_usage({"x-ratelimit-limit-hour": "100"}), [])
        self.assertEqual(provider.parse_usage({"x-ratelimit-limit-hour": "x", "x-ratelimit-remaining-hour": "y"}), [])

    def test_standard_dialects_still_work(self):
        meters = llm_provider().parse_usage({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "9"})
        self.assertEqual([(m.unit, m.limit) for m in meters], [("requests", 10)])

    def test_header_names_are_case_insensitive(self):
        meters = llm_provider().parse_usage({"X-RateLimit-Limit-Hour": "5", "X-RateLimit-Remaining-Hour": "5"})
        self.assertEqual([m.period for m in meters], ["hour"])


if __name__ == "__main__":
    unittest.main()
