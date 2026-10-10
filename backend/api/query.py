"""
Query API endpoints for RAG queries.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List, Literal, Optional
from markdown_it import MarkdownIt

from backend.models.conversation import ChatTurn
from backend.models.filters import CitationTarget
from backend.models.trace import QueryTrace
from backend.services.access_gate import assert_can_access
from backend.services.base_agent import (
    NeedsClarificationError, NeedsClientEvidenceError, NeedsUserInputError, QueryPlan,
)
from backend.services.mentions_agent import ClientEvidence
from backend.services.query_orchestrator import QueryOrchestrator
from backend.services.rag_engine import (
    _DIVERSITY_FLOOR, _DIVERSITY_ESCALATION_FACTOR, _DIVERSITY_ESCALATION_MAX_TOP_K,
    _MAX_CHUNKS_PER_DOCUMENT, _LOW_DIVERSITY_AVAILABLE_FLOOR,
)
from backend.services.trace_collector import TraceCollector
from backend.services.zotero_identity import ZoteroIdentity
from backend.services.embeddings import (
    EmbeddingAuthenticationError, EmbeddingConfigurationError, EmbeddingEndpointUnavailableError,
    EmbeddingRateLimitExhaustedError,
)
from backend.services.llm import LLMConfigurationError, LLMEndpointUnavailableError
from backend.db.vector_store import VectorStore, VectorStoreError, VectorStoreTimeoutError
from backend.config.settings import get_settings
from backend.providers import ProviderConfigError, get_providers
from backend.dependencies import get_client_api_keys, get_vector_store, get_zotero_identity, make_embedding_service, make_llm_service

def _llm_has_live_models(preset) -> bool:
    """True when the LLM provider offers a live model list (so any listed model is allowed)."""
    try:
        return get_providers(preset)["llm"].has_live_models
    except ProviderConfigError:
        return False


router = APIRouter()
logger = logging.getLogger(__name__)


class SourceCitation(BaseModel):
    """Source citation with location information."""
    item_id: str
    library_id: str
    title: str
    page_number: Optional[int] = None
    text_anchor: Optional[str] = None  # First 5 words of chunk
    relevance_score: float


class QueryRequest(BaseModel):
    """RAG query request."""
    question: str
    library_ids: List[str]
    top_k: Optional[int] = None  # Number of chunks to retrieve (uses preset default if not specified)
    min_score: Optional[float] = None  # Minimum similarity score (uses preset default if not specified)
    enable_routing: bool = True  # False skips routing LLM call (backward-compatible pure-RAG mode)
    llm_model: Optional[str] = None  # Override preset default; must be in preset's model_names list
    include_trace: bool = False  # When True, attach a full execution trace to the response
    client_evidence: Optional[ClientEvidence] = None  # gathered client-side, resubmit round trip
    query_plan: Optional[QueryPlan] = None  # echoed back from a prior "needs_client_evidence" response
    conversation_history: List[ChatTurn] = []  # prior turns of a follow-up chat conversation
    force_fresh_retrieval: bool = False        # ignore conversation_history for routing this turn

    # Retrieval/answer-diversity tuning — client-configurable (plugin Preferences),
    # not exposed in the query dialog itself. See rag_engine.py's module docstrings
    # for _DIVERSITY_FLOOR / _MAX_CHUNKS_PER_DOCUMENT / _LOW_DIVERSITY_AVAILABLE_FLOOR
    # for what each one does; defaults here match those.
    diversity_floor: Optional[int] = None
    diversity_escalation_factor: Optional[int] = None
    diversity_escalation_max_top_k: Optional[int] = None
    max_chunks_per_document: Optional[int] = None
    low_diversity_available_floor: Optional[int] = None
    enable_quality_self_review: bool = False
    # When True, a thin/low-coverage answer triggers one escalated-retrieval
    # retry before returning (see rag_engine.py's _thin_context_coverage).
    # Off by default — see docs/superpowers/specs/2026-10-05-routing-retrieval-quality-design.md §6.


class QueryResponse(BaseModel):
    """RAG query response."""
    question: str
    answer: str
    answer_format: str  # Format of answer: "text", "html", or "markdown"
    sources: List[SourceCitation]
    library_ids: List[str]
    model_name: Optional[str] = None
    agents_used: List[str] = []
    library_document_counts: dict[str, int] = {}
    trace: Optional[QueryTrace] = None  # Populated when include_trace=True
    status: Literal["complete", "needs_client_evidence", "needs_clarification"] = "complete"
    citation_targets: List[CitationTarget] = []  # populated when status == "needs_client_evidence"
    query_plan: Optional[QueryPlan] = None  # echo back on the resubmit round trip
    source_refs: List[str] = []                       # union of every used source's chunk_id,
                                                        # echoed back verbatim on the next turn
    clarification_message: Optional[str] = None        # populated when status == "needs_clarification"


def _needs_evidence_response(query: QueryRequest, exc: NeedsClientEvidenceError) -> QueryResponse:
    """Map a NeedsClientEvidenceError to a placeholder response telling the client
    to gather full-text citation evidence locally and resubmit with it attached."""
    return QueryResponse(
        question=query.question,
        answer="",
        answer_format="text",
        sources=[],
        library_ids=query.library_ids,
        status="needs_client_evidence",
        citation_targets=exc.citation_targets,
        query_plan=exc.plan,
    )


def _needs_clarification_response(query: QueryRequest, exc: NeedsClarificationError) -> QueryResponse:
    """Map a NeedsClarificationError to a response asking the user to narrow their question."""
    return QueryResponse(
        question=query.question,
        answer="",
        answer_format="text",
        sources=[],
        library_ids=query.library_ids,
        status="needs_clarification",
        clarification_message=exc.message,
        query_plan=exc.plan,
    )


@router.post("/query", response_model=QueryResponse)
async def query_libraries(
    query: QueryRequest,
    http_request: Request,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Query indexed libraries with a question.

    Uses RAG to retrieve relevant context from indexed documents
    and generate an answer using an LLM.

    Args:
        request: Query request with question and library IDs.

    Returns:
        Answer with source citations including page numbers and text anchors.

    Raises:
        HTTPException: If query fails or libraries not indexed.
    """
    if not query.library_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one library ID must be provided"
        )

    for library_id in query.library_ids:
        assert_can_access(identity, library_id)

    if not query.question.strip():
        raise HTTPException(
            status_code=400,
            detail="Question cannot be empty"
        )

    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        # Initialize services with client-supplied API keys
        settings = get_settings()
        preset = settings.get_hardware_preset()
        client_keys = get_client_api_keys(http_request)

        embedding_service = make_embedding_service(client_keys)
        # For a provider with a live model list the allowed set is dynamic; skip static validation.
        if query.llm_model and not _llm_has_live_models(preset) and query.llm_model not in preset.llm.model_names:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{query.llm_model}' not in preset model list: {preset.llm.model_names}"
            )
        llm_service = make_llm_service(client_keys, model_name_override=query.llm_model or None)

        # Use preset defaults if not specified in request
        top_k = query.top_k if query.top_k is not None else preset.rag.top_k
        min_score = query.min_score if query.min_score is not None else preset.rag.score_threshold

        # Retrieval/answer-diversity tuning — client-configurable, falls back to the
        # hardcoded defaults in rag_engine.py when not overridden by the plugin.
        diversity_floor = query.diversity_floor if query.diversity_floor is not None else _DIVERSITY_FLOOR
        diversity_escalation_factor = (
            query.diversity_escalation_factor
            if query.diversity_escalation_factor is not None else _DIVERSITY_ESCALATION_FACTOR
        )
        diversity_escalation_max_top_k = (
            query.diversity_escalation_max_top_k
            if query.diversity_escalation_max_top_k is not None else _DIVERSITY_ESCALATION_MAX_TOP_K
        )
        max_chunks_per_document = (
            query.max_chunks_per_document
            if query.max_chunks_per_document is not None else _MAX_CHUNKS_PER_DOCUMENT
        )
        low_diversity_available_floor = (
            query.low_diversity_available_floor
            if query.low_diversity_available_floor is not None else _LOW_DIVERSITY_AVAILABLE_FLOOR
        )

        # Validate that at least one library is indexed; collect per-library document counts
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        indexed_count = 0
        library_document_counts: dict[str, int] = {}
        for library_id in query.library_ids:
            count = (await asyncio.to_thread(
                vector_store.client.count,
                collection_name=vector_store.CHUNKS_COLLECTION,
                count_filter=Filter(
                    must=[
                        FieldCondition(
                            key="library_id",
                            match=MatchValue(value=library_id)
                        )
                    ]
                ),
            )).count
            if count > 0:
                indexed_count += 1
            meta = await asyncio.to_thread(vector_store.get_library_metadata, library_id)
            if meta and meta.total_items_indexed > 0:
                library_document_counts[library_id] = meta.total_items_indexed

        if indexed_count == 0:
            raise HTTPException(
                status_code=400,
                detail="None of the specified libraries have been indexed. Please index the libraries before querying."
            )

        # Create orchestrator and run query (routing is enabled by default)
        orchestrator = QueryOrchestrator(
            embedding_service=embedding_service,
            llm_service=llm_service,
            vector_store=vector_store,
            settings=settings,
        )
        trace_collector = TraceCollector(
            question=query.question,
            library_ids=query.library_ids,
            parameters={
                "top_k": top_k,
                "min_score": min_score,
                "enable_routing": query.enable_routing,
                "llm_model": query.llm_model,
                "diversity_floor": diversity_floor,
                "diversity_escalation_factor": diversity_escalation_factor,
                "diversity_escalation_max_top_k": diversity_escalation_max_top_k,
                "max_chunks_per_document": max_chunks_per_document,
                "low_diversity_available_floor": low_diversity_available_floor,
                "enable_quality_self_review": query.enable_quality_self_review,
            },
        ) if query.include_trace else None

        result = await orchestrator.query(
            question=query.question,
            library_ids=query.library_ids,
            top_k=top_k,
            min_score=min_score,
            enable_routing=query.enable_routing,
            trace=trace_collector,
            client_evidence=query.client_evidence,
            preset_plan=query.query_plan,
            diversity_floor=diversity_floor,
            diversity_escalation_factor=diversity_escalation_factor,
            diversity_escalation_max_top_k=diversity_escalation_max_top_k,
            max_chunks_per_document=max_chunks_per_document,
            low_diversity_available_floor=low_diversity_available_floor,
            enable_quality_self_review=query.enable_quality_self_review,
            conversation_history=query.conversation_history,
            force_fresh_retrieval=query.force_fresh_retrieval,
        )

        # Format citations
        sources = [
            SourceCitation(
                item_id=source.item_id,
                library_id=source.library_id,
                title=source.title,
                page_number=source.page_number,
                text_anchor=source.text_anchor,
                relevance_score=source.score
            )
            for source in result.sources
        ]

        # Convert markdown answer to HTML
        md = MarkdownIt()
        answer_html = md.render(result.answer)

        return QueryResponse(
            question=query.question,
            answer=answer_html,
            answer_format="html",
            sources=sources,
            library_ids=query.library_ids,
            model_name=result.model_name,
            agents_used=result.agents_used,
            library_document_counts=library_document_counts,
            trace=trace_collector.finalize() if trace_collector is not None else None,
            source_refs=result.source_refs,
        )

    except NeedsUserInputError as exc:
        if isinstance(exc, NeedsClientEvidenceError):
            return _needs_evidence_response(query, exc)
        if isinstance(exc, NeedsClarificationError):
            return _needs_clarification_response(query, exc)
        raise

    except VectorStoreTimeoutError as e:
        # Known, recoverable failure mode (the search backend is overloaded) —
        # a short warning is enough; a full traceback would just be log noise.
        # Include the real detail: a bare "try again" message hides whether
        # this is ordinary load or something that won't resolve by retrying
        # (e.g. the search backend itself failing for a specific reason).
        logger.warning(f"Query failed: {e}")
        raise HTTPException(
            status_code=504,
            detail=f"The search backend is taking longer than usual to respond ({e}). Please try again in a moment.",
        )

    except VectorStoreError as e:
        # Distinct from a timeout: Qdrant returned an explicit error (e.g. the
        # disk-full "No space left on device" case) — not transient load, so
        # don't frame it as "try again in a moment" the way the timeout above does.
        logger.warning(f"Query failed: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"The search backend returned an error: {e}",
        )

    except (EmbeddingEndpointUnavailableError, LLMEndpointUnavailableError, LLMConfigurationError,
            EmbeddingAuthenticationError, EmbeddingConfigurationError, EmbeddingRateLimitExhaustedError) as e:
        # The configured remote embedding/LLM provider itself is the problem
        # (unreachable, cold, misconfigured credentials, or rate-limited) —
        # not a bug in this backend, so 503 rather than 500. Each of these
        # already carries a clear, actionable message (see their own
        # docstrings in backend.services.embeddings / backend.services.llm);
        # a full traceback would just be log noise for what is, from this
        # backend's point of view, an expected and already-classified
        # upstream failure mode.
        logger.warning(f"Query failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))

    except Exception as e:
        logger.exception("Query failed")
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )
