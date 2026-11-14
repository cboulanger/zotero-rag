"""Optional re-ranking of the vector-search candidate pool with a cross-encoder.

A preset's ``rerank`` section (``backend.config.presets.RerankConfig``) turns this
step on. ``rag_engine`` calls :func:`rerank_pool` on the raw candidate pool, after
the optional lexical blend and before embedding MMR, dedup and the per-document
cap, so MMR and the cap work on the sharper cross-encoder score.

The step never blocks or breaks a query: when the preset has no re-ranker, the
user switched it off, a remote endpoint is not ready, the model cannot load, the
call fails or it runs past ``timeout_seconds``, the pool keeps its vector order and
the returned :class:`RerankOutcome` says why (it goes into the query trace).

Two scorers implement :class:`Reranker`:

- :class:`LocalCrossEncoderReranker` runs a sentence-transformers ``CrossEncoder``
  in the backend process (loaded once per process and model, scored in a worker
  thread). Needs the ``local-models`` extra.
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
import threading
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

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


# -- local cross-encoder -------------------------------------------------------

_model_cache: dict[tuple, Any] = {}
_model_lock = threading.Lock()


def _load_cross_encoder(model_name: str, max_length: int, kwargs: dict) -> Any:
    """Load (once per process) a sentence-transformers CrossEncoder."""
    key = (model_name, max_length, tuple(sorted((k, repr(v)) for k, v in kwargs.items())))
    with _model_lock:
        model = _model_cache.get(key)
        if model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise RerankerUnavailable(
                    "sentence-transformers is not installed (install the 'local-models' extra)"
                ) from exc
            started = time.monotonic()
            model = CrossEncoder(model_name, max_length=max_length, **kwargs)
            logger.info(f"Loaded re-ranker {model_name} in {time.monotonic() - started:.1f}s")
            _model_cache[key] = model
        return model


class LocalCrossEncoderReranker(Reranker):
    """A cross-encoder in the backend process; scores in a worker thread."""

    provider_id = None

    def __init__(self, model_name: str, max_length: int = 512, batch_size: int = 16, model_kwargs: Optional[dict] = None):
        self.model_name = model_name
        self.max_length = max_length
        self.batch_size = batch_size
        self.model_kwargs = dict(model_kwargs or {})

    def _score_sync(self, question: str, texts: list[str]) -> list[float]:
        model = _load_cross_encoder(self.model_name, self.max_length, self.model_kwargs)
        raw = model.predict(
            [(question, t) for t in texts],
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        values = [float(v) for v in list(raw)]
        # Models differ in whether predict() applies a sigmoid; map logits into [0, 1].
        if any(v < 0.0 or v > 1.0 for v in values):
            values = [_sigmoid(v) for v in values]
        return values

    async def score(self, question: str, texts: list[str]) -> list[float]:
        return await asyncio.to_thread(self._score_sync, question, texts)


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
        return LocalCrossEncoderReranker(cfg.model_name, cfg.max_length, cfg.batch_size, cfg.model_kwargs)
    from backend.providers import get_provider

    return RemoteReranker(get_provider(preset, "rerank"), cfg.model_name, cfg.timeout_seconds)


async def rerank_pool(
    question: str,
    results: list,
    preset: Any,
    reranker: Optional[Reranker] = None,
    cache: Optional[dict[str, float]] = None,
) -> tuple[list, RerankOutcome]:
    """Rescore the first ``pool_size`` of ``results`` and sort them by the new score.

    Each rescored ``SearchResult`` gets ``score`` = cross-encoder score (in [0, 1])
    and keeps its vector score in ``vector_score``; ``pre_rerank_rank`` is its
    1-based position before. Candidates beyond ``pool_size`` follow, in their old
    order, with their scores scaled below the lowest rescored one so every later
    score-based step (MMR cap, proximity escalation) stays consistent. On any
    failure the input is returned unchanged.

    ``cache`` maps chunk ids to scores already computed for this question, so a
    retrieval escalation within the same query only scores the new candidates;
    it is filled in place.
    """
    cfg = getattr(preset, "rerank", None)
    if cfg is None:
        return results, RerankOutcome(status="off")
    base = {"model": cfg.model_name, "pool_size": cfg.pool_size}
    if not current_rerank_request().enabled:
        return results, RerankOutcome(status="disabled", **base)
    if not results:
        return results, RerankOutcome(status="ran", **base)
    started = time.monotonic()
    try:
        scorer = reranker or build_reranker(preset)
        base["provider"] = getattr(scorer, "provider_id", None)
        head, tail = results[: cfg.pool_size], results[cfg.pool_size:]
        known = cache if cache is not None else {}
        ids = [_chunk_id(r) for r in head]
        todo = [i for i, cid in enumerate(ids) if cid is None or cid not in known]
        fresh: list[float] = []
        if todo:
            fresh = await asyncio.wait_for(
                scorer.score(question, [head[i].chunk.text for i in todo]), timeout=cfg.timeout_seconds
            )
            if len(fresh) != len(todo):
                raise ValueError(f"re-ranker returned {len(fresh)} scores for {len(todo)} passages")
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
