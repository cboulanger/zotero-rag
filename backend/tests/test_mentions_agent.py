"""
Unit tests for MentionsAgent and its evidence-formatting helpers.
"""

import unittest

from backend.models.filters import CitationTarget, MetadataFilters
from backend.services.mentions_agent import (
    ClientEvidence, MentionEvidenceItem, MentionsAgent, TargetMatch, _evidence_to_context,
)


class TestEvidenceToContext(unittest.TestCase):

    def test_no_items(self):
        text = _evidence_to_context(ClientEvidence(), [CitationTarget(author="teubner")])
        self.assertIn("No publications", text)

    def test_formats_snippet_and_count(self):
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="ABC", library_id="1", title="Systemtheorie",
                authors=["Fischer-Lescano, A."], year=2013,
                target_matches={"0": TargetMatch(count=5, snippets=["...Wiethölter, der..."])},
            )
        ])
        text = _evidence_to_context(evidence, [CitationTarget(author="wiethölter")])
        self.assertIn("[S1]", text)
        self.assertIn("Systemtheorie", text)
        self.assertIn("5 occurrence", text)
        self.assertIn("Wiethölter, der", text)

    def test_self_citation_noted_not_listed_as_citer(self):
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="SELF", library_id="1", title="Globale Bukowina",
                authors=["Teubner, G."],
                target_matches={"0": TargetMatch(count=3, is_self=True)},
            )
        ])
        text = _evidence_to_context(
            evidence, [CitationTarget(author="teubner", title_keywords=["bukowina"])]
        )
        self.assertIn("appears to BE", text)

    def test_partial_index_flag_noted(self):
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="P", library_id="1", title="T",
                target_matches={"0": TargetMatch(count=1)}, partial_index=True,
            )
        ])
        text = _evidence_to_context(evidence, [CitationTarget(author="x")])
        self.assertIn("incomplete", text)

    def test_truncation_noted(self):
        evidence = ClientEvidence(
            items=[MentionEvidenceItem(
                item_key="A", library_id="1", title="T",
                target_matches={"0": TargetMatch(count=1)},
            )],
            truncated=True, total_candidates=99,
        )
        text = _evidence_to_context(evidence, [CitationTarget(author="x")])
        self.assertIn("top 1 of 99", text)


class TestMentionsAgentExecute(unittest.IsolatedAsyncioTestCase):

    async def test_excludes_self_citation_from_sources_but_keeps_genuine_citer(self):
        agent = MentionsAgent()
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="SELF", library_id="1", title="Globale Bukowina",
                authors=["Teubner, G."],
                target_matches={"0": TargetMatch(count=3, is_self=True)},
            ),
            MentionEvidenceItem(
                item_key="CITER", library_id="1", title="Systemtheorie",
                authors=["Fischer-Lescano, A."],
                target_matches={"0": TargetMatch(count=2)},
            ),
        ])
        result = await agent.execute(
            question="Who cites Teubner?", library_ids=["1"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
            client_evidence=evidence,
        )
        source_ids = [s["item_id"] for s in result.sources]
        self.assertNotIn("SELF", source_ids)
        self.assertIn("CITER", source_ids)

    async def test_context_text_mentions_citer(self):
        agent = MentionsAgent()
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="CITER", library_id="1", title="Systemtheorie",
                target_matches={"0": TargetMatch(count=2, snippets=["...teubner..."])},
            ),
        ])
        result = await agent.execute(
            question="Who cites Teubner?", library_ids=["1"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
            client_evidence=evidence,
        )
        self.assertIn("Systemtheorie", result.context_text)
