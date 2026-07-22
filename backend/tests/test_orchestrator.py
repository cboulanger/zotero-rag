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

from backend.models.conversation import ChatTurn
from backend.models.filters import CitationTarget, MetadataFilters
from backend.services.base_agent import (
    AgentResult, BaseAgent, NeedsClarificationError, NeedsClientEvidenceError, QueryPlan,
)
from backend.services.mentions_agent import ClientEvidence
from backend.services.query_orchestrator import (
    QueryOrchestrator, _all_empty, _merge_sources, _rag_passthrough,
)
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


# ---------------------------------------------------------------------------
# _all_empty
# ---------------------------------------------------------------------------

class TestAllEmpty(unittest.TestCase):

    def test_empty_results_list(self):
        self.assertTrue(_all_empty([]))

    def test_no_sources_and_empty_text(self):
        ar = AgentResult(agent_name="metadata", context_text="", sources=[])
        self.assertTrue(_all_empty([ar]))

    def test_no_sources_and_no_items_found_text(self):
        ar = AgentResult(agent_name="metadata", context_text="No items found matching the metadata criteria.", sources=[])
        self.assertTrue(_all_empty([ar]))

    def test_no_sources_but_meaningful_text(self):
        ar = AgentResult(agent_name="rag", context_text="The association was founded in 1976.", sources=[])
        self.assertFalse(_all_empty([ar]))

    def test_has_sources(self):
        src = _make_source(item_id="A")
        ar = AgentResult(agent_name="metadata", context_text="No items found.", sources=[src])
        self.assertFalse(_all_empty([ar]))

    def test_mixed_results_one_has_sources(self):
        empty = AgentResult(agent_name="metadata", context_text="No items found.", sources=[])
        filled = AgentResult(agent_name="rag", context_text="answer", sources=[_make_source()])
        self.assertFalse(_all_empty([empty, filled]))


# ---------------------------------------------------------------------------
# RAG fallback when metadata agent returns empty
# ---------------------------------------------------------------------------

