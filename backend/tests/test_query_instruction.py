"""Tests for the query-side task instruction of instruction-tuned embedding models."""

import unittest
from unittest.mock import AsyncMock

from backend.config.presets import EmbeddingConfig
from backend.services.embeddings import (
    DEFAULT_QUERY_INSTRUCTIONS,
    LocalEmbeddingService,
    MockEmbeddingService,
    RemoteEmbeddingService,
    format_query_for_embedding,
    resolve_query_instruction,
)

E5 = "multilingual-e5-large-instruct"
E5_INSTRUCTION = DEFAULT_QUERY_INSTRUCTIONS[E5]


class TestResolveQueryInstruction(unittest.TestCase):
    def test_known_model_gets_default(self):
        self.assertEqual(resolve_query_instruction(EmbeddingConfig(model_name=E5), E5), E5_INSTRUCTION)

    def test_organisation_prefix_and_case_are_ignored(self):
        name = "intfloat/Multilingual-E5-Large-Instruct"
        self.assertEqual(resolve_query_instruction(EmbeddingConfig(model_name=name), name), E5_INSTRUCTION)

    def test_unknown_model_gets_none(self):
        name = "sentence-transformers/all-MiniLM-L6-v2"
        self.assertIsNone(resolve_query_instruction(EmbeddingConfig(model_name=name), name))

    def test_explicit_instruction_wins(self):
        config = EmbeddingConfig(model_name=E5, query_instruction="Find it")
        self.assertEqual(resolve_query_instruction(config, E5), "Find it")

    def test_empty_instruction_disables_default(self):
        config = EmbeddingConfig(model_name=E5, query_instruction="")
        self.assertIsNone(resolve_query_instruction(config, E5))

    def test_no_config_uses_model_default(self):
        self.assertEqual(resolve_query_instruction(None, E5), E5_INSTRUCTION)


class TestFormatQuery(unittest.TestCase):
    def test_wraps_query(self):
        self.assertEqual(format_query_for_embedding("What is Zotero?", "Task"), "Instruct: Task\nQuery: What is Zotero?")

    def test_no_instruction_leaves_query_unchanged(self):
        self.assertEqual(format_query_for_embedding("What is Zotero?", None), "What is Zotero?")


class TestEmbedQuery(unittest.IsolatedAsyncioTestCase):
    async def test_remote_service_prefixes_queries_only(self):
        service = RemoteEmbeddingService(EmbeddingConfig(model_type="remote", model_name=E5), api_key="k")
        service.embed_text = AsyncMock(return_value=[0.5])
        self.assertEqual(await service.embed_query("Who uses Zotero?"), [0.5])
        service.embed_text.assert_awaited_once_with(f"Instruct: {E5_INSTRUCTION}\nQuery: Who uses Zotero?")

    async def test_local_service_without_instruction_embeds_bare(self):
        name = "sentence-transformers/all-MiniLM-L6-v2"
        service = LocalEmbeddingService(EmbeddingConfig(model_name=name))
        service.embed_text = AsyncMock(return_value=[0.5])
        await service.embed_query("Who uses Zotero?")
        service.embed_text.assert_awaited_once_with("Who uses Zotero?")

    async def test_preset_can_turn_it_off(self):
        config = EmbeddingConfig(model_type="remote", model_name=E5, query_instruction="")
        service = RemoteEmbeddingService(config, api_key="k")
        service.embed_text = AsyncMock(return_value=[0.5])
        await service.embed_query("Who uses Zotero?")
        service.embed_text.assert_awaited_once_with("Who uses Zotero?")

    async def test_mock_service_has_no_instruction(self):
        service = MockEmbeddingService(embedding_dim=3)
        self.assertIsNone(service.get_query_instruction())
        self.assertEqual(await service.embed_query("q"), [0.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
