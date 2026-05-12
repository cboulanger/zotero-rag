"""
Query orchestrator — agent registry, routing, parallel dispatch, and synthesis.

Usage:
    orchestrator = QueryOrchestrator(embedding_service, llm_service, vector_store, settings)
    result = await orchestrator.query(question, library_ids, top_k, min_score)

Custom agents can be registered before the first query:
    orchestrator.register(MyCustomAgent(...))
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from backend.config.settings import Settings
from backend.db.vector_store import VectorStore
from backend.models.filters import MetadataFilters
from backend.models.trace import FallbackTrace, LLMCallTrace
from backend.services.base_agent import AgentResult, BaseAgent, QueryPlan
from backend.services.embeddings import EmbeddingService
from backend.services.llm import LLMService
from backend.services.metadata_agent import MetadataAgent
from backend.services.query_router import QueryRouter
from backend.services.rag_agent import RAGAgent
from backend.services.rag_engine import QueryResult, SourceInfo

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector

logger = logging.getLogger(__name__)

_SYNTHESIS_PROMPT = """\
You are answering an academic research question using information retrieved from a library.

Question: {question}

Retrieved information:
{context_blocks}

Instructions:
- Provide a comprehensive answer based on the retrieved information above.
- For catalog listings, present items as a formatted list with author, year, and title.
- Cite every source you mention using [SN] notation (e.g. [S1], [S2]).
  Use [SN] for both catalog entries and quoted document passages.
  For document passages with a page number use [SN:P] (e.g. [S2:7]).
- Do not invent source numbers — only use the [SN] labels present in the retrieved information above.
- If no relevant information was found, say so clearly.
"""


class QueryOrchestrator:
    """
    Coordinates query routing, agent dispatch, and answer synthesis.

    Agents registered here contribute their capability_prompt to the router prompt
    at runtime. The router selects which agents to invoke for each question.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        llm_service: LLMService,
        vector_store: VectorStore,
        settings: Settings,
    ):
        self._agents: dict[str, BaseAgent] = {}
        self._llm_service = llm_service
        self._settings = settings
        self._register_defaults(embedding_service, llm_service, vector_store, settings)

    def register(self, agent: BaseAgent) -> None:
        """Register an agent under its name. Replaces any existing agent with the same name."""
        self._agents[agent.name] = agent
        logger.debug("QueryOrchestrator: registered agent '%s'", agent.name)

    def _register_defaults(
        self,
        embedding_service: EmbeddingService,
        llm_service: LLMService,
        vector_store: VectorStore,
        settings: Settings,
    ) -> None:
        self.register(RAGAgent(embedding_service, llm_service, vector_store, settings))
        self.register(MetadataAgent(vector_store))

    async def query(
        self,
        question: str,
        library_ids: list[str],
        top_k: int = 5,
        min_score: float = 0.3,
        enable_routing: bool = True,
        trace: Optional[TraceCollector] = None,
    ) -> QueryResult:
        """
        Route the question, run selected agents, and synthesize the final answer.

        Args:
            question: User's question.
            library_ids: Libraries to search (empty list searches all).
            top_k: Number of chunks for semantic search (RAGAgent).
            min_score: Minimum similarity score (RAGAgent).
            enable_routing: False skips the routing LLM call and goes straight to RAGAgent.
            trace: Optional collector for recording intermediate trace events.
        """
        # 1. Routing
        if enable_routing and len(self._agents) > 1:
            plan = await QueryRouter(self._llm_service).route(
                question, list(self._agents.values()), trace=trace
            )
            logger.info(
                "QueryOrchestrator: routing → agents=%s filters=%s description=%s",
                plan.agents_to_use,
                plan.filters.model_dump(exclude_defaults=True) or "{}",
                plan.routing_description,
            )
        else:
            plan = QueryPlan(agents_to_use=["rag"])

        # 2. Resolve agents (unknown names fall back to "rag")
        selected: list[BaseAgent] = [
            self._agents[n] for n in plan.agents_to_use if n in self._agents
        ]
        if not selected:
            fallback = self._agents.get("rag") or next(iter(self._agents.values()), None)
            if fallback:
                selected = [fallback]

        # 3. Execute in parallel
        agent_results: list[AgentResult] = await asyncio.gather(*[
            agent.execute(
                question=question,
                library_ids=library_ids,
                filters=plan.filters,
                trace=trace,
                top_k=top_k,
                min_score=min_score,
            )
            for agent in selected
        ])

        # 3b. RAG fallback — if no agent produced useful content and "rag" wasn't
        # already selected, run the RAG agent so we don't return an empty answer.
        if _all_empty(agent_results) and "rag" not in plan.agents_to_use:
            rag_agent = self._agents.get("rag")
            if rag_agent:
                logger.info(
                    "QueryOrchestrator: all agents returned empty — falling back to RAG"
                )
                if trace is not None:
                    trace.record(FallbackTrace())
                fallback_result = await rag_agent.execute(
                    question=question,
                    library_ids=library_ids,
                    filters=MetadataFilters(),  # clear filters for the fallback search
                    trace=trace,
                    top_k=top_k,
                    min_score=min_score,
                )
                return _rag_passthrough(
                    fallback_result, question,
                    model_name=self._llm_service.model_name if isinstance(self._llm_service.model_name, str) else None,
                    agents_used=plan.agents_to_use + ["rag"],
                )

        # 4. Synthesize or pass through
        agents_used = [a.name for a in selected]
        _raw_model = self._llm_service.model_name
        llm_model: Optional[str] = _raw_model if isinstance(_raw_model, str) else None
        if len(agent_results) == 1 and agent_results[0].agent_name == "rag":
            return _rag_passthrough(agent_results[0], question,
                                    model_name=llm_model, agents_used=agents_used)

        return await self._synthesize(question, plan, agent_results,
                                      model_name=llm_model, agents_used=agents_used,
                                      trace=trace)

    async def _synthesize(
        self,
        question: str,
        plan: QueryPlan,
        results: list[AgentResult],
        model_name: Optional[str] = None,
        agents_used: Optional[list[str]] = None,
        trace: Optional[TraceCollector] = None,
    ) -> QueryResult:
        # Renumber each agent's local [S1],[S2]... citations into a global sequence
        # so the merged sources list stays consistent with what the LLM sees.
        offset = 0
        renumbered_blocks = []
        for r in results:
            text = _shift_source_refs(r.context_text, offset)
            renumbered_blocks.append(f"[{r.agent_name.upper()} AGENT RESULTS]\n{text}")
            offset += len(r.sources)

        context_blocks = "\n\n---\n\n".join(renumbered_blocks)
        prompt = _SYNTHESIS_PROMPT.format(
            question=question,
            context_blocks=context_blocks,
        )

        preset = self._settings.get_hardware_preset()
        max_tokens = preset.llm.max_answer_tokens
        t_llm = time.monotonic()
        answer = await self._llm_service.generate(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.7,
        )
        llm_duration_ms = int((time.monotonic() - t_llm) * 1000)

        if trace is not None:
            trace.record(LLMCallTrace(
                call_type="synthesis",
                model=self._llm_service.model_name,
                prompt=prompt,
                response=answer,
                temperature=0.7,
                max_tokens=max_tokens,
                duration_ms=llm_duration_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

        sources = _merge_sources(results)
        return QueryResult(
            question=question,
            answer=answer,
            sources=sources,
            model_name=model_name,
            agents_used=agents_used or [],
        )


_EMPTY_MARKERS = frozenset([
    "no items found",
    "no relevant",
    "no information",
    "no results",
    "could not find",
])


def _all_empty(results: list[AgentResult]) -> bool:
    """Return True when every agent produced no sources and no meaningful content."""
    for r in results:
        if r.sources:
            return False
        text_lower = r.context_text.strip().lower()
        if text_lower and not any(m in text_lower for m in _EMPTY_MARKERS):
            return False
    return True


def _shift_source_refs(text: str, offset: int) -> str:
    """Add `offset` to every [SN] / [SN:P] citation number in text."""
    if offset == 0:
        return text

    def _replace(m: re.Match) -> str:
        n = int(m.group(1))
        page = m.group(2) or ""
        return f"[S{n + offset}{page}]"

    return re.sub(r'\[S(\d+)(:\d+)?\]', _replace, text)


def _rag_passthrough(
    agent_result: AgentResult,
    question: str,
    model_name: Optional[str] = None,
    agents_used: Optional[list[str]] = None,
) -> QueryResult:
    """Convert a single RAGAgent result directly to a QueryResult without synthesis."""
    sources: list[SourceInfo] = []
    for s in agent_result.sources:
        if isinstance(s, SourceInfo):
            sources.append(s)
        elif isinstance(s, dict):
            sources.append(SourceInfo(**s))
    return QueryResult(
        question=question,
        answer=agent_result.context_text,
        sources=sources,
        model_name=model_name,
        agents_used=agents_used or [],
    )


def _merge_sources(results: list[AgentResult]) -> list[SourceInfo]:
    """Merge and deduplicate sources from multiple agents (by item_id)."""
    seen: set[str] = set()
    merged: list[SourceInfo] = []
    for result in results:
        for s in result.sources:
            if isinstance(s, SourceInfo):
                item_id = s.item_id
                if item_id not in seen:
                    seen.add(item_id)
                    merged.append(s)
            elif isinstance(s, dict):
                item_id = s.get("item_id", "")
                if item_id not in seen:
                    seen.add(item_id)
                    try:
                        merged.append(SourceInfo(**s))
                    except Exception:
                        pass
    return merged
