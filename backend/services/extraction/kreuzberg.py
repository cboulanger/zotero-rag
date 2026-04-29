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
from typing import Any

import httpx

from backend.services.extraction.base import DocumentExtractor, ExtractionChunk

logger = logging.getLogger(__name__)

_TIMEOUT_RETRIES = 2
_TIMEOUT_RETRY_DELAY = 5.0


class KreuzbergTimeoutError(RuntimeError):
    """Raised when the kreuzberg sidecar times out after all retries."""


class KreuzbergParsingError(RuntimeError):
    """Raised when kreuzberg returns a 422 ParsingError (e.g. binary data in an HTML file)."""


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
        timeout_seconds: int = 300,
    ):
        """
        Args:
            kreuzberg_url: Base URL of the kreuzberg sidecar (e.g. "http://localhost:8100").
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
            ocr_enabled: Whether to attempt OCR on image-only pages.
            timeout_seconds: Per-request HTTP timeout. Large HTML snapshots and
                OCR-heavy PDFs may need more than the default 120s.
        """
        self._kreuzberg_url = kreuzberg_url.rstrip("/")
        self._ocr_enabled = ocr_enabled
        self._timeout_seconds = timeout_seconds
        self._config: dict[str, Any] = {
            "chunking": {
                "max_characters": max_chunk_size,
                "overlap": chunk_overlap,
            },
        }
        logger.debug(
            f"Initialized KreuzbergExtractor (url={kreuzberg_url}, "
            f"max_chars={max_chunk_size}, overlap={chunk_overlap}, ocr={ocr_enabled}, "
            f"timeout={timeout_seconds}s)"
        )

    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
    ) -> list[ExtractionChunk]:
        """
        Send document bytes to the kreuzberg sidecar and return extraction chunks.

        Args:
            content: Raw document bytes.
            mime_type: MIME type of the document (e.g. "application/pdf").

        Returns:
            List of ExtractionChunk objects, empty if extraction fails.
        """
        url = f"{self._kreuzberg_url}/extract"
        last_exc: httpx.TimeoutException | httpx.ReadError | None = None
        for attempt in range(1, _TIMEOUT_RETRIES + 2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(
                        url,
                        files={"files": ("document", content, mime_type)},
                        data={"config": json.dumps(self._config)},
                    )
                    response.raise_for_status()
                break  # success
            except httpx.ConnectError as exc:
                raise RuntimeError(
                    f"Cannot connect to kreuzberg sidecar at {self._kreuzberg_url}: {exc}. "
                    f"Ensure the kreuzberg container is running."
                ) from exc
            except httpx.HTTPStatusError as exc:
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
                last_exc = exc
                if attempt <= _TIMEOUT_RETRIES:
                    logger.warning(
                        f"kreuzberg sidecar timeout for mime={mime_type} "
                        f"(attempt {attempt}/{_TIMEOUT_RETRIES + 1}), retrying in {_TIMEOUT_RETRY_DELAY}s"
                    )
                    await asyncio.sleep(_TIMEOUT_RETRY_DELAY)
                else:
                    raise KreuzbergTimeoutError(
                        f"kreuzberg sidecar timed out for mime={mime_type} after {_TIMEOUT_RETRIES + 1} attempts"
                    ) from last_exc
            except httpx.ReadError as exc:
                # Sidecar dropped the connection mid-response (crash, OOM, etc.)
                last_exc = exc
                if attempt <= _TIMEOUT_RETRIES:
                    logger.warning(
                        f"kreuzberg sidecar read error for mime={mime_type} "
                        f"(attempt {attempt}/{_TIMEOUT_RETRIES + 1}), retrying in {_TIMEOUT_RETRY_DELAY}s"
                    )
                    await asyncio.sleep(_TIMEOUT_RETRY_DELAY)
                else:
                    raise KreuzbergTimeoutError(
                        f"kreuzberg sidecar connection dropped for mime={mime_type} after {_TIMEOUT_RETRIES + 1} attempts"
                    ) from last_exc

        try:
            results = response.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to parse kreuzberg response as JSON: {exc}") from exc

        # Response is a list of ExtractionResult objects (one per file sent)
        if not results or not isinstance(results, list):
            logger.debug(f"kreuzberg returned empty result list for mime={mime_type}")
            return []

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
