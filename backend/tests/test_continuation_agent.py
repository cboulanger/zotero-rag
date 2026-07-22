"""Unit tests for ContinuationAgent."""

import unittest
from unittest.mock import MagicMock

from backend.models.conversation import ChatTurn
from backend.models.filters import MetadataFilters
from backend.services.continuation_agent import ContinuationAgent


def _chunk(chunk_id, title="Doc", text="Some text", page=1):
    return {
        "id": f"point-{chunk_id}",
        "payload": {
            "chunk_id": chunk_id, "text": text, "title": title,
            "item_key": f"ITEM-{chunk_id}", "library_id": "1",
            "authors": [], "year": None, "page_number": page,
            "text_preview": text[:20],
        },
    }


class TestContinuationAgent(unittest.IsolatedAsyncioTestCase):
    def _make_agent(self, chunks_by_ids_return):
        store = MagicMock()
        store.get_chunks_by_ids.return_value = chunks_by_ids_return
        return ContinuationAgent(store), store

    async def test_re_fetches_prior_source_refs(self):
        agent, store = self._make_agent([_chunk("c1"), _chunk("c2")])
        history = [ChatTurn(question="Q0", answer="A0", source_refs=["c1", "c2"])]

        result = await agent.execute(
            question="Tell me more", library_ids=["1"], filters=MetadataFilters(),
            conversation_history=history,
        )

        store.get_chunks_by_ids.assert_called_once_with(["c1", "c2"])
        self.assertEqual(result.agent_name, "continuation")
        self.assertEqual(result.source_refs, ["c1", "c2"])
        self.assertEqual(len(result.sources), 2)
        self.assertIn("Q0", result.context_text)
        self.assertIn("Some text", result.context_text)

    async def test_missing_chunks_tolerated(self):
        agent, store = self._make_agent([_chunk("c1")])  # c2 no longer exists
        history = [ChatTurn(question="Q0", answer="A0", source_refs=["c1", "c2"])]

        result = await agent.execute(
            question="Tell me more", library_ids=["1"], filters=MetadataFilters(),
            conversation_history=history,
        )
        self.assertEqual(result.source_refs, ["c1"])
        self.assertEqual(len(result.sources), 1)

    async def test_no_source_refs_falls_back_to_history_only(self):
        agent, store = self._make_agent([])
        history = [ChatTurn(question="Q0", answer="A0", source_refs=[])]  # e.g. mentions-derived turn

        result = await agent.execute(
            question="Tell me more", library_ids=["1"], filters=MetadataFilters(),
            conversation_history=history,
        )
        store.get_chunks_by_ids.assert_not_called()
        self.assertEqual(result.sources, [])
        self.assertEqual(result.source_refs, [])
        self.assertIn("Q0", result.context_text)

    async def test_no_conversation_history_produces_empty_result(self):
        agent, store = self._make_agent([])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        store.get_chunks_by_ids.assert_not_called()
        self.assertEqual(result.sources, [])

    def test_capability_prompt_mentions_conversation_only(self):
        agent, _ = self._make_agent([])
        self.assertIn("conversation", agent.capability_prompt.lower())

    def test_name_is_continuation(self):
        agent, _ = self._make_agent([])
        self.assertEqual(agent.name, "continuation")


if __name__ == "__main__":
    unittest.main()
