"""
RAG agent — wraps RAGEngine as a registered query agent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from backend.config.settings import Settings
from backend.db.vector_store import VectorStore
from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent
from backend.services.embeddings import EmbeddingService
from backend.services.llm import LLMService
from backend.services.rag_engine import RAGEngine

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector

logger = logging.getLogger(__name__)


class RAGAgent(BaseAgent):
    """Semantic search agent backed by the existing RAGEngine."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        llm_service: LLMService,
        vector_store: VectorStore,
        settings: Settings,
    ):
        self._embedding_service = embedding_service
        self._llm_service = llm_service
        self._vector_store = vector_store
        self._settings = settings

    @property
    def name(self) -> str:
        return "rag"

    @property
    def capability_prompt(self) -> str:
        return (
            "Performs semantic search over indexed document content to find relevant passages.\n"
            "Best for: questions about content, arguments, definitions, quotes, or explanations.\n"
            "Also applies metadata filters (author, year range, item type, title keywords)\n"
            "to narrow the semantic search space when provided."
        )

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace: Optional[TraceCollector] = None,
        **kwargs,
    ) -> AgentResult:
        top_k: int = kwargs.get("top_k", 5)
        min_score: float = kwargs.get("min_score", 0.3)

        engine = RAGEngine(
            embedding_service=self._embedding_service,
            llm_service=self._llm_service,
            vector_store=self._vector_store,
            settings=self._settings,
        )
        result = await engine.query(
            question=question,
            library_ids=library_ids,
            top_k=top_k,
            min_score=min_score,
            filters=filters,
            trace=trace,
        )

        sources = result.sources
        return AgentResult(
            agent_name=self.name,
            context_text=result.answer,
            sources=sources,
        )
