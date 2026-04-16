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

        # Group chunks by document (attachment_key), preserving all relevant passages.
        # This gives the LLM real content (not just the highest-scoring chunk, which is
        # often a bibliography/reference section) while still assigning one source number
        # per document so citations are not repetitively labelled [1], [2], [3] for the
        # same paper.
        doc_chunks: dict[str, list] = {}
        doc_best_score: dict[str, float] = {}
        for result in search_results:
            key = (
                result.chunk.metadata.document_metadata.attachment_key
                or result.chunk.metadata.document_metadata.item_key
            )
            if key not in doc_chunks:
                doc_chunks[key] = []
                doc_best_score[key] = result.score
            doc_chunks[key].append(result)
            if result.score > doc_best_score[key]:
                doc_best_score[key] = result.score

        # Sort documents by their best chunk score (most relevant document first)
        sorted_doc_keys = sorted(doc_chunks.keys(), key=lambda k: doc_best_score[k], reverse=True)
        logger.info(f"Grouped into {len(sorted_doc_keys)} unique documents for context")

        # Step 3: Assemble context — one numbered source per document, all its chunks listed
        context_parts = []
        doc_representatives: list = []  # best-scoring chunk per doc for SourceInfo
        for i, doc_key in enumerate(sorted_doc_keys, 1):
            results_for_doc = doc_chunks[doc_key]
            # Sort chunks within document by page number, then chunk index
            results_for_doc.sort(key=lambda r: (
                r.chunk.metadata.page_number or 0,
                r.chunk.metadata.chunk_index or 0,
            ))
            best_result = max(results_for_doc, key=lambda r: r.score)
            doc_representatives.append(best_result)

            doc_meta = results_for_doc[0].chunk.metadata.document_metadata
            header = f"[Source {i}: {doc_meta.title or 'Unknown'}]"
            passages = []
            for result in results_for_doc:
                metadata = result.chunk.metadata
                page_label = f"[p. {metadata.page_number}] " if metadata.page_number else ""
                passages.append(f"{page_label}{result.chunk.text}")
            context_parts.append(f"{header}\n" + "\n\n".join(passages))

        context = "\n\n".join(context_parts)

        # Step 4: Generate prompt with context
        prompt = f"""
Based on the following context from academic documents, please answer the question.

Context:
{context}

Question: {question}

Provide a comprehensive answer based on the context above. Only use information from the context. If the context doesn't contain enough information to fully answer the question, acknowledge this in your response.

CRITICAL CITATION RULE: You MUST cite sources using ONLY bracket notation. The ONLY acceptable citation formats are:
  - [N]        — reference to source N (e.g. [1], [3])
  - [N:P]      — source N, page P (e.g. [2:7])
  - [N,M]      — multiple sources (e.g. [1,2,3])
  - [N:P,M:Q]  — multiple sources with pages (e.g. [1:10,2:20])

NEVER write "Source 1", "Source 2", "*Source 3*", "(Source 4)", or any other textual form.
Every reference to a source must use bracket notation such as [1] or [2:15].

PAGE SELECTION RULE: When citing a specific page, only cite pages that contain substantive content (arguments, analysis, findings). Do NOT cite pages that consist primarily of bibliographies, reference lists, or footnote-only content — use a different page from the same source instead, or omit the page number.
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

        # Step 6: Extract source citations (one per unique document, matching LLM context order)
        sources = []
        for result in doc_representatives:
            chunk = result.chunk
            metadata = chunk.metadata
            doc_meta = metadata.document_metadata

            source = SourceInfo(
                item_id=doc_meta.item_key or "unknown",
                library_id=doc_meta.library_id,
                title=doc_meta.title or "Unknown Document",
                # Don't set page_number here: the representative chunk for a deduplicated
                # document may come from any page (e.g. a bibliography section).  Inline
                # citations get the correct page via the LLM's explicit [N:P] notation
                # (passed as pageOverride in buildZoteroPDFURI); bibliography links open
                # the PDF at its natural start when page is absent.
                page_number=None,
                text_anchor=metadata.text_preview,
                score=result.score
            )
            sources.append(source)

        return QueryResult(
            question=question,
            answer=answer,
            sources=sources
        )
