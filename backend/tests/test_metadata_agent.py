"""
Unit tests for MetadataAgent.
"""

import unittest
from unittest.mock import MagicMock, patch

from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult
from backend.services.metadata_agent import MetadataAgent, MetadataResult, _format_authors, _results_to_context


class TestFormatAuthors(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_format_authors([]), "Unknown")

    def test_single(self):
        self.assertEqual(_format_authors(["Luhmann, N."]), "Luhmann, N.")

    def test_two(self):
        self.assertEqual(_format_authors(["Luhmann, N.", "Habermas, J."]), "Luhmann, N. & Habermas, J.")

    def test_three_or_more(self):
        self.assertEqual(_format_authors(["Luhmann, N.", "Habermas, J.", "Parsons, T."]), "Luhmann, N. et al.")


class TestResultsToContext(unittest.TestCase):

    def test_empty_results(self):
        text = _results_to_context([])
        self.assertIn("No items found", text)

    def test_formats_single_result(self):
        result = MetadataResult(
            item_id="ABC",
            library_id="1",
            title="Social Systems",
            authors=["Luhmann, N."],
            year=1984,
            item_type="book",
            text_preview="Systems theory is",
        )
        text = _results_to_context([result])
        self.assertIn("[S1]", text)
        self.assertIn("Social Systems", text)
        self.assertIn("1984", text)
        self.assertIn("book", text)
        self.assertIn("Systems theory is", text)

    def test_no_year_or_type(self):
        result = MetadataResult(
            item_id="XYZ",
            library_id="1",
            title="Unknown Document",
            authors=[],
            year=None,
            item_type=None,
            text_preview=None,
        )
        text = _results_to_context([result])
        self.assertIn("[S1]", text)
        self.assertNotIn("None", text)

    def test_catalog_only_item_is_flagged(self):
        """An item with no indexed full text (has_content=False) must be marked
        so the synthesis LLM doesn't silently treat 'no passages found' as
        'nothing exists' — it exists in the catalog, just isn't searchable."""
        result = MetadataResult(
            item_id="WASSERMANN1",
            library_id="1",
            title="Der soziale Zivilprozess",
            authors=["Rudolf Wassermann"],
            year=1973,
            item_type="book",
            text_preview=None,
            has_content=False,
        )
        text = _results_to_context([result])
        self.assertIn("Der soziale Zivilprozess", text)
        self.assertIn("full text not indexed", text.lower())

    def test_normal_item_is_not_flagged(self):
        result = MetadataResult(
            item_id="ABC",
            library_id="1",
            title="Social Systems",
            authors=["Luhmann, N."],
            year=1984,
            item_type="book",
            text_preview="Systems theory is",
        )
        text = _results_to_context([result])
        self.assertNotIn("full text not indexed", text.lower())


class TestMetadataAgentExecute(unittest.IsolatedAsyncioTestCase):

    def _make_agent(self, payloads: list[dict]) -> MetadataAgent:
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        return MetadataAgent(store)

    async def test_returns_agent_result(self):
        agent = self._make_agent([])
        result = await agent.execute(
            question="List books by Luhmann",
            library_ids=["1"],
            filters=MetadataFilters(),
        )
        self.assertIsInstance(result, AgentResult)
        self.assertEqual(result.agent_name, "metadata")

    async def test_passes_library_ids_and_filters(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        filters = MetadataFilters(authors=["luhmann"])
        await agent.execute(
            question="Books by Luhmann",
            library_ids=["42"],
            filters=filters,
        )
        store.get_items_by_metadata.assert_called_once_with(
            library_ids=["42"],
            filters=filters,
            limit=unittest.mock.ANY,
        )

    async def test_empty_library_ids_passed_as_none(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(question="Q", library_ids=[], filters=MetadataFilters())
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertIsNone(call_kwargs["library_ids"])

    async def test_builds_sources_from_payloads(self):
        agent = self._make_agent([
            {"item_key": "ABC", "library_id": "1", "title": "T", "authors": [], "year": 2000, "item_type": "book"},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0]["item_id"], "ABC")
        self.assertEqual(result.sources[0]["score"], 1.0)

    async def test_results_sorted_by_year(self):
        agent = self._make_agent([
            {"item_key": "C", "library_id": "1", "title": "C", "authors": [], "year": 2000},
            {"item_key": "A", "library_id": "1", "title": "A", "authors": [], "year": 1980},
            {"item_key": "B", "library_id": "1", "title": "B", "authors": [], "year": None},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        years = [s["year"] for s in result.sources]
        # year=None should be last
        self.assertEqual(years[-1], None)
        # 1980 before 2000
        self.assertLess(years.index(1980) if 1980 in years else 0,
                        years.index(2000) if 2000 in years else 1)

    async def test_tags_propagate_from_payload_into_trace(self):
        agent = self._make_agent([
            {"item_key": "TAGGED1", "library_id": "1", "title": "T", "authors": [],
             "tags": ["Rechtssoziologie"]},
        ])
        trace = MagicMock()
        await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters(), trace=trace)
        recorded = trace.record.call_args.args[0]
        self.assertEqual(recorded.catalog_results[0]["tags"], ["Rechtssoziologie"])

    def test_metadata_result_carries_tags(self):
        result = MetadataResult(
            item_id="TAGGED1",
            library_id="1",
            title="Tagged Item",
            authors=[],
            year=None,
            item_type=None,
            text_preview=None,
            tags=["Rechtssoziologie"],
        )
        self.assertEqual(result.tags, ["Rechtssoziologie"])

    async def test_has_content_false_propagates_from_payload(self):
        agent = self._make_agent([
            {"item_key": "STUB1", "library_id": "1", "title": "Stub", "authors": [],
             "has_content": False},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertIn("full text not indexed", result.context_text.lower())

    async def test_has_content_defaults_true_for_legacy_payloads(self):
        """Payloads written before the has_content field existed must not be
        misflagged as catalog-only."""
        agent = self._make_agent([
            {"item_key": "OLD1", "library_id": "1", "title": "Old Item", "authors": []},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertNotIn("full text not indexed", result.context_text.lower())

    async def test_context_text_contains_items(self):
        agent = self._make_agent([
            {"item_key": "X", "library_id": "1", "title": "Social Systems", "authors": ["Luhmann, N."], "year": 1984},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertIn("Social Systems", result.context_text)
        self.assertIn("1984", result.context_text)

    async def test_narrowing_threshold_controls_fetch_limit(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(
            question="Q", library_ids=[], filters=MetadataFilters(),
            metadata_narrowing_threshold=10,
        )
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertEqual(call_kwargs["limit"], 11)

    async def test_default_narrowing_threshold_is_50(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(question="Q", library_ids=[], filters=MetadataFilters())
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertEqual(call_kwargs["limit"], 51)

    async def test_over_threshold_returns_clarification_not_items(self):
        payloads = [
            {"item_key": f"I{i}", "library_id": "1", "title": f"T{i}", "authors": []}
            for i in range(11)
        ]
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        agent = MetadataAgent(store)
        result = await agent.execute(
            question="Q", library_ids=["1"], filters=MetadataFilters(),
            metadata_narrowing_threshold=10,
        )
        self.assertTrue(result.needs_clarification)
        self.assertIn("more than 10", result.clarification_message)
        self.assertEqual(result.sources, [])

    async def test_under_threshold_returns_normal_items(self):
        payloads = [
            {"item_key": "I1", "library_id": "1", "title": "T1", "authors": []},
        ]
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        agent = MetadataAgent(store)
        result = await agent.execute(
            question="Q", library_ids=["1"], filters=MetadataFilters(),
            metadata_narrowing_threshold=10,
        )
        self.assertFalse(result.needs_clarification)
        self.assertEqual(len(result.sources), 1)

    async def test_source_refs_populated_from_chunk_id(self):
        payloads = [
            {"item_key": "I1", "library_id": "1", "title": "T1", "authors": [], "chunk_id": "c1"},
        ]
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        agent = MetadataAgent(store)
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertEqual(result.source_refs, ["c1"])


if __name__ == "__main__":
    unittest.main()
