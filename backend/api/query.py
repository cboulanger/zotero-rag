"""
Query API endpoints for RAG queries.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from backend.services.rag_engine import RAGEngine
from backend.services.embeddings import create_embedding_service
from backend.services.llm import create_llm_service
from backend.db.vector_store import VectorStore
from backend.config.settings import get_settings

router = APIRouter()


class SourceCitation(BaseModel):
    """Source citation with location information."""
    item_id: str
    title: str
    page_number: Optional[int] = None
    text_anchor: Optional[str] = None  # First 5 words of chunk
    relevance_score: float


class QueryRequest(BaseModel):
    """RAG query request."""
    question: str
    library_ids: List[str]
    top_k: int = 5  # Number of chunks to retrieve
    min_score: float = 0.5  # Minimum similarity score


class QueryResponse(BaseModel):
    """RAG query response."""
    question: str
    answer: str
    sources: List[SourceCitation]
    library_ids: List[str]


@router.post("/query", response_model=QueryResponse)
async def query_libraries(request: QueryRequest):
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
    if not request.library_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one library ID must be provided"
        )

    if not request.question.strip():
        raise HTTPException(
            status_code=400,
            detail="Question cannot be empty"
        )

    try:
        # Initialize services
        settings = get_settings()
        embedding_service = create_embedding_service(settings)
        llm_service = create_llm_service(settings)
        vector_store = VectorStore(settings)

        # Create RAG engine
        rag_engine = RAGEngine(
            embedding_service=embedding_service,
            llm_service=llm_service,
            vector_store=vector_store
        )

        # Execute query
        result = await rag_engine.query(
            question=request.question,
            library_ids=request.library_ids,
            top_k=request.top_k,
            min_score=request.min_score
        )

        # Format citations
        sources = [
            SourceCitation(
                item_id=source.item_id,
                title=source.title,
                page_number=source.page_number,
                text_anchor=source.text_anchor,
                relevance_score=source.score
            )
            for source in result.sources
        ]

        return QueryResponse(
            question=request.question,
            answer=result.answer,
            sources=sources,
            library_ids=request.library_ids
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )
