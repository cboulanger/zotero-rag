"""
Query API endpoints for RAG queries.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from markdown_it import MarkdownIt

from backend.services.rag_engine import RAGEngine
from backend.services.embeddings import create_embedding_service
from backend.services.llm import create_llm_service
from backend.db.vector_store import VectorStore
from backend.config.settings import get_settings

router = APIRouter()


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
        preset = settings.get_hardware_preset()

        embedding_service = create_embedding_service(
            preset.embedding,
            cache_dir=str(settings.model_weights_path)
        )
        llm_service = create_llm_service(settings)

        # Use preset defaults if not specified in request
        top_k = request.top_k if request.top_k is not None else preset.rag.top_k
        min_score = request.min_score if request.min_score is not None else preset.rag.score_threshold

        # Use context manager to ensure VectorStore is closed after query
        with VectorStore(
            storage_path=settings.vector_db_path,
            embedding_dim=embedding_service.get_embedding_dim()
        ) as vector_store:

            # Validate that at least one library is indexed
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            indexed_count = 0
            for library_id in request.library_ids:
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
                question=request.question,
                library_ids=request.library_ids,
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
                question=request.question,
                answer=answer_html,
                answer_format="html",
                sources=sources,
                library_ids=request.library_ids
            )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )
