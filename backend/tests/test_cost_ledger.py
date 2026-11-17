"""Cost estimates of paid providers: the provider hooks and the per-request ledger (issue #119)."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.config.presets import EmbeddingConfig, HardwarePreset, LLMConfig, ProviderConfig, RAGConfig
from backend.config.settings import Settings
from backend.providers import Cost, Pricing, Usage, get_providers
from backend.providers.base import match_pricing
from backend.providers.testing import preset_with
from backend.services.cost_ledger import CostLedger, collect, current_ledger, priced_sides, record_usage, token_counts

PRICES = {"input_per_million_tokens": 1.0, "output_per_million_tokens": 4.0}


def llm_provider(kwargs=None, provider_id="generic"):
    return get_providers(preset_with(provider_id, kwargs={"api_key_env": "SIDE_KEY", **(kwargs or {})}))["llm"]


class TestPricing(unittest.TestCase):
    def test_price_adds_every_rate(self):
        prices = Pricing(input_per_million_tokens=2, output_per_million_tokens=8, per_request=0.01, per_second=0.5)
        usage = Usage(side="llm", model="m", input_tokens=500_000, output_tokens=250_000, requests=2, seconds=3)
        self.assertAlmostEqual(prices.price(usage), 1.0 + 2.0 + 0.02 + 1.5)

    def test_unknown_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            Pricing.model_validate({"per_token": 1})

    def test_match_pricing_first_matching_glob_case_insensitive(self):
        small, big = Pricing(per_request=1), Pricing(per_request=2)
        table = [("gpt-4o-mini*", small), ("gpt-4o*", big)]
        self.assertIs(match_pricing(table, "GPT-4o-mini-2024-07-18"), small)
        self.assertIs(match_pricing(table, "gpt-4o"), big)
        self.assertIsNone(match_pricing(table, "other"))


class TestProviderPricing(unittest.TestCase):
    def test_generic_provider_has_no_default_prices(self):
        provider = llm_provider()
        self.assertIsNone(provider.pricing("m"))
        self.assertIsNone(provider.estimate_cost(Usage(side="llm", model="m", input_tokens=10)))

    def test_preset_pricing_object_applies_to_every_model(self):
        provider = llm_provider({"pricing": PRICES})
        cost = provider.estimate_cost(Usage(side="llm", model="any", input_tokens=1_000_000, output_tokens=500_000))
        self.assertIsInstance(cost, Cost)
        self.assertAlmostEqual(cost.amount, 3.0)
        self.assertEqual((cost.currency, cost.side, cost.input_tokens), ("USD", "llm", 1_000_000))

    def test_preset_pricing_by_model_glob(self):
        provider = llm_provider({"pricing": {"big-*": {"per_request": 1.0}, "*": {"per_request": 0.1}}})
        self.assertEqual(provider.pricing("big-one").per_request, 1.0)
        self.assertEqual(provider.pricing("small").per_request, 0.1)

    def test_malformed_preset_pricing_is_ignored(self):
        provider = llm_provider({"pricing": {"per_token": 3}})
        self.assertIsNone(provider.pricing("m"))

    def test_free_provider_never_has_prices(self):
        provider = llm_provider({"pricing": PRICES})
        provider.cost = "free"  # instance attribute shadows the class's
        self.assertIsNone(provider.pricing("m"))


class TestLedger(unittest.TestCase):
    def test_nothing_is_recorded_outside_collect(self):
        self.assertIsNone(current_ledger())
        self.assertIsNone(record_usage(llm_provider({"pricing": PRICES}), Usage(side="llm", model="m", input_tokens=1)))

    def test_collect_sums_per_side_and_currency(self):
        provider = llm_provider({"pricing": PRICES})
        with collect() as ledger:
            for _ in range(3):
                record_usage(provider, Usage(side="llm", model="m", input_tokens=1_000_000))
            record_usage(provider, Usage(side="llm", model="m", input_tokens=0))
        (total,) = ledger.costs()
        self.assertAlmostEqual(total.amount, 3.0)
        self.assertEqual(total.requests, 4)
        self.assertEqual(total.input_tokens, 3_000_000)
        self.assertIsNone(current_ledger())

    def test_ledger_keeps_currencies_apart(self):
        ledger = CostLedger()
        ledger.add(Cost(side="llm", amount=1.0, currency="USD"))
        ledger.add(Cost(side="llm", amount=2.0, currency="EUR"))
        self.assertEqual([(c.currency, c.amount) for c in ledger.costs()], [("USD", 1.0), ("EUR", 2.0)])

    def test_nested_collect_also_adds_to_the_outer_ledger(self):
        provider = llm_provider({"pricing": PRICES})
        with collect() as outer:
            with collect() as inner:
                record_usage(provider, Usage(side="llm", model="m", input_tokens=1_000_000))
            self.assertAlmostEqual(inner.costs()[0].amount, 1.0)
        self.assertAlmostEqual(outer.costs()[0].amount, 1.0)

    def test_tasks_and_threads_share_the_requests_ledger(self):
        provider = llm_provider({"pricing": PRICES})
        usage = Usage(side="llm", model="m", input_tokens=1_000_000)

        async def in_task():
            await asyncio.sleep(0)
            record_usage(provider, usage)

        async def run():
            with collect() as ledger:
                await asyncio.gather(asyncio.create_task(in_task()), asyncio.to_thread(record_usage, provider, usage))
            return ledger

        self.assertAlmostEqual(asyncio.run(run()).costs()[0].amount, 2.0)

    def test_a_failing_provider_never_raises(self):
        broken = Mock()
        broken.estimate_cost.side_effect = RuntimeError("boom")
        with collect() as ledger:
            self.assertIsNone(record_usage(broken, Usage(side="llm", model="m")))
        self.assertEqual(ledger.costs(), [])

    def test_unpriced_calls_leave_the_ledger_empty(self):
        with collect() as ledger:
            record_usage(llm_provider(), Usage(side="llm", model="m", input_tokens=5))
        self.assertEqual(ledger.as_dicts(), [])


class TestTokenCounts(unittest.TestCase):
    def test_openai_and_anthropic_shapes(self):
        self.assertEqual(token_counts(SimpleNamespace(prompt_tokens=10, completion_tokens=3)), (10, 3))
        self.assertEqual(token_counts(SimpleNamespace(input_tokens=7, output_tokens=2)), (7, 2))
        self.assertEqual(token_counts({"prompt_tokens": 4, "total_tokens": 4}), (4, 0))
        self.assertEqual(token_counts(None), (0, 0))
        self.assertEqual(token_counts(Mock()), (0, 0))


class TestPricedSides(unittest.TestCase):
    def test_only_sides_with_a_known_price(self):
        preset = preset_with("generic", kwargs={"api_key_env": "SIDE_KEY", "pricing": PRICES})
        self.assertEqual(priced_sides(preset), ["llm"])
        self.assertEqual(priced_sides(preset_with("generic")), [])


class TestServicesRecordCosts(unittest.IsolatedAsyncioTestCase):
    def _settings(self):
        preset = HardwarePreset(
            name="priced", description="d",
            embedding=EmbeddingConfig(model_type="remote", model_name="emb",
                                      model_kwargs={"api_key_env": "EMB_KEY", "base_url": "http://x/v1",
                                                    "pricing": {"input_per_million_tokens": 2.0}}),
            llm=LLMConfig(model_type="remote", model_names="m", provider=ProviderConfig(id="generic"),
                          model_kwargs={"api_key_env": "LLM_KEY", "base_url": "http://x/v1", "pricing": PRICES}),
            rag=RAGConfig(), memory_budget_gb=1.0,
        )
        settings = Mock(spec=Settings)
        settings.get_hardware_preset.return_value = preset
        settings.log_file = None
        return settings, preset

    async def test_llm_call_is_priced_from_the_response_usage(self):
        from backend.services.llm import RemoteLLMService

        settings, _ = self._settings()
        service = RemoteLLMService(settings, api_key="k")
        response = Mock()
        response.choices = [Mock()]
        response.choices[0].message.content = "answer"
        response.usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=250_000)
        raw = Mock()
        raw.parse.return_value = response
        raw.headers = {}
        client = AsyncMock()
        client.chat.completions.with_raw_response.create.return_value = raw
        with patch("openai.AsyncOpenAI", return_value=client), collect() as ledger:
            await service.generate("q")
        (cost,) = ledger.costs()
        self.assertEqual((cost.side, cost.input_tokens, cost.output_tokens), ("llm", 1_000_000, 250_000))
        self.assertAlmostEqual(cost.amount, 2.0)

    async def test_embedding_call_is_priced_from_the_response_usage(self):
        from backend.services.embeddings import RemoteEmbeddingService

        _, preset = self._settings()
        provider = get_providers(preset)["embedding"]
        service = RemoteEmbeddingService(preset.embedding, api_key="k", provider=provider)
        parsed = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])],
                                 usage=SimpleNamespace(prompt_tokens=500_000, total_tokens=500_000))
        raw = Mock()
        raw.parse.return_value = parsed
        raw.headers = {}
        client = AsyncMock()
        client.embeddings.with_raw_response.create.return_value = raw
        service._client = client
        with collect() as ledger:
            await service._create_embeddings_with_backoff(["text"])
        (cost,) = ledger.costs()
        self.assertEqual((cost.side, cost.input_tokens), ("embedding", 500_000))
        self.assertAlmostEqual(cost.amount, 1.0)


if __name__ == "__main__":
    unittest.main()
