"""
Continuation agent — answers follow-up chat turns by re-fetching evidence
already retrieved earlier in the conversation instead of running a new
search. See docs/query-routing.md's "Follow-up conversations" section.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from backend.db.vector_store import VectorStore
from backend.models.conversation import ChatTurn
from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector

logger = logging.getLogger(__name__)


class ContinuationAgent(BaseAgent):
    """Elaborates on, clarifies, or compares evidence already retrieved in
    this conversation, without running a new search."""

    def __init__(self, vector_store: VectorStore):
        self._vector_store = vector_store

    @property
    def name(self) -> str:
        return "continuation"

    @property
    def capability_prompt(self) -> str:
        return (
            "Use when the follow-up elaborates on, clarifies, or compares evidence "
            "already retrieved in this conversation, without needing a new search "
            "(e.g. 'explain that more', 'what does source 3 say about X', "
            "'how do these two sources differ'). Only applicable when there is a "
            "conversation history — never select this for the first question."
        )

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace: Optional["TraceCollector"] = None,
        **kwargs,
    ) -> AgentResult:
        conversation_history: list[ChatTurn] = kwargs.get("conversation_history") or []

        prior_refs: list[str] = []
        for turn in conversation_history:
            for ref in turn.source_refs:
                if ref not in prior_refs:
                    prior_refs.append(ref)

        chunks: list[dict] = []
        if prior_refs:
            chunks = await asyncio.to_thread(self._vector_store.get_chunks_by_ids, prior_refs)
            if len(chunks) < len(prior_refs):
                logger.info(
                    "ContinuationAgent: %d of %d prior evidence chunks no longer found",
                    len(prior_refs) - len(chunks), len(prior_refs),
                )

        context_text = _render_history(conversation_history)
        if chunks:
            context_text += "\n\n" + _render_chunks(chunks)
        elif prior_refs:
            context_text += (
                "\n\n(The specific passages cited earlier are no longer available — "
                "answer from the conversation history above only.)"
            )

        return AgentResult(
            agent_name=self.name,
            context_text=context_text,
            sources=_sources_from_chunks(chunks),
            source_refs=[c["payload"]["chunk_id"] for c in chunks if c["payload"].get("chunk_id")],
        )


def _render_history(history: list[ChatTurn]) -> str:
    if not history:
        return ""
    lines = ["Conversation so far:"]
    for turn in history:
        lines.append(f"Q: {turn.question}")
        lines.append(f"A: {turn.answer}")
    return "\n".join(lines)


def _render_chunks(chunks: list[dict]) -> str:
    lines = ["Previously retrieved passages:"]
    for i, c in enumerate(chunks, 1):
        payload = c["payload"]
        title = payload.get("title") or "Unknown Document"
        page = payload.get("page_number")
        page_label = f" [p. {page}]" if page else ""
        lines.append(f"[S{i}: {title}{page_label}]\n{payload.get('text', '')}")
    return "\n\n".join(lines)


def _sources_from_chunks(chunks: list[dict]) -> list[dict]:
    sources = []
    for c in chunks:
        payload = c["payload"]
        sources.append(dict(
            item_id=payload.get("item_key", "unknown"),
            library_id=payload.get("library_id", ""),
            title=payload.get("title") or "Unknown Document",
            authors=payload.get("authors") or [],
            year=payload.get("year"),
            page_number=payload.get("page_number"),
            text_anchor=payload.get("text_preview"),
            score=1.0,
            chunk_id=payload.get("chunk_id"),
        ))
    return sources
