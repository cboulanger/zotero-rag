"""
Unit tests for QueryRouter:
- prompt assembly from registered agents
- JSON parsing with various edge cases
- MetadataFilters extraction
- Fallback to RAG-only plan on parse failure or unknown agent names
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent, QueryPlan
from backend.services.query_router import QueryRouter, _parse_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(name: str, capability: str) -> BaseAgent:
    """Return a minimal concrete BaseAgent stub."""
    agent = MagicMock(spec=BaseAgent)
    agent.name = name
    agent.capability_prompt = capability
    return agent


def _make_router(json_response: str) -> QueryRouter:
    """Return a QueryRouter whose LLM always returns *json_response*."""
    llm = MagicMock()
    llm.generate = AsyncMock(return_value=json_response)
    return QueryRouter(llm)


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------

class TestParseJson(unittest.TestCase):

    def test_plain_json(self):
        data = _parse_json('{"agents": ["rag"], "year_min": null}')
        self.assertEqual(data["agents"], ["rag"])

    def test_strips_markdown_fence(self):
        raw = '```json\n{"agents": ["metadata"]}\n```'
        data = _parse_json(raw)
        self.assertEqual(data["agents"], ["metadata"])

    def test_strips_plain_code_fence(self):
        raw = '```\n{"agents": ["rag", "metadata"]}\n```'
        data = _parse_json(raw)
        self.assertEqual(data["agents"], ["rag", "metadata"])

    def test_json_with_leading_text(self):
        raw = 'Here is the answer: {"agents": ["rag"]}'
        data = _parse_json(raw)
        self.assertEqual(data["agents"], ["rag"])

    def test_raises_on_no_braces(self):
        with self.assertRaises(ValueError):
            _parse_json("no json here at all")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(Exception):
            _parse_json("{bad json}")


# ---------------------------------------------------------------------------
# QueryRouter.route
# ---------------------------------------------------------------------------

class TestQueryRouterRoute(unittest.IsolatedAsyncioTestCase):

    async def test_returns_query_plan(self):
        router = _make_router('{"agents": ["rag"], "year_min": null, "year_max": null, "authors": [], "item_types": [], "title_keywords": [], "routing_description": null}')
        agents = [_make_agent("rag", "semantic search")]
        plan = await router.route("What is ML?", agents)
        self.assertIsInstance(plan, QueryPlan)

    async def test_selects_rag_agent(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "semantic"), _make_agent("metadata", "catalog")]
        plan = await router.route("What does Luhmann say?", agents)
        self.assertEqual(plan.agents_to_use, ["rag"])

    async def test_selects_metadata_agent(self):
        router = _make_router('{"agents": ["metadata"]}')
        agents = [_make_agent("rag", "semantic"), _make_agent("metadata", "catalog")]
        plan = await router.route("List all books by Luhmann", agents)
        self.assertEqual(plan.agents_to_use, ["metadata"])

    async def test_selects_both_agents(self):
        router = _make_router('{"agents": ["rag", "metadata"]}')
        agents = [_make_agent("rag", "semantic"), _make_agent("metadata", "catalog")]
        plan = await router.route("Books by Luhmann about autopoiesis", agents)
        self.assertIn("rag", plan.agents_to_use)
        self.assertIn("metadata", plan.agents_to_use)

    async def test_extracts_year_range(self):
        router = _make_router('{"agents": ["metadata"], "year_min": 1970, "year_max": 1990}')
        agents = [_make_agent("metadata", "catalog")]
        plan = await router.route("Books from 1970 to 1990", agents)
        self.assertEqual(plan.filters.year_min, 1970)
        self.assertEqual(plan.filters.year_max, 1990)

    async def test_extracts_authors(self):
        router = _make_router('{"agents": ["rag"], "authors": ["luhmann", "habermas"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Luhmann vs Habermas", agents)
        self.assertIn("luhmann", plan.filters.authors)
        self.assertIn("habermas", plan.filters.authors)

    async def test_extracts_item_types(self):
        router = _make_router('{"agents": ["metadata"], "item_types": ["book"]}')
        agents = [_make_agent("metadata", "catalog")]
        plan = await router.route("List all books", agents)
        self.assertEqual(plan.filters.item_types, ["book"])

    async def test_extracts_title_keywords(self):
        router = _make_router('{"agents": ["metadata"], "title_keywords": ["autopoiesis"]}')
        agents = [_make_agent("metadata", "catalog")]
        plan = await router.route("Find autopoiesis papers", agents)
        self.assertEqual(plan.filters.title_keywords, ["autopoiesis"])

    async def test_extracts_routing_description(self):
        router = _make_router('{"agents": ["rag"], "routing_description": "semantic question"}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What is autopoiesis?", agents)
        self.assertEqual(plan.routing_description, "semantic question")

    async def test_fallback_on_invalid_json(self):
        router = _make_router("Sorry, I cannot answer that.")
        agents = [_make_agent("rag", "semantic"), _make_agent("metadata", "catalog")]
        plan = await router.route("Some question", agents)
        self.assertEqual(plan.agents_to_use, ["rag"])
        self.assertIsInstance(plan.filters, MetadataFilters)

    async def test_fallback_on_llm_exception(self):
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=RuntimeError("LLM error"))
        router = QueryRouter(llm)
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Some question", agents)
        self.assertEqual(plan.agents_to_use, ["rag"])

    async def test_unknown_agent_name_filtered_out(self):
        """Agent names returned by LLM that are not registered must be ignored."""
        router = _make_router('{"agents": ["rag", "nonexistent"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Question", agents)
        self.assertNotIn("nonexistent", plan.agents_to_use)
        self.assertIn("rag", plan.agents_to_use)

    async def test_all_unknown_names_falls_back_to_rag(self):
        """If all LLM-selected names are unknown, fall back to rag."""
        router = _make_router('{"agents": ["ghost", "phantom"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Question", agents)
        self.assertEqual(plan.agents_to_use, ["rag"])

    async def test_no_agents_returns_default_plan(self):
        """Empty agent list returns default QueryPlan without calling LLM."""
        llm = MagicMock()
        llm.generate = AsyncMock()
        router = QueryRouter(llm)
        plan = await router.route("Question", [])
        llm.generate.assert_not_called()
        self.assertEqual(plan.agents_to_use, ["rag"])

    async def test_prompt_contains_agent_capability(self):
        """Each agent's capability_prompt must appear in the assembled prompt."""
        llm = MagicMock()
        llm.generate = AsyncMock(return_value='{"agents": ["rag"]}')
        router = QueryRouter(llm)
        agents = [
            _make_agent("rag", "UNIQUE_RAG_CAPABILITY"),
            _make_agent("metadata", "UNIQUE_META_CAPABILITY"),
        ]
        await router.route("Question", agents)
        prompt = llm.generate.call_args.kwargs["prompt"]
        self.assertIn("UNIQUE_RAG_CAPABILITY", prompt)
        self.assertIn("UNIQUE_META_CAPABILITY", prompt)
        self.assertIn("[AGENT: rag]", prompt)
        self.assertIn("[AGENT: metadata]", prompt)

    async def test_prompt_contains_question(self):
        llm = MagicMock()
        llm.generate = AsyncMock(return_value='{"agents": ["rag"]}')
        router = QueryRouter(llm)
        agents = [_make_agent("rag", "semantic")]
        await router.route("UNIQUE_QUESTION_TEXT", agents)
        prompt = llm.generate.call_args.kwargs["prompt"]
        self.assertIn("UNIQUE_QUESTION_TEXT", prompt)


if __name__ == "__main__":
    unittest.main()