class TestRagFallback(unittest.IsolatedAsyncioTestCase):

    async def test_rag_fallback_runs_when_metadata_returns_empty(self):
        orch = _make_orchestrator()

        # Metadata agent returns empty
        meta_result = AgentResult(
            agent_name="metadata",
            context_text="No items found matching the metadata criteria.",
            sources=[],
        )
        meta_agent = _stub_agent("metadata", meta_result)

        # RAG agent returns content
        rag_result = AgentResult(
            agent_name="rag",
            context_text="The Vereinigung für Rechtssoziologie was founded in 1976.",
            sources=[_make_source(item_id="A")],
        )
        rag_agent = _stub_agent("rag", rag_result)

        orch.register(rag_agent)
        orch.register(meta_agent)
        orch._llm_service.model_name = "test-model"

        # Router selects metadata only (the mis-routing scenario)
        mock_plan = QueryPlan(agents_to_use=["metadata"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(
                question="Which German associations exist?",
                library_ids=["lib1"],
                enable_routing=True,
            )

        # RAG fallback should have been invoked
        rag_agent.execute.assert_called_once()
        self.assertIn("Vereinigung", result.answer)
        self.assertIn("rag", result.agents_used)


class TestMentionsShortCircuit(unittest.IsolatedAsyncioTestCase):

    async def test_raises_needs_client_evidence_when_targets_present_and_no_evidence(self):
        orch = _make_orchestrator()
        mentions_agent = _stub_agent(
            "mentions", AgentResult(agent_name="mentions", context_text="", sources=[])
        )
        # A second agent must be registered so the orchestrator's routing gate
        # (`len(self._agents) > 1`) actually invokes the mocked QueryRouter below,
        # rather than short-circuiting to the default ["rag"] plan.
        rag_agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="", sources=[]))
        orch.register(mentions_agent)
        orch.register(rag_agent)

        mock_plan = QueryPlan(
            agents_to_use=["mentions"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
        )
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            with self.assertRaises(NeedsClientEvidenceError) as cm:
                await orch.query(question="Who cites Teubner?", library_ids=["1"], enable_routing=True)

        self.assertEqual(len(cm.exception.citation_targets), 1)
        self.assertEqual(cm.exception.citation_targets[0].author, "teubner")
        mentions_agent.execute.assert_not_called()

    async def test_drops_mentions_when_no_citation_targets_extracted(self):
        orch = _make_orchestrator()
        rag_result = AgentResult(agent_name="rag", context_text="RAG answer", sources=[])
        rag_agent = _stub_agent("rag", rag_result)
        mentions_agent = _stub_agent(
            "mentions", AgentResult(agent_name="mentions", context_text="", sources=[])
        )
        orch.register(rag_agent)
        orch.register(mentions_agent)

        # Router selected "mentions" but extracted no citation_targets — must be dropped,
        # not passed through to raise NeedsClientEvidenceError for nothing.
        mock_plan = QueryPlan(agents_to_use=["mentions", "rag"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        mentions_agent.execute.assert_not_called()
        self.assertEqual(result.answer, "RAG answer")

    async def test_dropping_only_agent_falls_back_to_rag_without_double_execution(self):
        orch = _make_orchestrator()
        rag_agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="", sources=[]))
        mentions_agent = _stub_agent(
            "mentions", AgentResult(agent_name="mentions", context_text="", sources=[])
        )
        orch.register(rag_agent)
        orch.register(mentions_agent)

        # Router selected ONLY "mentions", with no citation_targets — and the RAG agent's
        # own result also happens to be empty, which is what triggers step 3b's fallback
        # guard. Before the fix, "mentions" being dropped left plan.agents_to_use == [],
        # fooling that guard into running RAG a second time.
        mock_plan = QueryPlan(agents_to_use=["mentions"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        rag_agent.execute.assert_called_once()
        mentions_agent.execute.assert_not_called()

    async def test_runs_mentions_when_evidence_supplied(self):
        orch = _make_orchestrator()
        mentions_result = AgentResult(agent_name="mentions", context_text="found it", sources=[])
        mentions_agent = _stub_agent("mentions", mentions_result)
        orch.register(mentions_agent)
        orch._llm_service.generate = AsyncMock(return_value="Synthesized")
        orch._settings.get_hardware_preset.return_value.llm.max_answer_tokens = 512

        evidence = ClientEvidence()
        mock_plan = QueryPlan(
            agents_to_use=["mentions"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
        )
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(
                question="Q?", library_ids=["1"], enable_routing=True, client_evidence=evidence,
            )

        mentions_agent.execute.assert_awaited_once()
        self.assertIs(mentions_agent.execute.call_args.kwargs["client_evidence"], evidence)
        self.assertEqual(result.answer, "Synthesized")

    async def test_preset_plan_skips_routing_llm_call(self):
        orch = _make_orchestrator()
        rag_agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="a", sources=[]))
        orch.register(rag_agent)

        preset_plan = QueryPlan(agents_to_use=["rag"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            await orch.query(question="Q?", library_ids=["1"], preset_plan=preset_plan)
            MockRouter.assert_not_called()


class TestConversationHistoryThreading(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_history_passed_to_agents(self):
        orch = _make_orchestrator()
        captured = {}

        async def fake_execute(**kwargs):
            captured.update(kwargs)
            return AgentResult(agent_name="rag", context_text="ans", sources=[])

        agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="x"))
        agent.execute = fake_execute
        orch._agents = {"rag": agent}
        history = [ChatTurn(question="Q0", answer="A0")]

        await orch.query("Follow-up", ["1"], enable_routing=False, conversation_history=history)

        self.assertEqual(captured["conversation_history"], history)

    async def test_force_fresh_retrieval_clears_history_for_agents(self):
        orch = _make_orchestrator()
        captured = {}

        async def fake_execute(**kwargs):
            captured.update(kwargs)
            return AgentResult(agent_name="rag", context_text="ans", sources=[])

        agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="x"))
        agent.execute = fake_execute
        orch._agents = {"rag": agent}
        history = [ChatTurn(question="Q0", answer="A0")]

        await orch.query("Follow-up", ["1"], enable_routing=False,
                          conversation_history=history, force_fresh_retrieval=True)

        self.assertEqual(captured["conversation_history"], [])


class TestClarificationShortCircuit(unittest.IsolatedAsyncioTestCase):
    async def test_router_clarification_needed_raises_before_agents_run(self):
        orch = _make_orchestrator()
        agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="should not run"))
        orch._agents = {"rag": agent}
        plan = QueryPlan(agents_to_use=["rag"], clarification_needed=True,
                          clarification_question="Which years?")

        with self.assertRaises(NeedsClarificationError) as ctx:
            await orch.query("Broad question", ["1"], preset_plan=plan)

        self.assertEqual(ctx.exception.message, "Which years?")
        agent.execute.assert_not_called()

    async def test_all_agents_flagging_clarification_raises(self):
        orch = _make_orchestrator()
        agent = _stub_agent("metadata", AgentResult(
            agent_name="metadata", context_text="too many",
            needs_clarification=True, clarification_message="Narrow it down",
        ))
        orch._agents = {"rag": agent, "metadata": agent}
        plan = QueryPlan(agents_to_use=["metadata"])

        with self.assertRaises(NeedsClarificationError) as ctx:
            await orch.query("Broad", ["1"], preset_plan=plan)
        self.assertEqual(ctx.exception.message, "Narrow it down")

    async def test_mixed_clarification_proceeds_with_usable_content(self):
        orch = _make_orchestrator()
        orch._llm_service.generate = AsyncMock(return_value="Synthesized answer.")
        orch._settings.get_hardware_preset.return_value.llm.max_answer_tokens = 512
        good = _stub_agent("rag", AgentResult(
            agent_name="rag", context_text="[S1] Good content",
            sources=[_make_source(item_id="A")],
        ))
        broad = _stub_agent("metadata", AgentResult(
            agent_name="metadata", context_text="too many",
            needs_clarification=True, clarification_message="Catalog too broad",
        ))
        orch._agents = {"rag": good, "metadata": broad}
        plan = QueryPlan(agents_to_use=["rag", "metadata"])

        result = await orch.query("Mixed", ["1"], preset_plan=plan)

        self.assertIn("Synthesized answer.", result.answer)
        sent_prompt = orch._llm_service.generate.call_args.kwargs["prompt"]
        self.assertIn("Catalog too broad", sent_prompt)


class TestContinuationAgentRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_agent_registered_by_default(self):
        with patch("backend.services.query_orchestrator.RAGAgent"), \
             patch("backend.services.query_orchestrator.MetadataAgent"), \
             patch("backend.services.query_orchestrator.ContinuationAgent") as MockContinuation:
            MockContinuation.return_value.name = "continuation"
            orch = QueryOrchestrator(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        self.assertIn("continuation", orch._agents)

    async def test_continuation_dropped_when_no_history(self):
        orch = _make_orchestrator()
        rag = _stub_agent("rag", AgentResult(agent_name="rag", context_text="ans"))
        continuation = _stub_agent("continuation", AgentResult(agent_name="continuation", context_text="x"))
        orch._agents = {"rag": rag, "continuation": continuation}
        plan = QueryPlan(agents_to_use=["continuation"])

        await orch.query("First question", ["1"], preset_plan=plan)

        continuation.execute.assert_not_called()
        rag.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
