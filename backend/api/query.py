"""
Query API endpoints for RAG queries.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional
from markdown_it import MarkdownIt

from backend.services.rag_engine import RAGEngine
from backend.db.vector_store import VectorStore
from backend.config.settings import get_settings
from backend.dependencies import get_client_api_keys, get_vector_store, make_embedding_service, make_llm_service

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


class QueryResponse(BaseModel):
    """RAG query response."""
    question: str
    answer: str
    answer_format: str  # Format of answer: "text", "html", or "markdown"
    sources: List[SourceCitation]
    library_ids: List[str]


@router.post("/query", response_model=QueryResponse)
async def query_libraries(
    query: QueryRequest,
    http_request: Request,
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
        llm_service = make_llm_service(client_keys)

        # Use preset defaults if not specified in request
        top_k = query.top_k if query.top_k is not None else preset.rag.top_k
        min_score = query.min_score if query.min_score is not None else preset.rag.score_threshold

        if True:  # keep indentation for the block below
            # Validate that at least one library is indexed
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            indexed_count = 0
            for library_id in query.library_ids:
                count = vector_store.client.count(
                    collection_name=vector_store.CHUNKS_COLLECTION,
                    count_filter=Filter(
                        must=[
                            FieldCondition(
                                key="library_id",
                                match=MatchValue(value=library_id)
                            )
                        ]
                    )
                ).count
                if count > 0:
                    indexed_count += 1

            if indexed_count == 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"None of the specified libraries have been indexed. Please index the libraries before querying."
                )

            # Create RAG engine
            rag_engine = RAGEngine(
                embedding_service=embedding_service,
                llm_service=llm_service,
                vector_store=vector_store,
                settings=settings
            )

            # Execute query with preset defaults
            result = await rag_engine.query(
                question=query.question,
                library_ids=query.library_ids,
                top_k=top_k,
                min_score=min_score
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
                library_ids=query.library_ids
            )

    except Exception as e:
        logger.exception("Query failed")
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )
