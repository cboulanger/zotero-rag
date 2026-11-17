"""Embedding-based near-duplicate removal over a vector-search candidate pool.

``rag_engine._dedup_chunks_by_text`` only removes chunks whose text is identical
after whitespace/case normalization. Chunks that say nearly the same thing in
slightly different words (overlapping sibling chunks of one passage, the same
paragraph extracted twice with different line breaks, a duplicate library item
extracted differently) still each take a ``top_k`` slot. This drops every
candidate whose embedding is at least ``threshold`` cosine-similar to a
higher-scoring candidate already kept, before the per-document cap and the
``top_k`` cutoff, so the freed slots go to the next distinct passages.

Unlike ``embedding_mmr_rerank`` it does not reorder anything: kept candidates
keep their vector scores, so the result only differs from plain ranking where
real near-copies were removed.

Needs each result's ``chunk.embedding`` populated
(``VectorStore.search(..., with_vectors=True)``); returns ``results`` unchanged
otherwise.
"""

import math

from backend.models.document import SearchResult


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def drop_near_duplicates(results: list[SearchResult], threshold: float) -> list[SearchResult]:
    """Return ``results`` sorted by score, without candidates near-identical to a better one.

    A candidate is dropped when its cosine similarity to any already-kept,
    higher-scoring candidate is >= ``threshold``. ``threshold <= 0``, fewer
    than two results, or any missing embedding returns ``results`` unchanged.
    """
    if threshold <= 0 or len(results) < 2:
        return results
    if any(r.chunk.embedding is None for r in results):
        return results
    kept: list[SearchResult] = []
    kept_vectors: list[list[float]] = []
    for result in sorted(results, key=lambda r: r.score, reverse=True):
        vector = _unit(result.chunk.embedding)
        if any(sum(a * b for a, b in zip(vector, other)) >= threshold for other in kept_vectors):
            continue
        kept.append(result)
        kept_vectors.append(vector)
    return kept
