"""
Docling-backed document extraction + chunking adapter.

Talks to a remote Docling conversion service over HTTP and turns the returned
``DoclingDocument`` (Docling's lossless JSON export) into page-tracked chunks.
Two API flavours are supported, selected with ``api_flavor``:

``serve``
    A `docling-serve <https://github.com/docling-project/docling-serve>`_
    instance (self-hosted container or any hosted deployment of it).
    ``POST {url}/v1/convert/file`` with multipart field ``files`` and form
    options (``to_formats=json``, ``do_ocr``, ``image_export_mode``, ...);
    the response carries ``document.json_content``. With ``async_mode`` the
    job is submitted to ``/v1/convert/file/async`` and polled through
    ``/v1/status/poll/{task_id}`` and ``/v1/result/{task_id}``, which avoids
    the server's sync-wait limit on long OCR jobs. An API key, when the
    instance is configured with one, is sent as ``X-Api-Key``.

``saia``
    The Docling endpoint of the GWDG KISSKI/SAIA Academic Cloud platform.
    ``POST {url}/v1/documents/convert?response_type=json`` with multipart
    field ``document`` and ``Authorization: Bearer <key>`` (the same key as
    the platform's chat/embedding API); the response carries ``json_data``
    (a ``DoclingDocument``). The gateway allows one request per second and
    has hourly/daily quotas, so requests to one URL are spaced by
    ``min_request_interval`` and HTTP 429 is waited out (see
    ``_RATE_LIMIT_WAIT_BUDGET_SECONDS``).

Either flavour falls back to a Markdown export (``markdown`` /
``document.md_content``) when no JSON document is returned; those chunks
carry no page numbers.

Errors are reported with the same exception types as the Kreuzberg adapter
(``KreuzbergTimeoutError``, ``KreuzbergParsingError``,
``KreuzbergUnavailableError``, ``AttachmentTooLargeError``), because the
document processor and cron indexer classify extraction outcomes by them,
whichever service did the extraction.
"""

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from io import BytesIO
from typing import Any, Optional

import httpx
import pikepdf

from backend.services import diagnostics_collector as diag
from backend.services.chunking import create_simple_chunks
from backend.services.extraction.base import DocumentExtractor, ExtractionChunk
from backend.utils.pdf_splitter import inspect_pdf
from backend.services.extraction.kreuzberg import (
    AttachmentTooLargeError,
    KreuzbergParsingError,
    KreuzbergTimeoutError,
    KreuzbergUnavailableError,
    _compute_timeout,
)

logger = logging.getLogger(__name__)

API_FLAVORS = ("serve", "saia")

_DEFAULT_PATHS = {
    "serve": "/v1/convert/file",
    "saia": "/v1/documents/convert",
}

# Input formats Docling converts (docling.datamodel.base_models.InputFormat).
# Anything else is refused locally instead of costing a request (and, on SAIA,
# quota) only to come back as an error. Plain text is chunked locally: Docling
# has no plain-text reader, and the text needs no conversion.
_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "text/html",
    "application/xhtml+xml",
    "text/markdown",
    "text/x-markdown",
    "text/csv",
    "text/asciidoc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
}
_LOCAL_TEXT_MIME_TYPES = {"text/plain"}

_SPACE_RUNS = re.compile(r"[ \t]+")

# Furniture that repeats on every page and would only add noise to retrieval.
_SKIPPED_LABELS = {"page_header", "page_footer"}

_CONNECT_RETRY_BUDGET_SECONDS = 600
_CONNECT_RETRY_INTERVAL_SECONDS = 15
# How long one request may wait out HTTP 429 in total. SAIA's per-second limit
# clears within a second; a reset further away than this means the hourly or
# daily quota is spent, which every remaining attachment would hit too, so the
# extractor raises KreuzbergUnavailableError and the indexing run stops.
_RATE_LIMIT_WAIT_BUDGET_SECONDS = 120
# Gateway errors (502/503/504) on a single request are retried this many
# times with backoff. A gateway that closes slow requests after a fixed time
# (the outbound proxy of cloud sessions answers 502 "upstream request failed"
# after about 30 s) fails a slow PDF range on every retry, so multi-page
# ranges are split instead (see DoclingExtractor._convert_pdf_range).
_GATEWAY_RETRIES = 3
_GATEWAY_RETRY_BASE_SECONDS = 5
_ASYNC_POLL_WAIT_SECONDS = 10


