"""
RAG (Retrieval-Augmented Generation) query engine.

Coordinates retrieval from vector database and generation with LLM.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional
from pydantic import BaseModel

from backend.models.filters import MetadataFilters
from backend.models.trace import AgentExecutionTrace, ChunkTrace, LLMCallTrace, RetrievalTrace
from backend.services.embeddings import EmbeddingService
from backend.services.llm import LLMService
from backend.db.vector_store import VectorStore
from backend.config.settings import Settings

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector

logger = logging.getLogger(__name__)


# Some models occasionally hallucinate raw tool/function-call pseudocode as
# their entire answer (e.g. `tool.call('getResearchTrends', ...)`), even
# though no `tools` parameter is ever sent in the completion request — an
# artifact of heavy agentic fine-tuning bleeding into plain-completion mode,
# more likely on questions that sound like they want structured/classified
# output. Detected below so query() can retry once rather than silently
# showing the user pseudocode instead of an answer.
_TOOL_CALL_LEAK_PATTERN = re.compile(
    r"\b\w+\.call\(|\bfunction_call\s*\(|\btool_call\s*\(|<tool_call>|<function_call>",
    re.IGNORECASE,
)


def _looks_like_tool_call_leak(text: str) -> bool:
    """True if `text` looks like leaked tool/function-call pseudocode rather
    than a natural-language answer."""
    return bool(_TOOL_CALL_LEAK_PATTERN.search(text))


# Weaker models frequently comply with the CRITICAL CITATION RULE's *format*
# but skip citations altogether — observed live: 2 of 3 repeated attempts at
# the same question produced an answer with zero [SN] markers anywhere.
# Detected below so query() can ask for a revision rather than silently
# returning claims with no attributable source.
_SN_CITATION_PATTERN = re.compile(r"\[S\d+(?::\d+)?(?:,\s*S\d+(?::\d+)?)*\]")


def _missing_citations(text: str) -> bool:
    """True if `text` contains no [SN] citation markers at all."""
    return not _SN_CITATION_PATTERN.search(text)


def _quality_issue_reinforcement(answer: str) -> Optional[str]:
    """Return a reinforcement instruction to retry generation with, if `answer`
    has a detectable quality issue — or None if it looks fine. Checked once
    per generation attempt; only the first detected issue is reported."""
    if _looks_like_tool_call_leak(answer):
        return (
            "Your previous response incorrectly attempted to call a tool or function. "
            "You have no tools available — answer directly in plain prose using only "
            "the context above."
        )
    if _missing_citations(answer):
        return (
            "Your previous response did not include any [SN] citations. Revise it to "
            "add an inline [SN] citation (see the CRITICAL CITATION RULE above) "
            "immediately after every factual claim, using the source labels from the "
            "context above."
        )
    return None


# Observed live: a fixed top_k can be entirely saturated by chunks from a single
# dominant document (e.g. one paper with 100+ indexed chunks vs. a handful for
# everything else), starving the answer of other genuinely relevant sources even
# though they exist in the library. Vector search is cheap (unlike an LLM call),
# so when the raw search hits the top_k cap — not the corpus limit — and diversity
# still looks low, retry once at a larger top_k before generating.
_DIVERSITY_FLOOR = 3            # minimum distinct documents before escalating
_DIVERSITY_ESCALATION_FACTOR = 3
_DIVERSITY_ESCALATION_MAX_TOP_K = 30

# Caps how many chunks from a single document are included in the assembled
# context. Without this, an over-chunked document (especially after the
# escalation above) can flood the prompt with many near-duplicate passages
# while other included documents get only one or two — bounds prompt size and
# keeps the context readable, independent of document diversity itself.
_MAX_CHUNKS_PER_DOCUMENT = 4


def _format_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    last_names = [a.split(",")[0].strip() if "," in a else a.split()[-1] for a in authors]
    if len(last_names) == 1:
        return last_names[0]
    if len(last_names) == 2:
        return f"{last_names[0]} & {last_names[1]}"
    return f"{last_names[0]} et al."


class SourceInfo(BaseModel):
    """Source citation information."""
    item_id: str
    library_id: str
    title: str
    authors: list[str] = []
    year: int | None = None
    page_number: int | None = None
    text_anchor: str | None = None
    score: float
    chunk_id: str | None = None   # payload chunk_id backing this citation's representative
                                   # chunk — lets a follow-up turn re-fetch the same evidence
                                   # via VectorStore.get_chunks_by_ids()


class QueryResult(BaseModel):
    """RAG query result."""
    question: str
    answer: str
    sources: List[SourceInfo]
    model_name: Optional[str] = None
    agents_used: list[str] = []
    source_refs: list[str] = []   # union of every contributing source's chunk_id


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
        min_score: float = 0.3,  # Fallback default, should use preset value from API layer
        filters: Optional[MetadataFilters] = None,
        trace: Optional[TraceCollector] = None,
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
        t_start = time.monotonic()

        # Step 1: Generate embedding for question
        logger.debug("Generating query embedding...")
        query_embedding = await self.embedding_service.embed_text(question)
        embedding_model = getattr(self.embedding_service, "model_name", "unknown")

        # Step 2: Search vector database for relevant chunks
        logger.debug(f"Searching for top {top_k} chunks in libraries: {library_ids}")
        active_filters = filters if filters and not filters.is_empty() else None
        search_results = await asyncio.to_thread(
            self.vector_store.search,
            query_vector=query_embedding,
            limit=top_k,
            score_threshold=min_score,
            library_ids=library_ids if library_ids else None,
            filters=active_filters,
        )

        if not search_results:
            logger.warning("No relevant chunks found for query")
            return QueryResult(
                question=question,
                answer="I couldn't find any relevant information in the indexed documents to answer this question.",
                sources=[]
            )

        logger.info(f"Retrieved {len(search_results)} relevant chunks")

        # Escalate once if the search hit the top_k cap (not the corpus limit) and
        # came back dominated by too few distinct documents.
        escalated = False
        if len(search_results) == top_k and top_k < _DIVERSITY_ESCALATION_MAX_TOP_K:
            unique_doc_count = len({
                r.chunk.metadata.document_metadata.attachment_key
                or r.chunk.metadata.document_metadata.item_key
                for r in search_results
            })
            if unique_doc_count < _DIVERSITY_FLOOR:
                escalated_top_k = min(top_k * _DIVERSITY_ESCALATION_FACTOR, _DIVERSITY_ESCALATION_MAX_TOP_K)
                logger.info(
                    f"Retrieval diversity low ({unique_doc_count} documents from top_k={top_k}); "
                    f"escalating to top_k={escalated_top_k}"
                )
                escalated_results = await asyncio.to_thread(
                    self.vector_store.search,
                    query_vector=query_embedding,
                    limit=escalated_top_k,
                    score_threshold=min_score,
                    library_ids=library_ids if library_ids else None,
                    filters=active_filters,
                )
                if len(escalated_results) > len(search_results):
                    search_results = escalated_results
                    escalated = True
                    logger.info(f"Escalated retrieval returned {len(search_results)} chunks")

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
            if len(results_for_doc) > _MAX_CHUNKS_PER_DOCUMENT:
                results_for_doc = sorted(results_for_doc, key=lambda r: r.score, reverse=True)[:_MAX_CHUNKS_PER_DOCUMENT]
            # Sort chunks within document by page number, then chunk index
            results_for_doc.sort(key=lambda r: (
                r.chunk.metadata.page_number or 0,
                r.chunk.metadata.chunk_index or 0,
            ))
            best_result = max(results_for_doc, key=lambda r: r.score)
            doc_representatives.append(best_result)

            doc_meta = results_for_doc[0].chunk.metadata.document_metadata
            authors_str = _format_authors(doc_meta.authors or [])
            year_str = f" ({doc_meta.year})" if doc_meta.year else ""
            attribution = f"{authors_str}{year_str} — " if authors_str or year_str else ""
            header = f"[S{i}: {attribution}{doc_meta.title or 'Unknown'}]"
            passages = []
            for result in results_for_doc:
                metadata = result.chunk.metadata
                page_label = f"[p. {metadata.page_number}] " if metadata.page_number else ""
                passages.append(f"{page_label}{result.chunk.text}")
            context_parts.append(f"{header}\n" + "\n\n".join(passages))

        context = "\n\n".join(context_parts)

        # Record retrieval trace before calling the LLM
        if trace is not None:
            scores = [r.score for r in search_results]
            chunk_traces = [
                ChunkTrace(
                    item_key=r.chunk.metadata.document_metadata.item_key or "",
                    attachment_key=r.chunk.metadata.document_metadata.attachment_key,
                    title=r.chunk.metadata.document_metadata.title or "",
                    authors=r.chunk.metadata.document_metadata.authors or [],
                    year=r.chunk.metadata.document_metadata.year,
                    page_number=r.chunk.metadata.page_number,
                    score=r.score,
                    text_preview=r.chunk.metadata.text_preview,
                )
                for r in search_results
            ]
            retrieval_trace = RetrievalTrace(
                embedding_model=embedding_model,
                embedding_dims=len(query_embedding),
                search_params={
                    "top_k": top_k,
                    "min_score": min_score,
                    "library_ids": library_ids,
                    "filters": active_filters.model_dump() if active_filters else None,
                },
                escalated=escalated,
                raw_results_count=len(search_results),
                score_stats={
                    "min": min(scores),
                    "max": max(scores),
                    "avg": sum(scores) / len(scores),
                },
                documents_grouped=len(sorted_doc_keys),
                chunks=chunk_traces,
            )

        # Step 4: Generate prompt with context
        prompt = f"""
