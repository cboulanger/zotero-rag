"""Global embedding-vector MMR reranking over a vector-search candidate pool.

Prototype for #90's "global ranking dilution" case: several chunks from
*different*, topically-adjacent-but-not-actually-relevant documents can
together outscore a genuinely relevant chunk by pure cosine similarity to the
query, even though none of them individually dominates (so the per-document
cap in rag_engine.py can't help — the competition is pool-wide, not
within one document). This re-ranks the whole candidate pool by real MMR
(``lambda * relevance - (1 - lambda) * max_similarity_to_already_selected``)
over actual embedding vectors, trading a little top-line relevance for
distinctness from chunks already selected — unlike
rag_engine.py's ``_select_diverse_chunks``, which only runs per-document and
uses a token-overlap similarity proxy, not real embeddings.

Needs each result's ``chunk.embedding`` populated
(``VectorStore.search(..., with_vectors=True)``) — returns ``results``
unchanged otherwise, so enabling the setting without updating the search call
is a safe no-op rather than a silent wrong answer.
"""

import math

from backend.models.document import SearchResult


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def rerank_embedding_mmr(results: list[SearchResult], lambda_param: float) -> list[SearchResult]:
    """Re-rank ``results`` by embedding-vector MMR; return them sorted by the new order.

    The returned results carry a synthetic descending score (rank-based, not
    comparable across calls) so every downstream step that sorts or cuts by
    score — the per-document cap, the top_k cutoff — sees one consistent
    ordering, mirroring how ``lexical_rerank.fuse_lexical`` overwrites scores.
    ``lambda_param <= 0``, fewer than two results, or any missing embedding
    returns ``results`` unchanged.
    """
    if lambda_param <= 0 or len(results) < 2:
        return results
    if any(r.chunk.embedding is None for r in results):
        return results

    pool = sorted(results, key=lambda r: r.score, reverse=True)
    selected = [0]
    remaining = set(range(1, len(pool)))
    while remaining:
        best_idx, best_value = None, float("-inf")
        for idx in remaining:
            max_sim = max(
                cosine_similarity(pool[idx].chunk.embedding, pool[s].chunk.embedding)
                for s in selected
            )
            value = lambda_param * pool[idx].score - (1 - lambda_param) * max_sim
            if value > best_value:
                best_value, best_idx = value, idx
        selected.append(best_idx)
        remaining.discard(best_idx)

    ordered = [pool[i] for i in selected]
    n = len(ordered)
    return [
        r.model_copy(update={"score": (n - rank) / n})
        for rank, r in enumerate(ordered)
    ]