class _GatewayError(RuntimeError):
    """HTTP 502/503/504 from the service or a gateway in front of it, after any retries."""


class _RequestSpacer:
    """Keeps consecutive requests to one URL at least ``interval`` seconds apart.

    Shared by every extractor instance in the process (the document processor
    builds a new extractor per request or indexing run) and safe across event
    loops and threads: a slot is reserved under a thread lock and the wait
    happens outside it.
    """

    _lock = threading.Lock()
    _next_slot: dict[str, float] = {}

    @classmethod
    async def wait(cls, key: str, interval: float) -> None:
        if interval <= 0:
            return
        with cls._lock:
            now = time.monotonic()
            slot = max(now, cls._next_slot.get(key, 0.0))
            cls._next_slot[key] = slot + interval
        delay = slot - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass
class _Block:
    """One readable unit of a Docling document, in reading order."""

    text: str
    page_number: Optional[int]
    is_heading: bool = False


def _first_page(item: dict) -> Optional[int]:
    for prov in item.get("prov") or []:
        page = prov.get("page_no")
        if isinstance(page, int):
            return page
    return None


def _table_text(item: dict) -> str:
    """Render a Docling table as pipe-separated rows (one line per row)."""
    data = item.get("data") or {}
    grid = data.get("grid")
    rows: list[list[str]] = []
    if isinstance(grid, list) and grid:
        for row in grid:
            rows.append([str((cell or {}).get("text") or "").strip() for cell in row])
    else:
        cells = data.get("table_cells") or []
        by_row: dict[int, dict[int, str]] = {}
        for cell in cells:
            r = cell.get("start_row_offset_idx", 0)
            c = cell.get("start_col_offset_idx", 0)
            by_row.setdefault(r, {})[c] = str(cell.get("text") or "").strip()
        for r in sorted(by_row):
            rows.append([by_row[r][c] for c in sorted(by_row[r])])
    lines = []
    for row in rows:
        # Spanning cells repeat in Docling's grid; collapse adjacent duplicates.
        deduped = [cell for i, cell in enumerate(row) if cell and (i == 0 or cell != row[i - 1])]
        if deduped:
            lines.append(" | ".join(deduped))
    return "\n".join(lines)


def docling_document_blocks(doc: dict) -> list[_Block]:
    """Flatten a ``DoclingDocument`` JSON dict into text blocks in reading order.

    Walks the ``body`` tree depth-first, resolving ``$ref`` pointers into
    ``texts``/``tables``/``groups``/``pictures``. Page headers/footers are
    dropped; picture and table captions are kept (they are children of their
    picture/table). Each block keeps the first page it appears on.
    """
    collections = {
        name: doc.get(name) or []
        for name in ("texts", "tables", "groups", "pictures", "key_value_items", "form_items")
    }

    def resolve(ref: str) -> Optional[dict]:
        parts = ref.lstrip("#/").split("/")
        if len(parts) != 2 or parts[0] not in collections:
            return None
        try:
            return collections[parts[0]][int(parts[1])]
        except (ValueError, IndexError):
            return None

    blocks: list[_Block] = []
    seen: set[str] = set()

    def walk(node: dict) -> None:
        for child in node.get("children") or []:
            ref = (child or {}).get("$ref", "")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            item = resolve(ref)
            if item is None:
                continue
            label = item.get("label", "")
            if ref.startswith("#/tables/"):
                text = _table_text(item)
                if text:
                    blocks.append(_Block(text, _first_page(item)))
            elif ref.startswith("#/texts/") and label not in _SKIPPED_LABELS:
                text = _SPACE_RUNS.sub(" ", item.get("text") or "").strip()
                if text:
                    blocks.append(_Block(text, _first_page(item), label in ("section_header", "title")))
            walk(item)

    walk(doc.get("body") or {})
    return blocks


