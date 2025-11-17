"""
RAG (Retrieval-Augmented Generation) query engine.

Coordinates retrieval from vector database and generation with LLM.
"""

import logging
from typing import List
from pydantic import BaseModel

from backend.services.embeddings import EmbeddingService
from backend.services.llm import LLMService
from backend.db.vector_store import VectorStore
from backend.config.settings import Settings

logger = logging.getLogger(__name__)


class SourceInfo(BaseModel):
    """Source citation information."""
    item_id: str
    library_id: str
    title: str
    page_number: int | None = None
    text_anchor: str | None = None
    score: float


class QueryResult(BaseModel):
    """RAG query result."""
    question: str
    answer: str
    sources: List[SourceInfo]


class RAGEngine:
    """
    RAG query engine for answering questions based on indexed documents.

    Combines vector similarity search with LLM generation to provide
    answers with source citations.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        llm_service: LLMService,
        vector_store: VectorStore,
        settings: Settings
    ):
        """
        Initialize RAG engine.

        Args:
            embedding_service: Service for generating query embeddings.
            llm_service: Service for text generation.
            vector_store: Vector database for retrieval.
            settings: Application settings (for accessing preset configuration).
        """
        self.embedding_service = embedding_service
        self.llm_service = llm_service
        self.vector_store = vector_store
        self.settings = settings

    async def query(
        self,
        question: str,
        library_ids: List[str],
        top_k: int = 5,
        min_score: float = 0.3  # Fallback default, should use preset value from API layer
    ) -> QueryResult:
        """
        Answer a question using RAG.

        Args:
            question: User's question.
            library_ids: List of library IDs to search.
            top_k: Number of chunks to retrieve.
            min_score: Minimum similarity score threshold (default: from preset, fallback 0.3).

        Returns:
            Query result with answer and source citations.
        """
        logger.info(f"Processing RAG query: {question}")

        # Step 1: Generate embedding for question
        logger.debug("Generating query embedding...")
        query_embedding = await self.embedding_service.embed_text(question)

        # Step 2: Search vector database for relevant chunks
        logger.debug(f"Searching for top {top_k} chunks in libraries: {library_ids}")
        search_results = self.vector_store.search(
            query_vector=query_embedding,
            limit=top_k,
            score_threshold=min_score,
            library_ids=library_ids if library_ids else None,
        )

        if not search_results:
            logger.warning("No relevant chunks found for query")
            return QueryResult(
                question=question,
                answer="I couldn't find any relevant information in the indexed documents to answer this question.",
                sources=[]
            )

        logger.info(f"Retrieved {len(search_results)} relevant chunks")

        # Step 3: Assemble context from retrieved chunks
        context_parts = []
        for i, result in enumerate(search_results, 1):
            chunk = result.chunk
            metadata = chunk.metadata
            doc_meta = metadata.document_metadata

            # Format source information
            source_info = f"[Source {i}: {doc_meta.title or 'Unknown'}"
            if metadata.page_number:
                source_info += f", p. {metadata.page_number}"
            source_info += "]"

            # Add chunk text with source
            context_parts.append(f"{source_info}\n{chunk.text}")

        context = "\n\n".join(context_parts)

        # Step 4: Generate prompt with context
        prompt = f"""
Based on the following context from academic documents, please answer the question.

Context:
{context}

Question: {question}

Provide a comprehensive answer based on the context above. Only use information from the context. If the context doesn't contain enough information to fully answer the question, acknowledge this in your response. Include references to the sources when relevant, in the Form "[X:Y]", X being the number of the source, Y the page in the source as given in the context. 
"""

        logger.debug(f"Generated prompt with {len(context)} characters of context")

        # Step 5: Get LLM completion
        # Use max_answer_tokens from preset configuration (calibrated for each model)
        preset = self.settings.get_hardware_preset()
        max_tokens = preset.llm.max_answer_tokens

        logger.debug(f"Generating answer with LLM (max_tokens={max_tokens})...")
        answer = await self.llm_service.generate(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.7
        )

        logger.info("Answer generated successfully")

        # Step 6: Extract source citations
        sources = []
        for result in search_results:
            chunk = result.chunk
            metadata = chunk.metadata
            doc_meta = metadata.document_metadata

            source = SourceInfo(
                item_id=doc_meta.item_key or "unknown",
                library_id=doc_meta.library_id,
                title=doc_meta.title or "Unknown Document",
                page_number=metadata.page_number,
                text_anchor=metadata.text_preview,
                score=result.score
            )
            sources.append(source)

        return QueryResult(
            question=question,
            answer=answer,
            sources=sources
        )
