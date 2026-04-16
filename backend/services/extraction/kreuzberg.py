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

import json
import logging
from typing import Any

import httpx

from backend.services.extraction.base import DocumentExtractor, ExtractionChunk

logger = logging.getLogger(__name__)


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
    ):
        """
        Args:
            kreuzberg_url: Base URL of the kreuzberg sidecar (e.g. "http://localhost:8100").
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
            ocr_enabled: Whether to attempt OCR on image-only pages.
        """
        self._kreuzberg_url = kreuzberg_url.rstrip("/")
        self._ocr_enabled = ocr_enabled
        self._config: dict[str, Any] = {
            "chunking": {
                "max_chars": max_chunk_size,
                "max_overlap": chunk_overlap,
            },
            "disable_ocr": not ocr_enabled,
        }
        logger.info(
            f"Initialized KreuzbergExtractor (url={kreuzberg_url}, "
            f"max_chars={max_chunk_size}, overlap={chunk_overlap}, ocr={ocr_enabled})"
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
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    url,
                    files={"file": ("document", content, mime_type)},
                    data={"config": json.dumps(self._config)},
                )
                response.raise_for_status()
        except httpx.ConnectError as exc:
            logger.error(
                f"Cannot connect to kreuzberg sidecar at {self._kreuzberg_url}: {exc}. "
                f"Ensure the kreuzberg container is running."
            )
            return []
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"kreuzberg sidecar returned HTTP {exc.response.status_code} "
                f"for mime={mime_type}: {exc.response.text}"
            )
            return []
        except httpx.TimeoutException as exc:
            logger.error(f"Request to kreuzberg sidecar timed out (mime={mime_type}): {exc}")
            return []

        try:
            payload = response.json()
        except Exception as exc:
            logger.error(f"Failed to parse kreuzberg response as JSON: {exc}")
            return []

        raw_chunks = payload.get("chunks") or []
        if not raw_chunks:
            logger.debug(f"kreuzberg returned no chunks for mime={mime_type}")
            return []

        extraction_chunks: list[ExtractionChunk] = []
        for chunk in raw_chunks:
            text = chunk.get("content") or chunk.get("text") or ""
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