_MD_HEADING = re.compile(r"^#{1,6}\s+")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<!--\s*image\s*-->")


def markdown_blocks(markdown: str) -> list[_Block]:
    """Split a Markdown export into paragraph blocks (no page information)."""
    blocks: list[_Block] = []
    for para in re.split(r"\n\s*\n", _MD_IMAGE.sub("", markdown)):
        text = para.strip()
        if text:
            heading = bool(_MD_HEADING.match(text))
            blocks.append(_Block(_MD_HEADING.sub("", text) if heading else text, None, heading))
    return blocks


def blocks_to_chunks(blocks: list[_Block], max_chunk_size: int, chunk_overlap: int) -> list[ExtractionChunk]:
    """Group consecutive blocks into chunks of at most ``max_chunk_size`` characters.

    A heading always starts a new chunk, so a section's title stays with its
    first paragraph; a block longer than the budget is split on word
    boundaries with ``chunk_overlap`` characters of overlap. A chunk's page is
    the page its first block starts on. The document processor merges these
    further up to the preset's chunk size (see ``coalesce_chunks``).
    """
    chunks: list[ExtractionChunk] = []
    buf: list[str] = []
    buf_page: Optional[int] = None
    buf_len = 0

    def emit(text: str, page: Optional[int]) -> None:
        chunks.append(ExtractionChunk(text=text, page_number=page, chunk_index=len(chunks)))

    def flush() -> None:
        nonlocal buf, buf_page, buf_len
        if buf:
            emit("\n\n".join(buf), buf_page)
        buf, buf_page, buf_len = [], None, 0

    for block in blocks:
        if len(block.text) > max_chunk_size:
            flush()
            for piece in create_simple_chunks(block.text, max_size=max_chunk_size, overlap=chunk_overlap):
                emit(piece.text, block.page_number)
            continue
        if buf and (block.is_heading or buf_len + 2 + len(block.text) > max_chunk_size):
            flush()
        if not buf:
            buf_page = block.page_number
        buf.append(block.text)
        buf_len += len(block.text) + (2 if len(buf) > 1 else 0)
    flush()
    return chunks


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """Seconds until a 429 clears, from Retry-After or the IETF RateLimit-Reset header."""
    for name in ("retry-after", "ratelimit-reset", "x-ratelimit-reset"):
        value = response.headers.get(name)
        if not value:
            continue
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                continue
    return None


def _quota_exhausted(response: httpx.Response) -> Optional[str]:
    """Name of a spent long-window quota (hour/day/month) from SAIA's Kong headers, if any."""
    for window in ("Month", "Day", "Hour"):
        if response.headers.get(f"x-ratelimit-remaining-{window.lower()}") == "0":
            return window.lower()
    return None


