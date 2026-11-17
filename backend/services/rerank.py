"""Optional re-ranking of the vector-search candidate pool with a cross-encoder.

A preset's ``rerank`` section (``backend.config.presets.RerankConfig``) turns this
step on. ``rag_engine`` calls :func:`rerank_pool` on the raw candidate pool, after
the optional lexical blend and before embedding MMR, dedup and the per-document
cap, so MMR and the cap work on the sharper cross-encoder score.

The step never blocks or breaks a query: when the preset has no re-ranker, the
user switched it off, a remote endpoint is not ready, the model cannot load, the
call fails or it runs past ``timeout_seconds``, the pool keeps its vector order and
the returned :class:`RerankOutcome` says why (it goes into the query trace).

Scorers implement :class:`Reranker`:

- a local scorer from a backend plugin's ``inference.local`` capability for the
  ``rerank`` side (the shipped ``local_torch`` plugin runs a sentence-transformers
  ``CrossEncoder`` in the backend process; needs the ``local-models`` extra).
- :class:`RemoteReranker` POSTs to ``{base_url}/rerank`` in the Jina/Cohere format
  that vLLM, Hugging Face TEI and Infinity serve. Its credentials come from the
  side's provider for the calling user (see :func:`set_rerank_request`).

Per-request state (whether this query may re-rank, the caller's identity and keys)
travels in a context variable set by the query API, so the orchestrator, agents and
engine need no extra parameters.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel

logger = logging.getLogger(__name__)

RerankStatus = Literal["ran", "off", "disabled", "not_ready", "timeout", "error"]


class RerankOutcome(BaseModel):
    """What the re-ranking step did for one query (recorded in the trace).

    ``off``: the preset has no re-ranker. ``disabled``: the user switched it off
    (Preferences or the query dialog). ``not_ready``: a remote endpoint is not
    running or the caller has no credentials for it. ``timeout``/``error``: the
    scorer failed; the vector order was kept.
    """

    status: RerankStatus
    model: Optional[str] = None
    provider: Optional[str] = None
    pool_size: int = 0
    scored: int = 0
    duration_ms: int = 0
    detail: str = ""


@dataclass
class RerankRequest:
    """Per-request re-ranking context, set by the query API."""

    enabled: bool = True
    #: Caller's pool size (``rerank_pool_size`` of the query); None uses the preset's ``pool_size``.
    pool_size: Optional[int] = None
    #: Personal keys sent by the client, by env-var name (as ``get_client_api_keys`` returns them).
    client_keys: dict[str, str] = field(default_factory=dict)
    #: Zotero user id of the caller (None for loopback/public callers).
    user_id: Optional[int] = None


_request: ContextVar[RerankRequest] = ContextVar("rerank_request", default=RerankRequest())


def set_rerank_request(request: RerankRequest):
    """Set the re-ranking context for the current request; returns a token for :func:`reset_rerank_request`."""
    return _request.set(request)


def reset_rerank_request(token) -> None:
    _request.reset(token)


def current_rerank_request() -> RerankRequest:
    return _request.get()


#: Upper bound of any pool, the same as ``RerankConfig.pool_size``'s.
MAX_POOL_SIZE = 500


def scaled_pool_size(cfg: Any, total_chunks: int) -> int:
    """The preset's ``pool_size`` scaled to the size of the searched libraries.

    In a large index the vector scores of the top candidates lie close together, so a
    relevant passage can rank far down the first, vector-only search and needs a larger
    pool to reach the re-ranker. In a small one a larger pool mostly adds generic
    passages. Up to ``pool_reference_chunks`` chunks the preset's value applies; above
    it the pool grows logarithmically, up to :data:`MAX_POOL_SIZE`.
    """
    base = cfg.pool_size
    reference = getattr(cfg, "pool_reference_chunks", None)
    if not reference or total_chunks <= reference:
        return base
    return max(base, min(MAX_POOL_SIZE, round(base * (1 + math.log10(total_chunks / reference)))))


def effective_pool_size(cfg: Any, total_chunks: int = 0) -> int:
    """Candidates to rescore for this request: the caller's override, else the scaled preset value."""
    override = current_rerank_request().pool_size
    return override if override is not None else scaled_pool_size(cfg, total_chunks)


class RerankerUnavailable(RuntimeError):
    """The scorer cannot run for this caller right now (endpoint not ready, no credentials)."""


class Reranker(ABC):
    """Scores (question, passage) pairs; higher is more relevant, in [0, 1]."""

    model_name: str
    provider_id: Optional[str] = None

    @abstractmethod
    async def score(self, question: str, texts: list[str]) -> list[float]:
        """One score per text, in input order."""


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


# -- remote /rerank endpoint ----------------------------------------------------


