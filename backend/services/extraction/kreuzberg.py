"""
Kreuzberg-backed document extraction + chunking adapter.

Calls the kreuzberg sidecar container's HTTP API instead of importing the
kreuzberg Python library directly.  This eliminates the Rust/maturin build
dependency from the main image.

Kreuzberg HTTP API (POST /extract):
  - multipart body: `file` (bytes) + optional `config` (JSON string)
  - response:  {"chunks": [{"content": str, "metadata": {...}}, ...]}
  - chunk metadata keys:  "first_page" (1-based int | null), "chunk_index" (int)

See https://docs.kreuzberg.dev/guides/docker/ for full API reference.
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from backend.services import diagnostics_collector as diag
from backend.services.extraction.base import DocumentExtractor, ExtractionChunk

logger = logging.getLogger(__name__)

_TIMEOUT_FLOOR = 60       # seconds
_TIMEOUT_CAP_DEFAULT = 1800    # seconds (30 min) — overridable via Settings.kreuzberg_timeout_seconds
_BYTES_PER_SECOND_PDF = 3_000    # OCR-heavy; slow per byte
_BYTES_PER_SECOND_OTHER = 10_000

# A connection failure (sidecar not accepting connections at all) gets a
# generous retry budget before giving up — the sidecar restarting after a
# crash/OOM/deploy is a known, recoverable condition (see docs/ci-cd.md's
# deploy notes), unlike a request the sidecar actively rejected. Once the
# budget is exhausted, every subsequent attachment would fail identically
# until the sidecar comes back, so KreuzbergUnavailableError is treated as
# fatal by the indexing loop (see document_processor._FATAL_PROCESSING_ERRORS)
# rather than retried fresh for each attachment.
_CONNECT_RETRY_BUDGET_SECONDS = 600  # 10 minutes
_CONNECT_RETRY_INTERVAL_SECONDS = 15


def _compute_timeout(
    content_size: int,
    mime_type: str,
    cap: int = _TIMEOUT_CAP_DEFAULT,
    multiplier: float = 1.0,
) -> int:
    """Return a per-request timeout scaled to document size and type.

    `multiplier` scales both the size-based computed value and the cap by the
    same factor — scaling only the raw value would be a no-op for any file
    already large enough to saturate the cap, which is exactly the case a
    repair retry with a longer timeout needs to help.
    """
    rate = _BYTES_PER_SECOND_PDF if mime_type == "application/pdf" else _BYTES_PER_SECOND_OTHER
    scaled_cap = int(cap * multiplier)
    scaled_value = int((content_size // rate) * multiplier)
    return max(_TIMEOUT_FLOOR, min(scaled_cap, scaled_value))


class KreuzbergTimeoutError(RuntimeError):
    """Raised when the kreuzberg sidecar times out after all retries."""


class KreuzbergParsingError(RuntimeError):
    """Raised when kreuzberg returns a 422 ParsingError (e.g. binary data in an HTML file)."""


class KreuzbergUnavailableError(RuntimeError):
    """Raised when the kreuzberg sidecar is still unreachable after retrying
    connection attempts for _CONNECT_RETRY_BUDGET_SECONDS. Treated as fatal by
    the indexing loop — see document_processor._FATAL_PROCESSING_ERRORS."""


class AttachmentTooLargeError(RuntimeError):
    """Raised when content exceeds max_content_bytes — refused before ever reaching kreuzberg.

    Unlike KreuzbergTimeoutError (kreuzberg tried and ran out of time) this is a
    pre-flight refusal: the request is never sent. This is what protects kreuzberg's
    memory limit from a single pathologically large document (a 329MB scanned PDF
    OOM-killed the sidecar's 8GB cgroup in production) — including the PDF-splitting
    fallback path, which otherwise sends the whole original file as one part when
    splitting itself fails, bypassing the size-based splitting entirely.
    """


class KreuzbergExtractor(DocumentExtractor):
    """
    Extraction adapter that calls the kreuzberg sidecar HTTP API.

    Supports PDF (with optional OCR), DOCX, HTML, EPUB, and 87+ other formats.
    OCR is handled by the sidecar container (Tesseract is bundled there).
    """

    def __init__(
        self,
        kreuzberg_url: str = "http://localhost:8100",
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
        ocr_enabled: bool = True,
        timeout_cap: int = _TIMEOUT_CAP_DEFAULT,
        max_content_bytes: Optional[int] = None,
        connect_retry_budget_seconds: int = _CONNECT_RETRY_BUDGET_SECONDS,
    ):
        """
        Args:
            kreuzberg_url: Base URL of the kreuzberg sidecar (e.g. "http://localhost:8100").
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
            ocr_enabled: Whether to attempt OCR on image-only pages.
            timeout_cap: Upper bound (seconds) for the per-request timeout computed
                from document size — see _compute_timeout(). Normally comes from
                Settings.kreuzberg_timeout_seconds.
            max_content_bytes: Hard cap on bytes sent to kreuzberg in one request;
                None means no cap. Normally comes from Settings.kreuzberg_max_content_bytes.
                Deliberately NOT scaled by extract_and_chunk's timeout_multiplier — a
                document refused for being too large needs a smaller/better file, not
                a longer timeout, so the Fix Unavailable "retry with longer timeout"
                action must not be able to bypass this cap.
            connect_retry_budget_seconds: How long to retry a connection failure
                (sidecar not accepting connections at all) before raising
                KreuzbergUnavailableError — see that class's docstring. 0 means
                fail on the first attempt (DocumentProcessor passes 0 in
                settings.testing, so a test exercising the real upload path
                without mocking httpx fails fast instead of hanging for minutes).
        """
        self._kreuzberg_url = kreuzberg_url.rstrip("/")
        self._connect_retry_budget_seconds = connect_retry_budget_seconds
        self._ocr_enabled = ocr_enabled
        self._timeout_cap = timeout_cap
        self._max_content_bytes = max_content_bytes
        self._config: dict[str, Any] = {
            "chunking": {
                "max_characters": max_chunk_size,
                "overlap": chunk_overlap,
            },
            "force_ocr": ocr_enabled,
        }
        logger.debug(
            f"Initialized KreuzbergExtractor (url={kreuzberg_url}, "
            f"max_chars={max_chunk_size}, overlap={chunk_overlap}, ocr={ocr_enabled}, "
            f"timeout_cap={timeout_cap})"
        )

    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
        timeout_multiplier: float = 1.0,
    ) -> list[ExtractionChunk]:
        """
        Send document bytes to the kreuzberg sidecar and return extraction chunks.

        Args:
            content: Raw document bytes.
            mime_type: MIME type of the document (e.g. "application/pdf").
            timeout_multiplier: Scales both the computed per-request timeout and
                this instance's configured cap by this factor. Used only by the
                Fix Unavailable repair action for attachments that previously hit
                `skipped_timeout` — normal indexing always uses the default 1.0.

        Returns:
            List of ExtractionChunk objects, empty if extraction fails.

        Raises:
            AttachmentTooLargeError: If content exceeds max_content_bytes — refused
                before ever contacting kreuzberg.
        """
        if self._max_content_bytes and len(content) > self._max_content_bytes:
            size_mb = len(content) / (1024 * 1024)
            limit_mb = self._max_content_bytes / (1024 * 1024)
            raise AttachmentTooLargeError(
                f"File is {size_mb:.0f} MB, which exceeds the {limit_mb:.0f} MB limit "
                f"for automatic text extraction (mime={mime_type})"
            )
        url = f"{self._kreuzberg_url}/extract"
        timeout = _compute_timeout(len(content), mime_type, cap=self._timeout_cap, multiplier=timeout_multiplier)
        logger.debug(
            f"kreuzberg request: mime={mime_type} size={len(content)} timeout={timeout}s "
            f"(cap={self._timeout_cap}, multiplier={timeout_multiplier})"
        )
        with diag.stage("kreuzberg_request") as kb_stage:
            kb_stage.set(
                path="/extract", mime_type=mime_type, size_bytes=len(content),
                computed_timeout_seconds=timeout, timeout_cap_seconds=self._timeout_cap,
                timeout_multiplier=timeout_multiplier,
            )
            return await self._post_extract(url, content, mime_type, timeout, kb_stage)

    async def _post_extract(
        self, url: str, content: bytes, mime_type: str, timeout: int, kb_stage: Any
    ) -> list[ExtractionChunk]:
        """POST to the sidecar and parse chunks; records HTTP status/body on ``kb_stage``.

        A connection failure (sidecar not accepting connections at all) is
        retried for up to self._connect_retry_budget_seconds before raising
        KreuzbergUnavailableError — see that class's docstring. Any other
        failure (HTTP error status, timeout, dropped connection) is not
        retried here; those aren't "sidecar is down" conditions.
        """
        deadline = time.monotonic() + self._connect_retry_budget_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        url,
                        files={"files": ("document", content, mime_type)},
                        data={"config": json.dumps(self._config)},
                    )
                    kb_stage.set(http_status=response.status_code)
                    response.raise_for_status()
                break
            except httpx.ConnectError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    kb_stage.set(failure="connect_unavailable", exception=type(exc).__name__)
                    raise KreuzbergUnavailableError(
                        f"kreuzberg sidecar at {self._kreuzberg_url} still unreachable after "
                        f"retrying for {self._connect_retry_budget_seconds}s: {exc}"
                    ) from exc
                wait = min(_CONNECT_RETRY_INTERVAL_SECONDS, remaining)
                logger.warning(
                    f"Cannot connect to kreuzberg sidecar at {self._kreuzberg_url} "
                    f"(attempt {attempt}, {remaining:.0f}s left in retry budget): {exc}. "
                    f"Retrying in {wait:.0f}s."
                )
                await asyncio.sleep(wait)
            except httpx.HTTPStatusError as exc:
                kb_stage.set(http_status=exc.response.status_code, response_body=diag.body_excerpt(exc.response.text))
                if exc.response.status_code == 422:
                    try:
                        body = exc.response.json()
                        if body.get("error_type") == "ParsingError":
                            raise KreuzbergParsingError(
                                f"kreuzberg sidecar returned HTTP 422 for mime={mime_type}: {exc.response.text}"
                            ) from exc
                    except (ValueError, AttributeError):
                        pass
                raise RuntimeError(
                    f"kreuzberg sidecar returned HTTP {exc.response.status_code} "
                    f"for mime={mime_type}: {exc.response.text}"
                ) from exc
            except httpx.TimeoutException as exc:
                kb_stage.set(failure="timeout", exception=type(exc).__name__)
                raise KreuzbergTimeoutError(
                    f"kreuzberg sidecar timed out for mime={mime_type} "
                    f"(size={len(content)}, timeout={timeout}s)"
                ) from exc
            except httpx.ReadError as exc:
                kb_stage.set(failure="connection_dropped", exception=type(exc).__name__)
                raise KreuzbergTimeoutError(
                    f"kreuzberg sidecar connection dropped for mime={mime_type} "
                    f"(size={len(content)}, timeout={timeout}s)"
                ) from exc

        try:
            results = response.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to parse kreuzberg response as JSON: {exc}") from exc

        # Response is a list of ExtractionResult objects (one per file sent)
        if not results or not isinstance(results, list):
            logger.debug(f"kreuzberg returned empty result list for mime={mime_type}")
            return []

        kb_stage.set(result_count=len(results))
        # We send one file, so take the first result
        first_result = results[0]
        raw_chunks = first_result.get("chunks") or []

        if not raw_chunks:
            logger.debug(f"kreuzberg returned no chunks for mime={mime_type}")
            return []

        extraction_chunks: list[ExtractionChunk] = []
        for chunk in raw_chunks:
            text = chunk.get("content") or ""
            if not text.strip():
                continue
            meta = chunk.get("metadata") or {}
            extraction_chunks.append(
                ExtractionChunk(
                    text=text,
                    page_number=meta.get("first_page"),  # 1-based; None for non-paginated formats
                    chunk_index=meta.get("chunk_index", len(extraction_chunks)),
                )
            )

        logger.debug(
            f"KreuzbergExtractor: {len(extraction_chunks)} chunks "
            f"from kreuzberg sidecar (mime={mime_type})"
        )
        return extraction_chunks
