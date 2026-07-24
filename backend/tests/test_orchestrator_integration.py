"""
Integration tests for QueryOrchestrator using REAL agent instances.

test_orchestrator.py's `_make_orchestrator()` bypasses `QueryOrchestrator.__init__`
entirely (`QueryOrchestrator.__new__(QueryOrchestrator)` + manually setting
`orch._agents = {}`) and substitutes stub/mock agents everywhere. That's fine for
unit-testing orchestration logic in isolation, but it means nothing in the suite
actually exercises the real `_register_defaults()` wiring — real `ContinuationAgent`/
`MetadataAgent`/`RAGAgent` instances constructed the way production constructs them,
called through the orchestrator's real `agent.execute(**kwargs)` call. A future
agent-name typo or a kwargs mismatch between the orchestrator and an agent's
`execute()` signature would slip past every existing test.

These tests construct a REAL `QueryOrchestrator` via its normal constructor (so
`_register_defaults()` actually runs) against a REAL embedded-Qdrant `VectorStore`.
Only `llm_service`/`embedding_service` are mocked, since those need model weights /
API keys unavailable in the test environment.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from qdrant_client.models import Distance

from backend.config.settings import Settings
from backend.db.vector_store import VectorStore
from backend.models.conversation import ChatTurn
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata
from backend.services.base_agent import NeedsClarificationError, QueryPlan
from backend.services.query_orchestrator import QueryOrchestrator


def _make_chunk(chunk_id: str, item_key: str, text: str = "Some content.") -> DocumentChunk:
    return DocumentChunk(
        text=text,
        metadata=ChunkMetadata(
            chunk_id=chunk_id,
            document_metadata=DocumentMetadata(
                library_id="1", item_key=item_key, title=f"Doc {item_key}",
                authors=["Author"], year=2024,
            ),
            page_number=1, text_preview=text[:20], chunk_index=0, content_hash=f"hash-{chunk_id}",
        ),
        embedding=[0.1] * 384,
    )


class RealOrchestratorTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared setUp/tearDown: a real embedded-Qdrant VectorStore in a temp dir
    (same pattern as test_vector_store.py's setUp)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_path = Path(self.temp_dir) / "qdrant"
        self.vector_store = VectorStore(
            storage_path=self.storage_path,
            embedding_dim=384,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_orchestrator(self, **settings_overrides) -> QueryOrchestrator:
        """Construct a REAL QueryOrchestrator via its normal __init__, so
        _register_defaults() actually runs and registers real agent instances
        (RAGAgent, MetadataAgent, MentionsAgent, ContinuationAgent) exactly as
        production wires them up — not stubs."""
        settings = Settings(**settings_overrides)
        llm_service = MagicMock()
        llm_service.model_name = "mock-model"
        llm_service.generate = AsyncMock(return_value="Synthesized answer.")
        embedding_service = MagicMock()
        return QueryOrchestrator(
            embedding_service=embedding_service,
            llm_service=llm_service,
            vector_store=self.vector_store,
            settings=settings,
        )


class TestRealContinuationPath(RealOrchestratorTestCase):
    """Proves the full real chain works end-to-end: orchestrator dispatch ->
    real ContinuationAgent -> real VectorStore.get_chunks_by_ids -> back
    through orchestrator._synthesize() -> QueryResult.source_refs."""

    async def test_continuation_returns_real_chunk_source_ref(self):
        chunk = _make_chunk("real-chunk-1", "ITEM1", text="Luhmann on autopoiesis.")
        self.vector_store.add_chunk(chunk)

        orch = self._make_orchestrator()
        plan = QueryPlan(agents_to_use=["continuation"])
        history = [ChatTurn(
            question="What did Luhmann write?",
            answer="He wrote about autopoiesis.",
            source_refs=["real-chunk-1"],
        )]

        result = await orch.query(
            "Tell me more about that",
            library_ids=["1"],
            preset_plan=plan,
            conversation_history=history,
        )

        self.assertIn("real-chunk-1", result.source_refs)
        self.assertEqual(result.answer, "Synthesized answer.")


class TestRealClarificationPath(RealOrchestratorTestCase):
    """Proves the real MetadataAgent -> real orchestrator clarification
    short-circuit actually fires end-to-end when too many distinct items
    match, not just in the mocked-agent orchestrator tests."""

    async def test_metadata_agent_raises_needs_clarification_over_threshold(self):
        for i in range(3):
            self.vector_store.add_chunk(_make_chunk(f"chunk-{i}", f"ITEM{i}"))

        orch = self._make_orchestrator(metadata_narrowing_threshold=2)
        plan = QueryPlan(agents_to_use=["metadata"])

        with self.assertRaises(NeedsClarificationError):
            await orch.query(
                "List everything",
                library_ids=["1"],
                preset_plan=plan,
            )


if __name__ == "__main__":
    unittest.main()