class RemoteReranker(Reranker):
    """A ``/rerank`` endpoint (Jina/Cohere request format) on the side's provider."""

    def __init__(self, provider: Any, model_name: str, timeout_seconds: float):
        self.provider = provider
        self.provider_id = getattr(provider, "id", None)
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

    def _credentials(self, refresh: bool = False):
        """Base URL and key for the calling user; raises RerankerUnavailable when there are none."""
        hook = getattr(self.provider, "rerank_credentials", None)
        if hook is None:
            raise RerankerUnavailable(f"provider {self.provider_id!r} cannot serve a re-ranker")
        creds = hook(current_rerank_request(), refresh=refresh)
        if creds is None or not creds.base_url:
            raise RerankerUnavailable("the re-ranking endpoint is not running, or no key for it is set")
        return creds

    async def _post(self, creds, question: str, texts: list[str]):
        import httpx

        headers = {"Content-Type": "application/json"}
        if creds.api_key:
            headers["Authorization"] = f"Bearer {creds.api_key}"
        body = {"model": self.model_name, "query": question, "documents": texts, "top_n": len(texts)}
        # A provider may route the call differently (RunPod: through /runsync).
        route = getattr(self.provider, "rerank_http", None)
        url, payload = route(creds.base_url, body) if route else (creds.base_url.rstrip("/") + "/rerank", body)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            return await client.post(url, json=payload, headers=headers)

    async def score(self, question: str, texts: list[str]) -> list[float]:
        import httpx

        creds = await asyncio.to_thread(self._credentials)
        try:
            resp = await self._post(creds, question, texts)
            retry = resp.status_code in (401, 503)
        except httpx.TransportError:
            retry, resp = True, None
        if retry:
            creds = await asyncio.to_thread(self._credentials, True)
            resp = await self._post(creds, question, texts)
        if resp.status_code >= 400:
            # A paused or cold endpoint is "not running" (the query goes on without
            # re-ranking, and the dialog offers to start it), not a failure.
            classify = getattr(self.provider, "classify_http_error", None)
            state = classify(resp.status_code, resp.text) if classify is not None else None
            if state in ("paused", "cold"):
                raise RerankerUnavailable(f"the re-ranking endpoint is {state}")
        resp.raise_for_status()
        touch = getattr(self.provider, "rerank_activity", None)
        if touch is not None:
            try:
                await asyncio.to_thread(touch, current_rerank_request())
            except Exception as exc:  # activity reporting must never fail a query
                logger.debug(f"Re-ranker activity report failed: {exc}")
        data = resp.json()
        unwrap = getattr(self.provider, "rerank_unwrap", None)
        if unwrap is not None:
            data = unwrap(data)
        return parse_rerank_response(data, len(texts))


def parse_rerank_response(data: Any, n: int) -> list[float]:
    """Scores in input order from a Jina/Cohere ``/rerank`` response.

    Accepts ``{"results": [{"index", "relevance_score"}]}`` (vLLM, Jina, Cohere,
    Infinity) and TEI's bare list ``[{"index", "score"}]``.
    """
    items = data.get("results", data.get("data")) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("unexpected /rerank response")
    scores = [0.0] * n
    seen = 0
    for item in items:
        idx = item.get("index")
        value = item.get("relevance_score", item.get("score"))
        if not isinstance(idx, int) or not 0 <= idx < n or value is None:
            continue
        value = float(value)
        scores[idx] = value if 0.0 <= value <= 1.0 else _sigmoid(value)
        seen += 1
    if seen != n:
        raise ValueError(f"/rerank returned {seen} scores for {n} documents")
    return scores


def _chunk_id(result: Any) -> Optional[str]:
    try:
        return result.chunk.metadata.chunk_id or None
    except AttributeError:
        return None


def rerank_client_fields(config: Any) -> list[dict]:
    """Personal key fields of a remote ``rerank`` side, in the shape of
    ``RemoteEmbeddingService.required_client_fields``.

    A remote re-ranker is always per user (scope ``user``), so only the personal
    ``api_key_env`` field exists. It is optional: a query without it simply runs
    without re-ranking, so it is never counted as a missing key.
    """
    if config is None or config.model_type != "remote":
        return []
    env_var = config.model_kwargs.get("api_key_env")
    if not env_var:
        return []
    from backend.services.embeddings import docs_url_for, env_var_to_header

    return [{
        "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "api_key",
        "description": f"API key for re-ranking ({config.model_name})",
        "docs_url": docs_url_for(config.model_kwargs, env_var), "required_for": ["reranking"],
        "pattern": config.model_kwargs.get("api_key_pattern"),
    }]


# -- factory and the pipeline step ---------------------------------------------


def build_reranker(preset: Any) -> Optional[Reranker]:
    """The scorer for ``preset``'s ``rerank`` section, or None when it has none."""
    cfg = getattr(preset, "rerank", None)
    if cfg is None:
        return None
    if cfg.model_type == "local":
        from backend.pluginhost import LocalEngineUnavailable, select_local_engine

        try:
            engine = select_local_engine("rerank", cfg.engine)
        except LocalEngineUnavailable as exc:
            raise RerankerUnavailable(str(exc)) from exc
        return engine.create(cfg)
    from backend.providers import get_provider

    return RemoteReranker(get_provider(preset, "rerank"), cfg.model_name, cfg.timeout_seconds)