Based on the following context from academic documents, please answer the question.

Context:
{context}

Question: {question}

Provide a comprehensive answer based on the context above. Only use information from the context. If the context doesn't contain enough information to fully answer the question, state clearly what is missing and stop there — do not supplement your answer with general knowledge, guesses, or suggestions that are not grounded in and cited from the context above.

Answer directly. Do not narrate your process or describe what you are about to do (e.g. do not write "I will look through the context" or "Here are some relevant sources:") — begin with the substantive answer itself.

You have no tools, functions, or external APIs available. Respond only with plain natural-language prose that directly answers the question — never emit tool-call or function-call syntax.

CRITICAL CITATION RULE: The sources above are labelled [S1], [S2], [S3] etc. You MUST cite them using ONLY that notation. Every sentence that states a specific fact, feature, or claim drawn from the sources MUST end with an inline citation in that notation — if you cannot attribute a claim to a specific source, do not state it. The ONLY acceptable citation formats are:
  - [SN]        — reference to source N (e.g. [S1], [S3])
  - [SN:P]      — source N, page P — P is a plain integer, e.g. [S2:7] NOT [S2:p.7]
  - [SN,SM]     — multiple sources (e.g. [S1,S2,S3])
  - [SN:P,SM:Q] — multiple sources with pages (e.g. [S1:10,S2:20])