class DoclingExtractor(DocumentExtractor):
    """Extraction adapter for a remote Docling service (docling-serve or KISSKI/SAIA)."""

    def __init__(
        self,
        url: str,
        api_flavor: str = "serve",
        api_key: Optional[str] = None,
        api_key_name: Optional[str] = None,
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
        ocr_enabled: bool = True,
        timeout_cap: int = 1800,
        max_content_bytes: Optional[int] = None,
        connect_retry_budget_seconds: int = _CONNECT_RETRY_BUDGET_SECONDS,
        min_request_interval: float = 0.0,
        async_mode: bool = False,
        path: Optional[str] = None,
        extra_options: Optional[dict[str, Any]] = None,
        max_pages_per_request: Optional[int] = None,
    ):
        """
        Args:
            url: Base URL of the service (``http://localhost:5001`` for a local
                docling-serve, ``https://chat-ai.academiccloud.de`` for SAIA).
            api_flavor: ``"serve"`` or ``"saia"`` (see the module docstring).
            api_key: Key for the service; ``None`` sends no credential.
            api_key_name: Name of the provider key ``api_key`` came from (e.g.
                ``KISSKI_API_KEY``). When set and ``api_key`` is empty,
                extraction fails with KreuzbergUnavailableError naming it
                instead of sending an unauthenticated request.
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters when one block has to be split.
            ocr_enabled: Passed to docling-serve as ``do_ocr``. SAIA applies its
                own OCR settings and accepts no OCR option.
            timeout_cap: Upper bound (seconds) of the size-scaled request timeout.
            max_content_bytes: Refuse documents larger than this (None: no cap).
            connect_retry_budget_seconds: How long a connection failure is
                retried before KreuzbergUnavailableError (0: fail at once).
            min_request_interval: Minimum seconds between requests to this URL,
                shared by all extractor instances in the process.
            async_mode: docling-serve only: submit and poll instead of one
                blocking request.
            path: Override the conversion path of the flavour.
            extra_options: Extra form fields (serve) or query parameters (saia)
                sent with every request, e.g. ``{"table_mode": "accurate"}``.
            max_pages_per_request: Send PDFs in page ranges of at most this many
                pages (None: the whole file). Independently of it, a PDF range
                that times out or hits a gateway error is halved and retried,
                down to single pages; a single page that still fails is
                skipped (see ``_convert_pdf_range``).
        """
        if api_flavor not in API_FLAVORS:
            raise ValueError(f"Unknown Docling API flavour {api_flavor!r}. Available: {API_FLAVORS}")
        self._url = url.rstrip("/")
        self._flavor = api_flavor
        self._api_key = api_key
        self._api_key_name = api_key_name
        self._max_chunk_size = max_chunk_size
        self._chunk_overlap = chunk_overlap
        self._ocr_enabled = ocr_enabled
        self._timeout_cap = timeout_cap
        self._max_content_bytes = max_content_bytes
        self._connect_retry_budget_seconds = connect_retry_budget_seconds
        self._min_request_interval = min_request_interval
        self._async_mode = async_mode and api_flavor == "serve"
        self._path = path or _DEFAULT_PATHS[api_flavor]
        self._extra_options = dict(extra_options or {})
        self._max_pages_per_request = max_pages_per_request
        logger.debug(
            f"Initialized DoclingExtractor (url={self._url}, flavor={api_flavor}, "
            f"max_chars={max_chunk_size}, overlap={chunk_overlap}, async={self._async_mode})"
        )

    # ------------------------------------------------------------------ public
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
        timeout_multiplier: float = 1.0,
    ) -> list[ExtractionChunk]:
        """Convert the document with Docling and return page-tracked chunks.

        Raises:
            AttachmentTooLargeError: content exceeds max_content_bytes.
            KreuzbergParsingError: Docling cannot read this file (HTTP 415/422,
                or a MIME type Docling has no reader for).
            KreuzbergTimeoutError: the request timed out or the connection dropped.
            KreuzbergUnavailableError: the service is unreachable, rejects the
                key, or the rate-limit quota is spent (fatal for the run).
        """
        if self._max_content_bytes and len(content) > self._max_content_bytes:
            size_mb = len(content) / (1024 * 1024)
            limit_mb = self._max_content_bytes / (1024 * 1024)
            raise AttachmentTooLargeError(
                f"File is {size_mb:.0f} MB, which exceeds the {limit_mb:.0f} MB limit "
                f"for automatic text extraction (mime={mime_type})"
            )
        base_mime = mime_type.split(";")[0].strip().lower()
        if base_mime not in _LOCAL_TEXT_MIME_TYPES and self._api_key_name and not self._api_key:
            raise KreuzbergUnavailableError(
                f"No {self._api_key_name} for the Docling service at {self._url}: enter it in the "
                "plugin's preferences (or store it as a shared key via /api/config/remote-fields)."
            )
        if base_mime in _LOCAL_TEXT_MIME_TYPES:
            text = content.decode("utf-8", errors="replace")
            return blocks_to_chunks(markdown_blocks(text), self._max_chunk_size, self._chunk_overlap)
        if base_mime not in _SUPPORTED_MIME_TYPES:
            raise KreuzbergParsingError(f"Docling has no reader for mime={mime_type}")

        with diag.stage("docling_request") as stage:
            stage.set(
                flavor=self._flavor, path=self._path, mime_type=base_mime, size_bytes=len(content),
                timeout_multiplier=timeout_multiplier,
            )
            page_count = await self._pdf_page_count(content) if base_mime == "application/pdf" else None
            if page_count:
                blocks = await self._convert_pdf(content, page_count, timeout_multiplier, stage)
            else:
                timeout = _compute_timeout(
                    len(content), base_mime, cap=self._timeout_cap, multiplier=timeout_multiplier
                )
                payload = await self._convert(content, base_mime, timeout, stage)
                blocks = self._blocks_from_payload(payload)
            chunks = blocks_to_chunks(blocks, self._max_chunk_size, self._chunk_overlap)
            stage.set(blocks=len(blocks), chunks=len(chunks))
        logger.debug(f"DoclingExtractor: {len(chunks)} chunks from {len(blocks)} blocks (mime={base_mime})")
        return chunks

    # ------------------------------------------------------------------ PDF page ranges
    @staticmethod
    async def _pdf_page_count(content: bytes) -> Optional[int]:
        try:
            return (await asyncio.to_thread(inspect_pdf, content)).page_count or None
        except ValueError:
            return None  # unparseable locally: let Docling try the whole file

    async def _convert_pdf(self, content: bytes, page_count: int, multiplier: float, stage: Any) -> list[_Block]:
        """Convert a PDF range by range, keeping document page numbers.

        Ranges start at ``max_pages_per_request`` pages (the whole file when
        unset). When a range has to be halved (see ``_convert_pdf_range``), the
        rest of the document continues with the size that worked, so a slow
        service costs one failed request per document rather than one per range.
        """
        state = {"size": min(self._max_pages_per_request or page_count, page_count), "requests": 0}
        failed_pages: list[int] = []
        blocks: list[_Block] = []
        pos = 0
        while pos < page_count:
            pages = min(state["size"], page_count - pos)
            if pages == page_count:
                part = content
            else:
                try:
                    part = await asyncio.to_thread(_pdf_pages, content, pos, pages)
                except ValueError as exc:
                    if pos:
                        raise KreuzbergParsingError(f"Could not cut pages from the PDF: {exc}") from exc
                    logger.warning(f"Could not split PDF into page ranges ({exc}); sending it whole")
                    part, pages = content, page_count
            blocks += await self._convert_pdf_range(part, pos, pages, multiplier, stage, failed_pages, state)
            pos += pages
        stage.set(pages=page_count, requests=state["requests"], final_range_pages=state["size"],
                  **({"failed_pages": failed_pages[:50]} if failed_pages else {}))
        if failed_pages and len(failed_pages) >= page_count:
            raise KreuzbergTimeoutError(
                f"Docling could not convert any of the {page_count} pages (timeouts or gateway errors)"
            )
        if failed_pages:
            logger.warning(f"Docling skipped {len(failed_pages)} of {page_count} pages that kept failing")
        return blocks

    async def _convert_pdf_range(
        self, part: bytes, offset: int, pages: int, multiplier: float, stage: Any,
        failed_pages: list[int], state: dict[str, int],
    ) -> list[_Block]:
        """Convert one page range; on a timeout or gateway error split it in half and recurse.

        Some gateways (and the outbound proxy of cloud sessions) close a request
        after a fixed time, so a range that is too slow fails the same way on
        every retry; halving converges where retrying cannot. Multi-page ranges
        therefore skip the gateway retries; only single pages get them.
        """
        timeout = _compute_timeout(len(part), "application/pdf", cap=self._timeout_cap, multiplier=multiplier)
        state["requests"] += 1
        try:
            payload = await self._convert(part, "application/pdf", timeout, stage, retry_gateway=pages <= 1)
        except (KreuzbergTimeoutError, _GatewayError) as exc:
            if pages <= 1:
                logger.warning(f"Docling failed on page {offset + 1}: {exc}; skipping it")
                failed_pages.append(offset + 1)
                return []
            half = (pages + 1) // 2
            state["size"] = min(state["size"], half)
            logger.info(
                f"Docling failed on pages {offset + 1}-{offset + pages} ({exc}); "
                f"retrying in ranges of {half} pages"
            )
            blocks: list[_Block] = []
            for sub_offset in range(0, pages, half):
                sub_pages = min(half, pages - sub_offset)
                try:
                    sub = await asyncio.to_thread(_pdf_pages, part, sub_offset, sub_pages)
                except ValueError:
                    raise exc
                blocks += await self._convert_pdf_range(
                    sub, offset + sub_offset, sub_pages, multiplier, stage, failed_pages, state
                )
            return blocks
        blocks = self._blocks_from_payload(payload)
        for block in blocks:
            if block.page_number is not None:
                block.page_number += offset
        return blocks

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _blocks_from_payload(payload: dict) -> list[_Block]:
        document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
        doc_json = payload.get("json_data") or document.get("json_content")
        if isinstance(doc_json, dict) and doc_json.get("body"):
            return docling_document_blocks(doc_json)
        markdown = payload.get("markdown") or document.get("md_content") or ""
        return markdown_blocks(markdown) if isinstance(markdown, str) else []

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            if self._flavor == "saia":
                headers["Authorization"] = f"Bearer {self._api_key}"
            else:
                headers["X-Api-Key"] = self._api_key
        return headers

    def _request_kwargs(self, content: bytes, mime_type: str) -> dict[str, Any]:
        filename = "document" + _extension_for(mime_type)
        if self._flavor == "saia":
            return {
                "params": {"response_type": "json", **self._extra_options},
                "files": {"document": (filename, content, mime_type)},
            }
        data: dict[str, Any] = {
            "to_formats": "json",
            "do_ocr": "true" if self._ocr_enabled else "false",
            "image_export_mode": "placeholder",
        }
        data.update({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in self._extra_options.items()})
        return {"data": data, "files": {"files": (filename, content, mime_type)}}

    async def _convert(
        self, content: bytes, mime_type: str, timeout: int, stage: Any, retry_gateway: bool = True
    ) -> dict:
        async with httpx.AsyncClient(timeout=timeout, headers=self._headers()) as client:
            if not self._async_mode:
                response = await self._send(
                    client, "POST", self._url + self._path, stage, retry_gateway=retry_gateway,
                    **self._request_kwargs(content, mime_type),
                )
                return self._json(response)
            # docling-serve async: submit, poll until done, fetch the result.
            submitted = self._json(await self._send(
                client, "POST", self._url + self._path + "/async", stage, **self._request_kwargs(content, mime_type)
            ))
            task_id = submitted.get("task_id")
            if not task_id:
                raise RuntimeError(f"docling-serve returned no task_id: {submitted}")
            deadline = time.monotonic() + timeout
            status = submitted.get("task_status")
            while status not in ("success", "failure"):
                if time.monotonic() > deadline:
                    stage.set(failure="timeout")
                    raise KreuzbergTimeoutError(
                        f"docling-serve task {task_id} not finished within {timeout}s (mime={mime_type})"
                    )
                polled = self._json(await self._send(
                    client, "GET", f"{self._url}/v1/status/poll/{task_id}", stage,
                    params={"wait": _ASYNC_POLL_WAIT_SECONDS},
                ))
                status = polled.get("task_status")
            if status == "failure":
                raise KreuzbergParsingError(f"docling-serve task {task_id} failed (mime={mime_type})")
            return self._json(await self._send(client, "GET", f"{self._url}/v1/result/{task_id}", stage))

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Docling response is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected Docling response type {type(data).__name__}")
        return data

    async def _send(
        self, client: httpx.AsyncClient, method: str, url: str, stage: Any, retry_gateway: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """One request with this service's retry rules (see the module constants).

        ``retry_gateway=False`` raises _GatewayError on the first 502/503/504
        instead of retrying, for a caller that splits the work instead.
        """
        connect_deadline = time.monotonic() + self._connect_retry_budget_seconds
        rate_limit_waited = 0.0
        gateway_attempts = 0
        while True:
            await _RequestSpacer.wait(self._url, self._min_request_interval)
            try:
                response = await client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                remaining = connect_deadline - time.monotonic()
                if remaining <= 0:
                    stage.set(failure="connect_unavailable", exception=type(exc).__name__)
                    raise KreuzbergUnavailableError(
                        f"Docling service at {self._url} still unreachable after retrying for "
                        f"{self._connect_retry_budget_seconds}s: {exc}"
                    ) from exc
                wait = min(_CONNECT_RETRY_INTERVAL_SECONDS, remaining)
                logger.warning(f"Cannot reach Docling service at {self._url}: {exc}. Retrying in {wait:.0f}s.")
                await asyncio.sleep(wait)
                continue
            except httpx.TimeoutException as exc:
                stage.set(failure="timeout", exception=type(exc).__name__)
                raise KreuzbergTimeoutError(f"Docling service at {self._url} timed out ({exc})") from exc
            except httpx.ReadError as exc:
                stage.set(failure="connection_dropped", exception=type(exc).__name__)
                raise KreuzbergTimeoutError(f"Docling service at {self._url} dropped the connection ({exc})") from exc

            status = response.status_code
            stage.set(http_status=status)
            if status < 400:
                return response
            body = diag.body_excerpt(response.text)
            stage.set(response_body=body)
            if status == 429:
                wait = _retry_after_seconds(response)
                quota = _quota_exhausted(response)
                if quota or (wait is not None and rate_limit_waited + wait > _RATE_LIMIT_WAIT_BUDGET_SECONDS):
                    stage.set(failure="rate_limited")
                    raise KreuzbergUnavailableError(
                        f"Docling service at {self._url} rate limit exhausted"
                        + (f" ({quota}ly quota spent)" if quota else f" (reset in {wait:.0f}s)")
                    )
                wait = 1.0 if wait is None else max(wait, 0.5)
                if rate_limit_waited + wait > _RATE_LIMIT_WAIT_BUDGET_SECONDS:
                    raise KreuzbergUnavailableError(f"Docling service at {self._url} keeps answering HTTP 429")
                rate_limit_waited += wait
                await asyncio.sleep(wait)
                continue
            if status in (502, 503, 504) and (not retry_gateway or gateway_attempts >= _GATEWAY_RETRIES):
                stage.set(failure="gateway_error")
                raise _GatewayError(f"Docling service at {self._url} returned HTTP {status}: {body}")
            if status in (502, 503, 504):
                gateway_attempts += 1
                wait = _GATEWAY_RETRY_BASE_SECONDS * 2 ** (gateway_attempts - 1)
                logger.warning(
                    f"Docling service at {self._url} answered HTTP {status} "
                    f"(attempt {gateway_attempts}/{_GATEWAY_RETRIES}); retrying in {wait}s"
                )
                await asyncio.sleep(wait)
                continue
            if status in (401, 403):
                stage.set(failure="unauthorized")
                raise KreuzbergUnavailableError(
                    f"Docling service at {self._url} rejected the credentials (HTTP {status}): {body}"
                )
            if status in (415, 422):
                raise KreuzbergParsingError(f"Docling could not convert this file (HTTP {status}): {body}")
            raise RuntimeError(f"Docling service at {self._url} returned HTTP {status}: {body}")


_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/csv": ".csv",
    "text/asciidoc": ".adoc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}


def _pdf_pages(content: bytes, start: int, count: int) -> bytes:
    """A new PDF holding pages ``start``..``start+count-1`` (0-based) of ``content``.

    pikepdf copies only the objects those pages reference (see
    backend.utils.pdf_splitter.split_pdf_bytes).

    Raises:
        ValueError: the PDF cannot be parsed.
    """
    try:
        with pikepdf.open(BytesIO(content)) as src:
            dst = pikepdf.Pdf.new()
            dst.pages.extend(src.pages[start:start + count])
            buf = BytesIO()
            dst.save(buf)
            dst.close()
            return buf.getvalue()
    except pikepdf.PdfError as exc:
        raise ValueError(str(exc)) from exc


def _extension_for(mime_type: str) -> str:
    """File extension Docling uses to pick its reader (it sniffs by name as well as content)."""
    return _EXTENSIONS.get(mime_type, "")
