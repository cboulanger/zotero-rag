"""Tests for cross-lingual retrieval: translating non-English questions into English
(backend/services/query_translation.py) and searching/re-ranking with both versions."""

import unittest
from unittest.mock import AsyncMock, Mock

from backend.services.query_translation import (
    build_translation_prompt,
    parse_translation,
    translate_question,
)

DE = "Welche Werke analysieren die Rechtsdogmatik?"
EN = "Which works analyse legal doctrine?"


class ParseTranslationTest(unittest.TestCase):
    def test_first_nonempty_line_stripped_of_quotes_and_label(self):
        self.assertEqual(parse_translation(f'\n"{EN}"\n', DE), EN)
        self.assertEqual(parse_translation(f"Translation: {EN}", DE), EN)
        self.assertEqual(parse_translation(f"- {EN}\nsome commentary", DE), EN)

    def test_identical_output_is_ignored(self):
        self.assertIsNone(parse_translation("what is zotero?", "What is Zotero?"))

    def test_degenerate_output_is_ignored(self):
        self.assertIsNone(parse_translation("", DE))
        self.assertIsNone(parse_translation("   \n  ", DE))
        self.assertIsNone(parse_translation("a" * 400, DE))  # over the 300-character limit

    def test_prompt_contains_the_question(self):
        self.assertIn(DE, build_translation_prompt(DE))


class TranslateQuestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_translation(self):
        llm = Mock()
        llm.generate = AsyncMock(return_value=EN)
        self.assertEqual(await translate_question(llm, DE), EN)
        self.assertEqual(llm.generate.await_args.kwargs["temperature"], 0.0)

    async def test_llm_error_returns_none(self):
        llm = Mock()
        llm.generate = AsyncMock(side_effect=RuntimeError("provider down"))
        self.assertIsNone(await translate_question(llm, DE))


class EngineTranslationTest(unittest.IsolatedAsyncioTestCase):
    """RAGEngine.query searches with the question and its English translation."""

    def _engine(self, translation_on=True, translation=EN, translation_error=None, preset=None):
        from backend.config.settings import Settings
        from backend.services.rag_engine import RAGEngine
        from backend.tests.test_rerank import _preset, _result

        settings = Mock(spec=Settings)
        settings.retrieval_lexical_weight = 0.0
        settings.retrieval_embedding_mmr_lambda = 0.0
        settings.retrieval_near_dup_threshold = 0.0
        settings.retrieval_query_decomposition = 0
        settings.retrieval_score_proximity_epsilon = 0.0
        settings.retrieval_query_translation = translation_on
        settings.get_hardware_preset.return_value = preset or _preset()

        self.translation_calls = 0

        async def generate(prompt, **kwargs):
            if prompt.startswith("Translate this search question"):
                self.translation_calls += 1
                if translation_error:
                    raise translation_error
                return translation
            return "Answer [S1]"

        llm = Mock()
        llm.generate = AsyncMock(side_effect=generate)
        llm.model_name = "test-model"
        embedding = Mock()
        embedding.embed_query = AsyncMock(side_effect=lambda q: [0.2] if q == EN else [0.1])
        embedding.model_name = "test-embedding"
        pool = [_result("GEN", "generic text", 0.95), _result("FACT", "the fact", 0.80)]
        store = Mock()
        store.search = Mock(side_effect=lambda **kw: pool[: kw["limit"]])
        return RAGEngine(embedding_service=embedding, llm_service=llm, vector_store=store, settings=settings)

    @staticmethod
    def _vectors(engine):
        return [c.kwargs["query_vector"] for c in engine.vector_store.search.call_args_list]

    async def test_non_english_question_searches_twice_and_fuses(self):
        engine = self._engine()
        await engine.query(DE, ["1"], top_k=1, search_candidate_pool_size=2, diversity_floor=1)
        self.assertEqual(self._vectors(engine), [[0.1], [0.2]])
        self.assertEqual(self.translation_calls, 1)

    async def test_english_question_is_not_translated(self):
        engine = self._engine()
        await engine.query(EN, ["1"], top_k=1, search_candidate_pool_size=2, diversity_floor=1)
        self.assertEqual(self._vectors(engine), [[0.2]])  # the mock embeds EN as [0.2]; searched once
        self.assertEqual(self.translation_calls, 0)

    async def test_setting_off_means_no_translation(self):
        engine = self._engine(translation_on=False)
        await engine.query(DE, ["1"], top_k=1, search_candidate_pool_size=2, diversity_floor=1)
        self.assertEqual(self._vectors(engine), [[0.1]])
        self.assertEqual(self.translation_calls, 0)

    async def test_translation_failure_falls_back(self):
        engine = self._engine(translation_error=RuntimeError("provider down"))
        result = await engine.query(DE, ["1"], top_k=1, search_candidate_pool_size=2, diversity_floor=1)
        self.assertEqual(self._vectors(engine), [[0.1]])
        self.assertTrue(result.answer)

    async def test_translated_query_in_trace(self):
        from backend.services.trace_collector import TraceCollector

        collector = TraceCollector(DE, ["1"], {})
        await self._engine().query(DE, ["1"], top_k=1, search_candidate_pool_size=2, trace=collector)
        self.assertEqual(collector.finalize().agent_executions[0].retrieval.translated_query, EN)

    async def test_escalation_reuses_the_translation(self):
        engine = self._engine()
        await engine.query(DE, ["1"], top_k=1, search_candidate_pool_size=2, diversity_floor=5)
        # first search + escalation search, each with both versions; one translation call
        self.assertEqual(self._vectors(engine), [[0.1], [0.2], [0.1], [0.2]])
        self.assertEqual(self.translation_calls, 1)

    async def test_reranker_sees_both_versions(self):
        from unittest.mock import patch

        from backend.tests.test_rerank import FakeScorer, _preset

        questions = []

        class Recording(FakeScorer):
            async def score(self, question, texts):
                questions.append(question)
                return await super().score(question, texts)

        engine = self._engine(preset=_preset({"model_name": "x"}))
        with patch("backend.services.rerank.build_reranker", return_value=Recording()):
            await engine.query(DE, ["1"], top_k=1, search_candidate_pool_size=2, diversity_floor=1)
        self.assertEqual(questions, [DE, EN])


if __name__ == "__main__":
    unittest.main()
