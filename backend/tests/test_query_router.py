"""
Unit tests for QueryRouter:
- prompt assembly from registered agents
- JSON parsing with various edge cases
- MetadataFilters extraction
- Fallback to RAG-only plan on parse failure or unknown agent names
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from backend.models.conversation import ChatTurn
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

    async def test_extracts_citation_targets(self):
        router = _make_router(
            '{"agents": ["mentions"], "authors": [], '
            '"citation_targets": ['
            '{"author": "wiethölter", "year": 1975, "title_keywords": []}, '
            '{"author": "teubner", "title_keywords": ["bukowina"]}'
            ']}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route(
            "Which publications cite Wiethölter (1975) and Teubner's Globale Bukowina?", agents
        )
        self.assertEqual(len(plan.filters.citation_targets), 2)
        self.assertEqual(plan.filters.citation_targets[0].author, "wiethölter")
        self.assertEqual(plan.filters.citation_targets[0].year, 1975)
        self.assertEqual(plan.filters.citation_targets[1].title_keywords, ["bukowina"])
        self.assertEqual(plan.filters.authors, [])

    async def test_missing_citation_targets_defaults_to_empty(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What is autopoiesis?", agents)
        self.assertEqual(plan.filters.citation_targets, [])

    async def test_ignores_malformed_citation_target_entries(self):
        router = _make_router(
            '{"agents": ["mentions"], '
            '"citation_targets": [{"year": 1975}, "not a dict", {"author": "teubner"}]}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route("Which publications discuss teubner?", agents)
        self.assertEqual(len(plan.filters.citation_targets), 1)
        self.assertEqual(plan.filters.citation_targets[0].author, "teubner")

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

    async def test_prompt_warns_against_treating_tool_names_as_authors(self):
        """"Which features does Endnote have that Zotero does not?" was observed
        misrouted: the router extracted authors=["endnote"], which then made it
        look like an unconstrained by-author catalog request and triggered a
        spurious clarification_needed=true. The prompt must explicitly warn
        against treating reference-management tool/product names as authors."""
        llm = MagicMock()
        llm.generate = AsyncMock(return_value='{"agents": ["rag"]}')
        router = QueryRouter(llm)
        agents = [_make_agent("rag", "semantic"), _make_agent("metadata", "catalog")]
        await router.route("Which features does Endnote have that Zotero does not?", agents)
        prompt = llm.generate.call_args.kwargs["prompt"].lower()
        self.assertIn("not authors", prompt)
        self.assertIn("endnote", prompt)


class TestEntityMentionValidation(unittest.IsolatedAsyncioTestCase):
    """The router LLM can hallucinate authors/title_keywords/citation_targets
    that sound plausible for the topic but were never written in the
    question — observed live: a question with no named author ("Welche
    Beziehung besteht zwischen der systemtheoretischen Rechtssoziologie und
    der Rechtsdogmatik?") still got authors=["teubner", "wiethölter"]
    because those scholars are commonly associated with the topic. Any
    candidate not literally present in the question (allowing for
    diacritics/case/German inflection) must be dropped before it becomes a
    hard Qdrant filter."""

    async def test_drops_author_not_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "authors": ["teubner", "wiethölter"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route(
            "Welche Beziehung besteht zwischen der systemtheoretischen "
            "Rechtssoziologie und der Rechtsdogmatik?", agents,
        )
        self.assertEqual(plan.filters.authors, [])
        self.assertEqual(
            sorted(plan.dropped_filters["authors"]), ["teubner", "wiethölter"]
        )

    async def test_keeps_author_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "authors": ["luhmann"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What does Luhmann say about autopoiesis?", agents)
        self.assertEqual(plan.filters.authors, ["luhmann"])
        self.assertIsNone(plan.dropped_filters)

    async def test_keeps_author_mentioned_with_diacritic_variant(self):
        """The question may spell a name without its diacritic even though
        the router returns the canonical spelling, or vice versa."""
        router = _make_router('{"agents": ["rag"], "authors": ["wiethölter"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What did Wietholter argue?", agents)
        self.assertEqual(plan.filters.authors, ["wiethölter"])

    async def test_keeps_author_mentioned_in_inflected_german_form(self):
        """German genitive ("Wiethölters") must still count as a mention of
        "wiethölter" — substring containment after normalization handles
        this without a stemmer."""
        router = _make_router('{"agents": ["rag"], "authors": ["wiethölter"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Was ist Wiethölters Hauptargument?", agents)
        self.assertEqual(plan.filters.authors, ["wiethölter"])

    async def test_drops_title_keyword_not_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "title_keywords": ["bukowina"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What is systems theory?", agents)
        self.assertEqual(plan.filters.title_keywords, [])
        self.assertEqual(plan.dropped_filters["title_keywords"], ["bukowina"])

    async def test_keeps_title_keyword_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "title_keywords": ["bukowina"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Find the paper called 'Globale Bukowina'", agents)
        self.assertEqual(plan.filters.title_keywords, ["bukowina"])

    async def test_drops_citation_target_author_not_mentioned(self):
        router = _make_router(
            '{"agents": ["mentions"], '
            '"citation_targets": [{"author": "wiethölter", "year": 1975, "title_keywords": []}]}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route("Which publications discuss legal sociology?", agents)
        self.assertEqual(plan.filters.citation_targets, [])
        self.assertEqual(plan.dropped_filters["citation_targets.author"], ["wiethölter"])

    async def test_keeps_citation_target_author_mentioned(self):
        router = _make_router(
            '{"agents": ["mentions"], '
            '"citation_targets": [{"author": "wiethölter", "year": 1975, "title_keywords": []}]}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route("Which publications cite Wiethölter's 1975 article?", agents)
        self.assertEqual(len(plan.filters.citation_targets), 1)
        self.assertEqual(plan.filters.citation_targets[0].author, "wiethölter")

    async def test_dropped_filters_is_none_when_nothing_dropped(self):
        router = _make_router('{"agents": ["rag"], "authors": ["luhmann"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What does Luhmann argue?", agents)
        self.assertIsNone(plan.dropped_filters)


class TestConversationHistoryInPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_history_included_in_prompt(self):
        router = _make_router('{"agents": ["continuation"]}')
        agents = [_make_agent("continuation", "cap")]
        history = [ChatTurn(question="First question", answer="First answer")]

        await router.route("Follow-up", agents, conversation_history=history)

        sent_prompt = router._llm.generate.call_args.kwargs["prompt"]
        self.assertIn("First question", sent_prompt)
        self.assertIn("First answer", sent_prompt)

    async def test_no_conversation_history_omits_block(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "cap")]

        await router.route("A fresh question", agents)

        sent_prompt = router._llm.generate.call_args.kwargs["prompt"]
        self.assertNotIn("Conversation so far", sent_prompt)

    async def test_long_history_is_truncated_to_max_chars(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "cap")]
        history = [
            ChatTurn(question=f"Q{i}" * 50, answer=f"A{i}" * 50) for i in range(10)
        ]

        await router.route("Follow-up", agents, conversation_history=history,
                            max_conversation_context_chars=200)

        sent_prompt = router._llm.generate.call_args.kwargs["prompt"]
        # Only the most recent turn(s) fit under a 200-char budget — the first turn's
        # question text must have been dropped.
        self.assertNotIn("Q0" * 50, sent_prompt)
        self.assertIn("Q9" * 50, sent_prompt)


class TestClarificationParsing(unittest.IsolatedAsyncioTestCase):
    async def test_parses_clarification_needed_true(self):
        router = _make_router(
            '{"agents": ["metadata"], "clarification_needed": true, '
            '"clarification_question": "Which years?"}'
        )
        agents = [_make_agent("metadata", "cap")]
        plan = await router.route("What has Luhmann written?", agents)
        self.assertTrue(plan.clarification_needed)
        self.assertEqual(plan.clarification_question, "Which years?")

    async def test_clarification_needed_defaults_false(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "cap")]
        plan = await router.route("Q", agents)
        self.assertFalse(plan.clarification_needed)
        self.assertIsNone(plan.clarification_question)


if __name__ == "__main__":
    unittest.main()