async def _max_over_questions(scorer: Reranker, questions: list[str], texts: list[str]) -> list[float]:
    """Per passage, the highest score over all question versions."""
    best: list[float] = []
    for question in questions:
        scores = await scorer.score(question, texts)
        if len(scores) != len(texts):
            raise ValueError(f"re-ranker returned {len(scores)} scores for {len(texts)} passages")
        best = list(scores) if not best else [max(a, b) for a, b in zip(best, scores)]
    return best


async def rerank_pool(
    question: str,
    results: list,
    preset: Any,
    reranker: Optional[Reranker] = None,
    cache: Optional[dict[str, float]] = None,
    pool_size: Optional[int] = None,
    extra_questions: Sequence[str] = (),
) -> tuple[list, RerankOutcome]:
    """Rescore the first ``pool_size`` of ``results`` and sort them by the new score.

    ``pool_size`` is the size the caller resolved for this query (see
    :func:`effective_pool_size`); without it the request override or the preset's value applies.

    Each rescored ``SearchResult`` gets ``score`` = cross-encoder score (in [0, 1])
    and keeps its vector score in ``vector_score``; ``pre_rerank_rank`` is its
    1-based position before. Candidates beyond ``pool_size`` follow, in their old
    order, with their scores scaled below the lowest rescored one so every later
    score-based step (MMR cap, proximity escalation) stays consistent. On any
    failure the input is returned unchanged.

    ``extra_questions`` are other versions of the question (an English translation,
    see ``backend.services.query_translation``): each passage is scored against every
    version and keeps the highest score, so a passage in another language than the
    question is judged against the version in its own language.

    ``cache`` maps chunk ids to scores already computed for this question, so a
    retrieval escalation within the same query only scores the new candidates;
    it is filled in place.
    """
    cfg = getattr(preset, "rerank", None)
    if cfg is None:
        return results, RerankOutcome(status="off")
    if pool_size is None:
        pool_size = effective_pool_size(cfg)
    base = {"model": cfg.model_name, "pool_size": pool_size}
    if not current_rerank_request().enabled:
        return results, RerankOutcome(status="disabled", **base)
    if not results:
        return results, RerankOutcome(status="ran", **base)
    started = time.monotonic()
    try:
        scorer = reranker or build_reranker(preset)
        base["provider"] = getattr(scorer, "provider_id", None)
        head, tail = results[:pool_size], results[pool_size:]
        known = cache if cache is not None else {}
        ids = [_chunk_id(r) for r in head]
        todo = [i for i, cid in enumerate(ids) if cid is None or cid not in known]
        fresh: list[float] = []
        if todo:
            fresh = await asyncio.wait_for(
                _max_over_questions(scorer, [question, *extra_questions], [head[i].chunk.text for i in todo]),
                timeout=cfg.timeout_seconds,
            )
        new_scores = dict(zip(todo, fresh))
        scores = [new_scores[i] if i in new_scores else known[ids[i]] for i in range(len(head))]
        for i, value in new_scores.items():
            if ids[i] is not None:
                known[ids[i]] = value
    except RerankerUnavailable as exc:
        return results, RerankOutcome(status="not_ready", detail=str(exc), **base)
    except asyncio.TimeoutError:
        logger.warning(f"Re-ranking timed out after {cfg.timeout_seconds}s; keeping the vector order")
        return results, RerankOutcome(
            status="timeout", duration_ms=int((time.monotonic() - started) * 1000),
            detail=f"no answer within {cfg.timeout_seconds:g}s", **base,
        )
    except Exception as exc:
        logger.warning(f"Re-ranking failed ({type(exc).__name__}: {exc}); keeping the vector order")
        return results, RerankOutcome(
            status="error", duration_ms=int((time.monotonic() - started) * 1000),
            detail=f"{type(exc).__name__}: {exc}"[:300], **base,
        )

    rescored = []
    for rank, (result, score) in enumerate(zip(head, scores), start=1):
        rescored.append(result.model_copy(update={
            "score": score, "vector_score": result.score, "pre_rerank_rank": rank,
        }))
    rescored.sort(key=lambda r: r.score, reverse=True)
    if tail:
        floor = min((r.score for r in rescored), default=0.0)
        top = max((r.score for r in tail), default=0.0)
        for offset, result in enumerate(tail, start=len(head) + 1):
            rescored.append(result.model_copy(update={
                "score": floor * 0.999 * (result.score / top) if top > 0 else 0.0,
                "vector_score": result.score, "pre_rerank_rank": offset,
            }))
    outcome = RerankOutcome(
        status="ran", scored=len(todo), duration_ms=int((time.monotonic() - started) * 1000), **base
    )
    logger.info(f"Re-ranked {len(head)} candidates ({len(todo)} scored now) with {cfg.model_name} in {outcome.duration_ms} ms")
    return rescored, outcome
