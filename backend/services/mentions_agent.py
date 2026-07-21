"""
Mentions agent — formats client-gathered full-text citation evidence.

Unlike RAGAgent/MetadataAgent, this agent never queries the backend's own
storage: "who cites work X" can only be answered from the *citing* document's
full text, and the backend has no reliable index for that (Qdrant chunks are
sized/embedded for semantic search, not lexical citation lookup). Instead,
the Zotero client's own local full-text search index — built for every
downloaded attachment — is scanned for mentions client-side, and the
resulting snippets are shipped to the backend in the request. See
docs/query-routing.md for the two-phase request/response protocol this
agent depends on.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel

from backend.models.filters import CitationTarget, MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent
from backend.services.metadata_agent import _format_authors


class TargetMatch(BaseModel):
    """Evidence for one citation_targets[i] found in one document's full text."""

    count: int = 0
    snippets: list[str] = []
    is_self: bool = False  # True if the document appears to BE the cited work itself


class MentionEvidenceItem(BaseModel):
    """One document's full-text search evidence, gathered client-side."""

    item_key: str
    library_id: str
    title: str
    authors: list[str] = []
    year: Optional[int] = None
    target_matches: dict[str, TargetMatch] = {}  # key = str(index into citation_targets)
    partial_index: bool = False


class ClientEvidence(BaseModel):
    """Wire shape of the `client_evidence` field on POST /api/query."""

    items: list[MentionEvidenceItem] = []
    truncated: bool = False
    total_candidates: int = 0


def _evidence_to_context(evidence: ClientEvidence, citation_targets: list[CitationTarget]) -> str:
    if not evidence.items:
        return (
            "No publications in this library's locally indexed full text mention "
            "the requested work(s)."
        )

    labels = [t.author.title() + (f" ({t.year})" if t.year else "") for t in citation_targets]
    lines = [
        "Publications whose full text mentions " + " and ".join(labels)
        + " (found via the user's local Zotero full-text search index — coverage is "
        "limited to attachments downloaded and indexed on the user's machine; a mention "
        "is word co-occurrence, not a verified citation — judge each snippet yourself):\n"
    ]
    for i, item in enumerate(evidence.items, 1):
        year_str = f" ({item.year})" if item.year else ""
        lines.append(f"[S{i}] {_format_authors(item.authors)}{year_str} — {item.title}")
        for idx, label in enumerate(labels):
            match = item.target_matches.get(str(idx))
            if not match:
                continue
            if match.is_self:
                lines.append(
                    f"      NOTE: this item appears to BE the {label} work itself — "
                    "do not list it as one of the citing publications."
                )
                continue
            lines.append(f"      Mentions \"{label}\" ({match.count} occurrence(s)):")
            for snippet in match.snippets:
                lines.append(f"        \"...{snippet}...\"")
        if item.partial_index:
            lines.append(
                "      NOTE: this document's full-text index is incomplete "
                "(page/length limit) — mentions may be missing, especially near the end."
            )
    if evidence.truncated:
        lines.append(
            f"\n(Showing the top {len(evidence.items)} of {evidence.total_candidates} "
            "matching documents — ask a narrower question to see the rest.)"
        )
    return "\n".join(lines)


class MentionsAgent(BaseAgent):
    """Formats client-supplied citation/mention evidence for synthesis.

    Requires `client_evidence` (a `ClientEvidence`) in execute()'s kwargs.
    QueryOrchestrator guarantees this agent is only invoked once that
    evidence has been supplied — see NeedsClientEvidenceError.
    """

    @property
    def name(self) -> str:
        return "mentions"

    @property
    def capability_prompt(self) -> str:
        return (
            "Finds publications that CITE, DISCUSS, RESPOND TO, or MENTION a specific named "
            "work — as opposed to publications AUTHORED by someone.\n"
            "Use when the question asks who cites/references/discusses a named author's work "
            "(e.g. \"which publications cite Wiethölter's 1975 article\", \"who discusses "
            "Teubner's Globale Bukowina\").\n"
            "Populate citation_targets (NOT authors) with one entry per cited work: author "
            "surname, optional year, optional distinctive title keywords. Never put a cited "
            "author's name in `authors` — that field means 'written by', not 'cited by'.\n"
            "Retrieval runs against the user's local Zotero full-text index via a client round "
            "trip; results are approximate word co-occurrence, not verified citations, and only "
            "cover attachments the user has downloaded and indexed locally."
        )

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace=None,
        **kwargs,
    ) -> AgentResult:
        evidence: ClientEvidence = kwargs["client_evidence"]
        context_text = _evidence_to_context(evidence, filters.citation_targets)
        sources = [
            dict(
                item_id=item.item_key,
                library_id=item.library_id,
                title=item.title,
                authors=item.authors,
                year=item.year,
                score=1.0,
            )
            for item in evidence.items
            if not all(m.is_self for m in item.target_matches.values())
        ]
        return AgentResult(agent_name=self.name, context_text=context_text, sources=sources)