IMPORTANT: Page numbers are integers only. Write [S1:3] not [S1:p.3].
NEVER use plain numbers like [1] or [4] — those are bibliography references inside the documents, not source labels.
NEVER write "Source 1", "S1", or any form other than the bracket notation above.

PAGE SELECTION RULE: When citing a specific page, only cite pages that contain substantive content (arguments, analysis, findings). Do NOT cite pages that consist primarily of bibliographies, reference lists, or footnote-only content — use a different page from the same source instead, or omit the page number.
"""

        logger.debug(f"Generated prompt with {len(context)} characters of context")

        # Step 5: Get LLM completion
        # Use max_answer_tokens from preset configuration (calibrated for each model)
        preset = self.settings.get_hardware_preset()
        max_tokens = preset.llm.max_answer_tokens

        logger.debug(f"Generating answer with LLM (max_tokens={max_tokens})...")
        t_llm = time.monotonic()
        final_prompt = prompt
        answer = await self.llm_service.generate(
            prompt=final_prompt,
            max_tokens=max_tokens,
            temperature=0.7
        )

        reinforcement = _quality_issue_reinforcement(answer)
        if reinforcement:
            logger.warning(
                f"LLM answer had a quality issue; retrying once. {reinforcement} "
                f"Original answer: {answer[:200]!r}"
            )
            final_prompt = prompt + f"\n\nIMPORTANT: {reinforcement}"
            answer = await self.llm_service.generate(
                prompt=final_prompt,
                max_tokens=max_tokens,
                temperature=0.7
            )
            if _quality_issue_reinforcement(answer):
                logger.warning(
                    f"Retry still had a quality issue; using it anyway: {answer[:200]!r}"
                )

        llm_duration_ms = int((time.monotonic() - t_llm) * 1000)

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
                authors=doc_meta.authors or [],
                year=doc_meta.year,
                # Don't set page_number here: the representative chunk for a deduplicated
                # document may come from any page (e.g. a bibliography section).  Inline
                # citations get the correct page via the LLM's explicit [N:P] notation
                # (passed as pageOverride in buildZoteroPDFURI); bibliography links open
                # the PDF at its natural start when page is absent.
                page_number=None,
                text_anchor=metadata.text_preview,
                score=result.score,
                chunk_id=metadata.chunk_id,
            )
            sources.append(source)

        if trace is not None:
            trace.record(AgentExecutionTrace(
                agent_name="rag",
                retrieval=retrieval_trace,
                catalog_results=None,
                context_text=context,
                sources_count=len(sources),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            ))
            trace.record(LLMCallTrace(
                call_type="rag_generation",
                model=self.llm_service.model_name,
                prompt=final_prompt,
                response=answer,
                temperature=0.7,
                max_tokens=max_tokens,
                duration_ms=llm_duration_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

        return QueryResult(
            question=question,
            answer=answer,
            sources=sources
        )
