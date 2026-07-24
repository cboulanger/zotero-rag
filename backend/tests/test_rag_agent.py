"""Unit tests for RAGAgent."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.rag_agent import RAGAgent
from backend.services.rag_engine import QueryResult, SourceInfo
from backend.models.filters import MetadataFilters


class TestRAGAgentSourceRefs(unittest.IsolatedAsyncioTestCase):
    async def test_source_refs_populated_from_chunk_ids(self):
        agent = RAGAgent(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        fake_result = QueryResult(
            question="Q", answer="A",
            sources=[
                SourceInfo(item_id="A", library_id="1", title="T1", score=0.9, chunk_id="c1"),
                SourceInfo(item_id="B", library_id="1", title="T2", score=0.8, chunk_id=None),
            ],
        )
        with patch("backend.services.rag_agent.RAGEngine") as MockEngine:
            MockEngine.return_value.query = AsyncMock(return_value=fake_result)
            result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertEqual(result.source_refs, ["c1"])


if __name__ == "__main__":
    unittest.main()
