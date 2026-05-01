"""
Unit tests for QueryOrchestrator:
- agent registration and dispatch
- routing integration
- parallel execution
- synthesis vs pass-through
- custom agent registration
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent, QueryPlan
from backend.services.query_orchestrator import QueryOrchestrator, _merge_sources, _rag_passthrough
from backend.services.rag_engine import QueryResult, SourceInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(**kwargs) -> SourceInfo:
    defaults = dict(item_id="X", library_id="1", title="T", score=0.9)
    defaults.update(kwargs)
    return SourceInfo(**defaults)


def _make_orchestrator() -> QueryOrchestrator:
    """Return an orchestrator with mocked services (no real models loaded)."""
    with patch("backend.services.query_orchestrator.RAGAgent"), \
         patch("backend.services.query_orchestrator.MetadataAgent"):
        orch = QueryOrchestrator.__new__(QueryOrchestrator)
        orch._agents = {}
        orch._llm_service = MagicMock()
        orch._settings = MagicMock()
        return orch


def _stub_agent(name: str, result: AgentResult) -> BaseAgent:
    agent = MagicMock(spec=BaseAgent)
    agent.name = name
    agent.capability_prompt = f"{name} capability"
    agent.execute = AsyncMock(return_value=result)
    return agent


# ---------------------------------------------------------------------------
# _rag_passthrough
# ---------------------------------------------------------------------------

class TestRagPassthrough(unittest.TestCase):

    def test_returns_query_result(self):
        source = _make_source(item_id="A")
        ar = AgentResult(
            agent_name="rag",
            context_text="The answer is 42.",
            sources=[source],
        )
        qr = _rag_passthrough(ar, "What is 42?")
        self.assertIsInstance(qr, QueryResult)
        self.assertEqual(qr.answer, "The answer is 42.")
        self.assertEqual(qr.question, "What is 42?")
        self.assertEqual(len(qr.sources), 1)

    def test_converts_dict_sources(self):
        ar = AgentResult(
            agent_name="rag",
            context_text="answer",
            sources=[{"item_id": "B", "library_id": "1", "title": "T", "score": 0.8}],
        )
        qr = _rag_passthrough(ar, "Q")
        self.assertIsInstance(qr.sources[0], SourceInfo)
        self.assertEqual(qr.sources[0].item_id, "B")


# ---------------------------------------------------------------------------
# _merge_sources
# ---------------------------------------------------------------------------

class TestMergeSources(unittest.TestCase):

    def test_deduplicates_by_item_id(self):
        s = _make_source(item_id="SAME")
        results = [
            AgentResult(agent_name="rag", context_text="", sources=[s]),
            AgentResult(agent_name="metadata", context_text="", sources=[{"item_id": "SAME", "library_id": "1", "title": "T", "score": 1.0}]),
        ]
        merged = _merge_sources(results)
        ids = [m.item_id for m in merged]
        self.assertEqual(ids.count("SAME"), 1)

    def test_keeps_sources_from_multiple_agents(self):
        results = [
            AgentResult(agent_name="rag", context_text="", sources=[_make_source(item_id="A")]),
            AgentResult(agent_name="metadata", context_text="", sources=[
                {"item_id": "B", "library_id": "1", "title": "T", "score": 1.0}
            ]),
        ]
        merged = _merge_sources(results)
        ids = {m.item_id for m in merged}
        self.assertIn("A", ids)
        self.assertIn("B", ids)


# ---------------------------------------------------------------------------
# QueryOrchestrator
# ---------------------------------------------------------------------------

class TestOrchestratorRegister(unittest.TestCase):

    def test_register_adds_agent(self):
        orch = _make_orchestrator()
        agent = _stub_agent("custom", AgentResult(agent_name="custom", context_text=""))
        orch.register(agent)
        self.assertIn("custom", orch._agents)

    def test_register_replaces_existing(self):
        orch = _make_orchestrator()
        a1 = _stub_agent("rag", AgentResult(agent_name="rag", context_text="v1"))
        a2 = _stub_agent("rag", AgentResult(agent_name="rag", context_text="v2"))
        orch.register(a1)
        orch.register(a2)
        self.assertIs(orch._agents["rag"], a2)


class TestOrchestratorQuery(unittest.IsolatedAsyncioTestCase):

    async def test_rag_only_result_uses_passthrough(self):
        orch = _make_orchestrator()
        source = _make_source(item_id="P")
        rag_result = AgentResult(agent_name="rag", context_text="Passthrough answer", sources=[source])
        rag_agent = _stub_agent("rag", rag_result)
        orch.register(rag_agent)

        result = await orch.query(
            question="What?", library_ids=["1"], enable_routing=False
        )
        self.assertIsInstance(result, QueryResult)
        self.assertEqual(result.answer, "Passthrough answer")
        self.assertEqual(result.sources[0].item_id, "P")

    async def test_routing_disabled_uses_rag_only(self):
        orch = _make_orchestrator()
        rag_result = AgentResult(agent_name="rag", context_text="RAG answer", sources=[])
        rag_agent = _stub_agent("rag", rag_result)
        meta_agent = _stub_agent("metadata", AgentResult(agent_name="metadata", context_text="", sources=[]))
        orch.register(rag_agent)
        orch.register(meta_agent)

        result = await orch.query(
            question="What?", library_ids=["1"], enable_routing=False
        )
        rag_agent.execute.assert_called_once()
        meta_agent.execute.assert_not_called()
        self.assertEqual(result.answer, "RAG answer")

    async def test_routing_enabled_calls_router(self):
        orch = _make_orchestrator()
        rag_result = AgentResult(agent_name="rag", context_text="answer", sources=[])
        rag_agent = _stub_agent("rag", rag_result)
        meta_agent = _stub_agent("metadata", AgentResult(agent_name="metadata", context_text="", sources=[]))
        orch.register(rag_agent)
        orch.register(meta_agent)

        mock_plan = QueryPlan(agents_to_use=["rag"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            mock_router_instance = MagicMock()
            mock_router_instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = mock_router_instance

            await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        mock_router_instance.route.assert_called_once()

    async def test_multiple_agents_triggers_synthesis(self):
        orch = _make_orchestrator()
        rag_agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="RAG context", sources=[]))
        meta_agent = _stub_agent("metadata", AgentResult(agent_name="metadata", context_text="META context", sources=[]))
        orch.register(rag_agent)
        orch.register(meta_agent)

        # Stub LLM synthesis call
        orch._llm_service.generate = AsyncMock(return_value="Synthesized answer")
        orch._settings.get_hardware_preset.return_value.llm.max_answer_tokens = 512

        mock_plan = QueryPlan(agents_to_use=["rag", "metadata"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            mock_router_instance = MagicMock()
            mock_router_instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = mock_router_instance

            result = await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        self.assertEqual(result.answer, "Synthesized answer")
        # Synthesis prompt must contain both agents' context
        synthesis_prompt = orch._llm_service.generate.call_args.kwargs["prompt"]
        self.assertIn("RAG context", synthesis_prompt)
        self.assertIn("META context", synthesis_prompt)

    async def test_custom_agent_can_be_registered_and_called(self):
        orch = _make_orchestrator()
        custom_result = AgentResult(agent_name="custom", context_text="Custom answer", sources=[])
        custom_agent = _stub_agent("custom", custom_result)
        orch.register(custom_agent)

        # Stub LLM for synthesis (custom agent alone triggers synthesis path)
        orch._llm_service.generate = AsyncMock(return_value="Synthesized")
        orch._settings.get_hardware_preset.return_value.llm.max_answer_tokens = 512

        mock_plan = QueryPlan(agents_to_use=["custom"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(question="Custom Q", library_ids=[], enable_routing=True)

        custom_agent.execute.assert_called_once()

    async def test_unknown_agent_in_plan_falls_back_to_rag(self):
        orch = _make_orchestrator()
        rag_result = AgentResult(agent_name="rag", context_text="answer", sources=[])
        rag_agent = _stub_agent("rag", rag_result)
        orch.register(rag_agent)

        mock_plan = QueryPlan(agents_to_use=["nonexistent"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        rag_agent.execute.assert_called_once()

    async def test_agents_receive_filters_from_plan(self):
        orch = _make_orchestrator()
        rag_result = AgentResult(agent_name="rag", context_text="answer", sources=[])
        rag_agent = _stub_agent("rag", rag_result)
        # Need 2+ agents for routing to be triggered
        meta_agent = _stub_agent("metadata", AgentResult(agent_name="metadata", context_text="", sources=[]))
        orch.register(rag_agent)
        orch.register(meta_agent)

        filters = MetadataFilters(year_min=1970, authors=["luhmann"])
        mock_plan = QueryPlan(agents_to_use=["rag"], filters=filters)
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        call_kwargs = rag_agent.execute.call_args.kwargs
        self.assertEqual(call_kwargs["filters"].year_min, 1970)
        self.assertIn("luhmann", call_kwargs["filters"].authors)


if __name__ == "__main__":
    unittest.main()
