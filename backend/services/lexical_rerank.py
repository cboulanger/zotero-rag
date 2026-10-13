"""Lexical (BM25) fusion over a vector-search candidate pool.

Vector similarity under-ranks short, term-dense chunks (numeric results, proper
nouns) that mention the query's literal terms but are not generically similar to
it (see #89, #90). This module re-scores the raw candidate pool by blending the
vector score with a BM25 score computed over the pool itself, so such chunks can
climb into the final ``top_k`` window. No index or extra dependency is needed:
document frequencies come from the pool being re-ranked.

The blend is ``(1 - weight) * norm(vector) + weight * norm(bm25)`` with min-max
normalisation over the pool; ``weight == 0`` is a no-op (the default).
"""

import math
import re
from collections import Counter

from backend.models.document import SearchResult

_K1 = 1.5
_B = 0.75
# Keeps decimals and thousands groups ("52.6", "1,000") as one token so numeric
# facts match literally; unicode-aware for non-English libraries.
_TOKEN_RE = re.compile(r"[^\W_]+(?:[.,][^\W_]+)*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase word/number tokens of ``text``."""
    return _TOKEN_RE.findall(text.lower())


def bm25_scores(query: str, texts: list[str]) -> list[float]:
    """BM25 score of each text for ``query``, with IDF taken from ``texts`` itself."""
    docs = [Counter(tokenize(t)) for t in texts]
    q_terms = set(tokenize(query))
    n = len(docs)
    if not n or not q_terms:
        return [0.0] * n
    lengths = [sum(d.values()) for d in docs]
    avg_len = (sum(lengths) / n) or 1.0
    scores = []
    for d, length in zip(docs, lengths):
        s = 0.0
        for term in q_terms:
            tf = d.get(term, 0)
            if not tf:
                continue
            df = sum(1 for other in docs if term in other)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            s += idf * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * length / avg_len))
        scores.append(s)
    return scores


def _minmax(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def fuse_lexical(query: str, results: list[SearchResult], weight: float) -> list[SearchResult]:
    """Re-score ``results`` by blending vector score and BM25; return them sorted by the blend.

    The returned results carry the blended score (in [0, 1]) so every downstream
    step that sorts or cuts by score sees one consistent ordering. ``weight <= 0``
    or fewer than two results returns ``results`` unchanged.
    """
    if weight <= 0 or len(results) < 2:
        return results
    weight = min(weight, 1.0)
    vec = _minmax([r.score for r in results])
    lex = _minmax(bm25_scores(query, [r.chunk.text for r in results]))
    fused = [
        r.model_copy(update={"score": (1 - weight) * v + weight * l})
        for r, v, l in zip(results, vec, lex)
    ]
    fused.sort(key=lambda r: r.score, reverse=True)
    return fused
