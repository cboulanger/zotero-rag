"""
Remote document upload API endpoints.

These endpoints are used when the Zotero plugin connects to a remote backend
that cannot access local Zotero files directly.  The plugin reads attachment
bytes locally and uploads them together with item metadata.

Workflow
--------
1. Plugin calls POST /api/libraries/{id}/check-indexed with the list of items
   it has locally.  The backend replies with which attachments need indexing.
2. Plugin uploads each needed attachment via POST /api/index/document.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from backend.db.vector_store import VectorStore, _extract_lastnames
from backend.dependencies import get_client_api_keys, get_vector_store, make_embedding_service
from backend.models.document import (
    CURRENT_SCHEMA_VERSION,
    DocumentMetadata,
)
from backend.models.library import LibraryIndexMetadata
from backend.services.document_processor import DocumentProcessor
from backend.services.registration_service import RegistrationService
from backend.config.settings import Settings, get_settings
from backend.utils import format_file_size

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server-side item-level cache for check-indexed results
# ---------------------------------------------------------------------------
# Keyed by library_id -> {item_key: indexed_version | None}
# None means "confirmed not indexed at time of last check".
# No TTL — cleared on server restart; force_refresh bypasses reads;
# successful uploads update specific entries.
# ---------------------------------------------------------------------------

_check_indexed_item_cache: dict[str, dict[str, dict | None]] = {}
_check_indexed_item_cache_lock = Lock()


def _get_cached_item_versions(
    library_id: str, item_keys: list[str], force_refresh: bool
) -> tuple[dict[str, dict | None], list[str]]:
    """Return (cache_hits, cache_misses).

    cache_hits maps item_key -> state_dict|None (None = confirmed not indexed).
    cache_misses is the list of item_keys not found in the cache.
    When force_refresh=True all keys are treated as misses.
    """
    if force_refresh:
        return {}, list(item_keys)
    with _check_indexed_item_cache_lock:
        lib = _check_indexed_item_cache.get(library_id, {})
        hits = {k: lib[k] for k in item_keys if k in lib}
        misses = [k for k in item_keys if k not in lib]
    return hits, misses


def _update_item_cache(library_id: str, updates: dict[str, dict | None]) -> None:
    """Merge {item_key: state_dict|None} entries into the item-level cache."""
    with _check_indexed_item_cache_lock:
        _check_indexed_item_cache.setdefault(library_id, {}).update(updates)


def load_item_cache(path: Path) -> None:
    """Load the item-level cache from disk (called once at startup)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Migrate old format: bare int values → {"item_version": int}
        for lib_entries in data.values():
            for key, val in lib_entries.items():
                if isinstance(val, int):
                    lib_entries[key] = {"item_version": val}
        with _check_indexed_item_cache_lock:
            _check_indexed_item_cache.clear()
            _check_indexed_item_cache.update(data)
        total = sum(len(v) for v in data.values())
        logger.info(f"Loaded check-indexed item cache from {path} ({len(data)} libraries, {total} entries)")
    except FileNotFoundError:
        logger.info(f"No check-indexed cache file at {path} — starting with empty cache")
    except Exception as e:
        logger.warning(f"Failed to load check-indexed cache from {path}: {e} — starting with empty cache")


def save_item_cache(path: Path) -> None:
    """Persist the item-level cache to disk (called once at shutdown)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _check_indexed_item_cache_lock:
            data = {lib: dict(entries) for lib, entries in _check_indexed_item_cache.items()}
        path.write_text(json.dumps(data), encoding="utf-8")
        total = sum(len(v) for v in data.values())
        logger.info(f"Saved check-indexed item cache to {path} ({len(data)} libraries, {total} entries)")
    except Exception as e:
        logger.warning(f"Failed to save check-indexed cache to {path}: {e}")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AttachmentInfo(BaseModel):
    """Attachment info sent by the plugin for index-plan checks."""

    item_key: str
    attachment_key: str
    mime_type: str = "application/pdf"
    item_version: int = 0
    attachment_version: int = 0


class CheckIndexedRequest(BaseModel):
    """Batch check: which of these attachments need (re-)indexing?"""

    library_id: str
    library_type: str = "user"
    attachments: list[AttachmentInfo]
    force_refresh: bool = False  # set by client when doing a full reindex


class AttachmentIndexStatus(BaseModel):
    """Per-attachment result returned by the check endpoint."""

    item_key: str
    attachment_key: str
    needs_indexing: bool
    reason: str  # "not_indexed" | "version_changed" | "up_to_date"
    needs_metadata_update: bool = False  # True when schema_version < CURRENT_SCHEMA_VERSION


class ItemMetadataUpdate(BaseModel):
    """Metadata fields to update for a single Zotero item."""

    item_key: str
    title: Optional[str] = None
    authors: list[str] = []
    year: Optional[int] = None
    item_type: Optional[str] = None


class BatchMetadataUpdateRequest(BaseModel):
    """Batch metadata-only update request (no file re-upload, no re-embedding)."""

    library_id: str
    items: list[ItemMetadataUpdate]


class BatchMetadataUpdateResult(BaseModel):
    """Result of a batch metadata update."""

    library_id: str
    updated_items: int
    updated_chunks: int


class CheckIndexedResponse(BaseModel):
    """Response from the check-indexed endpoint."""

    library_id: str
    statuses: list[AttachmentIndexStatus]


class DocumentUploadResult(BaseModel):
    """Result returned after uploading a single document."""

    library_id: str
    item_key: str
    attachment_key: str
    chunks_added: int
    status: str  # "indexed" | "skipped_duplicate" | "error"
    message: str = ""
    rate_limit_retries: int = 0
    rate_limit_headers: dict[str, str] | None = None


class AbstractIndexRequest(BaseModel):
    """Request to index an item via its abstractNote (no attachment file)."""

    library_id: str
    library_type: str = "user"
    item_key: str
    item_version: int = 0
    title: Optional[str] = "Untitled"
    authors: list[str] = []
    year: Optional[int] = None
    item_type: Optional[str] = None
    zotero_modified: str = ""
    abstract_text: str
    library_name: str = ""
    user_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_registration(library_id: str, user_id: Optional[int], settings: Settings) -> None:
    """Raise 403 if registration is required and the user is not registered.

    Skipped when api_host is localhost/127.0.0.1 or REQUIRE_REGISTRATION=false.
    """
    if not settings.require_registration:
        return
    if settings.api_host in ("localhost", "127.0.0.1"):
        return
    service = RegistrationService(settings.registrations_path)
    if not service.is_registered(library_id, user_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "Library not registered for this user. "
                "Please update the plugin to the newest version."
            ),
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/libraries/{library_id}/check-indexed",
    response_model=CheckIndexedResponse,
    summary="Check which attachments need indexing (remote mode)",
)
async def check_indexed(
    library_id: str,
    request: CheckIndexedRequest,
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Given a list of attachments the plugin has locally, return which ones
    need (re-)indexing on the backend.

    The plugin uses this to build the upload queue before calling
    POST /api/index/document for each attachment that needs work.

    Args:
        library_id: Zotero library ID (from URL path, must match request body).
        request: List of attachments with current version numbers.

    Returns:
        Per-attachment status indicating whether indexing is needed.

    Raises:
        HTTPException 400: If library_id path param does not match request body.
        HTTPException 503: If the vector store is unavailable.
    """
    if library_id != request.library_id:
        raise HTTPException(
            status_code=400,
            detail="library_id in URL must match library_id in request body",
        )

    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    statuses: list[AttachmentIndexStatus] = []

    item_keys = [att.item_key for att in request.attachments]
    cache_hits, cache_misses = _get_cached_item_versions(library_id, item_keys, request.force_refresh)

    fresh_states: dict[str, dict] = {}
    if cache_misses:
        try:
            fresh_states = await asyncio.to_thread(vector_store.get_item_states_bulk, library_id, cache_misses)
        except Exception as exc:
            from qdrant_client.http.exceptions import ResponseHandlingException
            if isinstance(exc, ResponseHandlingException):
                logger.warning(f"check-indexed failed for library={library_id} (transient Qdrant error): {exc}")
            else:
                logger.exception(f"check-indexed failed for library={library_id}: {exc}")
            raise HTTPException(status_code=500, detail=f"Vector store error: {exc}") from exc
        # Cache results: None for items confirmed absent from the index
        _update_item_cache(library_id, {k: fresh_states.get(k) for k in cache_misses})

    # Merge: cache_hits has dict|None, fresh_states has dict (absent = not indexed → None)
    item_states: dict[str, dict | None] = {
        **cache_hits,
        **{k: fresh_states.get(k) for k in cache_misses},
    }

    logger.info(
        f"Check-indexed: library={library_id} items={len(item_keys)} "
        f"cache_hits={len(cache_hits)} cache_misses={len(cache_misses)} "
        f"force_refresh={request.force_refresh}"
    )

    for att in request.attachments:
        state = item_states.get(att.item_key)

        if state is None:
            statuses.append(AttachmentIndexStatus(
                item_key=att.item_key,
                attachment_key=att.attachment_key,
                needs_indexing=True,
                reason="not_indexed",
            ))
        elif state["item_version"] < att.item_version:
            statuses.append(AttachmentIndexStatus(
                item_key=att.item_key,
                attachment_key=att.attachment_key,
                needs_indexing=True,
                reason="version_changed",
            ))
        else:
            schema_outdated = state.get("schema_version", 2) < CURRENT_SCHEMA_VERSION
            statuses.append(AttachmentIndexStatus(
                item_key=att.item_key,
                attachment_key=att.attachment_key,
                needs_indexing=False,
                reason="up_to_date",
                needs_metadata_update=schema_outdated,
            ))

        # Repair missing library metadata if chunks already exist.
        # This handles the case where a previous indexing run stored chunks
        # but never wrote library_metadata (e.g. before the metadata-update fix).
        has_indexed_content = any(
            s.reason in ("up_to_date", "version_changed") for s in statuses
        )
        if has_indexed_content and not vector_store.get_library_metadata(library_id):
            best_version = max(
                (a.item_version for a, s in zip(request.attachments, statuses)
                 if s.reason in ("up_to_date", "version_changed")),
                default=0,
            )
            up_to_date_count = sum(
                1 for s in statuses if s.reason in ("up_to_date", "version_changed")
            )
            repaired = LibraryIndexMetadata(
                library_id=library_id,
                library_type=request.library_type,
                library_name="",
                last_indexed_version=best_version,
                last_indexed_at=datetime.now(timezone.utc).isoformat(),
                total_chunks=vector_store.count_library_chunks(library_id),
                total_items_indexed=up_to_date_count,
                indexing_mode="incremental",
            )
            vector_store.update_library_metadata(repaired)
            logger.info(
                f"Repaired missing library_metadata for {library_id} "
                f"(chunks={repaired.total_chunks}, version={best_version})"
            )

    _reason_counts: dict[str, int] = {}
    for _s in statuses:
        _reason_counts[_s.reason] = _reason_counts.get(_s.reason, 0) + 1
    logger.info(
        f"[DIAG] check-indexed reasons: library={library_id} "
        + " ".join(f"{k}={v}" for k, v in sorted(_reason_counts.items()))
    )

    return CheckIndexedResponse(library_id=library_id, statuses=statuses)


@router.post(
    "/index/document",
    response_model=DocumentUploadResult,
    summary="Upload and index a single document attachment (remote mode)",
)
async def upload_and_index_document(
    http_request: Request,
    file: UploadFile = File(..., description="Raw attachment bytes"),
    metadata: str = Form(
        ...,
        description=(
            "JSON string with fields: library_id, library_type, item_key, "
            "attachment_key, mime_type, item_version, attachment_version, "
            "title, authors (array), year, item_type, "
            "zotero_modified (ISO 8601 string)"
        ),
    ),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Upload a single document attachment and index it on the backend.

    Used by the Zotero plugin when the backend is remote and cannot access
    local Zotero files.  The plugin reads the file bytes locally and POSTs
    them here together with item metadata.

    Form fields
    -----------
    file
        The raw attachment file (PDF, DOCX, HTML, EPUB …).
    metadata
        JSON-encoded document metadata.  Required fields: library_id,
        item_key, attachment_key.  All other fields default to safe values
        if omitted.

    Returns
    -------
    DocumentUploadResult with the number of chunks added or a skip/error
    status.

    Raises
    ------
    HTTPException 400
        If metadata JSON is invalid or required fields are missing.
    """
    # Parse metadata
    try:
        meta_dict = json.loads(metadata)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid metadata JSON: {e}")

    required = {"library_id", "item_key", "attachment_key"}
    missing = required - meta_dict.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required metadata fields: {sorted(missing)}",
        )

    library_id: str = meta_dict["library_id"]
    item_key: str = meta_dict["item_key"]
    attachment_key: str = meta_dict["attachment_key"]
    user_id: Optional[int] = meta_dict.get("user_id")
    _check_registration(library_id, user_id, get_settings())
    library_type: str = meta_dict.get("library_type", "user")
    mime_type: str = meta_dict.get("mime_type", "application/pdf")
    item_version: int = int(meta_dict.get("item_version", 0))
    attachment_version: int = int(meta_dict.get("attachment_version", 0))
    item_modified: str = meta_dict.get(
        "zotero_modified", datetime.now(timezone.utc).isoformat()
    )

    doc_metadata = DocumentMetadata(
        library_id=library_id,
        item_key=item_key,
        attachment_key=attachment_key,
        title=meta_dict.get("title", "Untitled"),
        authors=meta_dict.get("authors", []),
        year=meta_dict.get("year"),
        item_type=meta_dict.get("item_type"),
    )

    # Read file bytes
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    logger.info(
        f"Upload request: library={library_id} user={user_id} "
        f"item={item_key} attachment={attachment_key} mime={mime_type} "
        f"size={format_file_size(len(file_bytes))}"
    )

    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    content_hash = hashlib.sha256(file_bytes).hexdigest()

    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)
    if True:  # keep indentation for the block below
        # Run blocking Qdrant calls in a thread pool so the asyncio event loop
        # stays free for other requests (avoids NetworkError on slow Qdrant ops).
        if await asyncio.to_thread(vector_store.check_duplicate, content_hash, library_id):
            logger.info(
                f"Document {attachment_key} already indexed (hash {content_hash[:8]})"
            )
            return DocumentUploadResult(
                library_id=library_id,
                item_key=item_key,
                attachment_key=attachment_key,
                chunks_added=0,
                status="skipped_duplicate",
                message="Document already indexed (content hash match)",
            )

        # Delete stale chunks for this item before re-indexing
        if item_version > 0:
            stale = await asyncio.to_thread(vector_store.get_item_version, library_id, item_key)
            if stale is not None and stale < item_version:
                deleted = await asyncio.to_thread(vector_store.delete_item_chunks, library_id, item_key)
                logger.info(
                    f"Deleted {deleted} stale chunks for {item_key} "
                    f"(v{stale} → v{item_version})"
                )

        # Process: extract → embed → store
        # ZoteroLocalAPI is not needed here — the plugin already sent the bytes
        processor = DocumentProcessor(
            zotero_client=None,  # type: ignore[arg-type]
            embedding_service=embedding_service,
            vector_store=vector_store,
        )
        try:
            proc_result = await processor._process_attachment_bytes(
                file_bytes=file_bytes,
                mime_type=mime_type,
                doc_metadata=doc_metadata,
                item_version=item_version,
                attachment_version=attachment_version,
                item_modified=item_modified,
            )
            chunks_added = proc_result.chunks_written
            logger.info(
                f"[DIAG] upload result: attachment={attachment_key} "
                f"status={proc_result.status} chunks={chunks_added} "
                f"mime={mime_type} size_bytes={len(file_bytes)}"
            )

            # Update library metadata so index-status reflects this upload
            lib_meta = await asyncio.to_thread(vector_store.get_library_metadata, library_id)
            if lib_meta is None:
                lib_meta = LibraryIndexMetadata(
                    library_id=library_id,
                    library_type=library_type,
                    library_name=meta_dict.get("library_name", ""),
                    indexing_mode="incremental",
                )
            lib_meta.last_indexed_version = max(
                lib_meta.last_indexed_version, item_version
            )
            lib_meta.last_indexed_at = datetime.now(timezone.utc).isoformat()
            lib_meta.total_chunks = await asyncio.to_thread(vector_store.count_library_chunks, library_id)
            if proc_result.status in ("indexed_fresh", "copied_cross_library"):
                lib_meta.total_items_indexed += 1
            await asyncio.to_thread(vector_store.update_library_metadata, lib_meta)
        except Exception as e:
            import openai
            from qdrant_client.http.exceptions import ResponseHandlingException
            from httpx import WriteTimeout, ReadTimeout, TimeoutException
            if isinstance(e, openai.InternalServerError):
                logger.warning(
                    f"Upstream embedding service error for {attachment_key}: {e}"
                )
            elif isinstance(e, (WriteTimeout, ReadTimeout, TimeoutException)):
                logger.warning(
                    f"Qdrant write timed out while storing chunks for {attachment_key} "
                    f"— batch may be too large or Qdrant is under load"
                )
            elif isinstance(e, ResponseHandlingException) and "timed out" in str(e).lower():
                logger.warning(
                    f"Qdrant request timed out while storing chunks for {attachment_key} "
                    f"— batch may be too large or Qdrant is under load"
                )
            elif isinstance(e, RuntimeError) and "kreuzberg sidecar" in str(e):
                logger.warning(f"Error processing upload for {attachment_key}: {e}")
            else:
                logger.error(
                    f"Error processing upload for {attachment_key}: {e}", exc_info=True
                )
            return DocumentUploadResult(
                library_id=library_id,
                item_key=item_key,
                attachment_key=attachment_key,
                chunks_added=0,
                status="error",
                message=str(e),
            )

    api_status = "indexed" if proc_result.status == "indexed_fresh" else proc_result.status
    if proc_result.status in (
        "indexed_fresh", "copied_cross_library",
        "skipped_empty", "skipped_timeout", "skipped_parse_error", "skipped_duplicate",
    ):
        _update_item_cache(library_id, {item_key: {"item_version": item_version, "schema_version": CURRENT_SCHEMA_VERSION}})
    return DocumentUploadResult(
        library_id=library_id,
        item_key=item_key,
        attachment_key=attachment_key,
        chunks_added=chunks_added,
        status=api_status,
        message=f"{api_status}: {chunks_added} chunks",
        rate_limit_retries=embedding_service.rate_limit_retries,
        rate_limit_headers=await embedding_service.get_rate_limit_info(),
    )


@router.post(
    "/index/abstract",

    response_model=DocumentUploadResult,
    summary="Index an item via its abstractNote (remote mode, no attachment file)",
)
async def upload_and_index_abstract(
    http_request: Request,
    request: AbstractIndexRequest,
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Index a Zotero item using its abstractNote when no attachment file is available.

    The abstract is chunked and embedded directly.  A virtual attachment key
    ``{item_key}:abstract`` is used so the chunks can be tracked independently.
    The abstract must meet the configured minimum word count (MIN_ABSTRACT_WORDS).
    """
    settings = get_settings()
    _check_registration(request.library_id, request.user_id, settings)
    word_count = len(request.abstract_text.split())
    logger.info(
        f"Abstract index: library={request.library_id} user={request.user_id} "
        f"item={request.item_key} words={word_count}"
    )
    if word_count < settings.min_abstract_words:
        raise HTTPException(
            status_code=400,
            detail=f"Abstract too short: {word_count} words (minimum {settings.min_abstract_words})",
        )

    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    abstract_key = f"{request.item_key}:abstract"
    doc_metadata = DocumentMetadata(
        library_id=request.library_id,
        item_key=request.item_key,
        attachment_key=abstract_key,
        title=request.title or "Untitled",
        authors=request.authors,
        year=request.year,
        item_type=request.item_type,
    )

    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)

    processor = DocumentProcessor(
        zotero_client=None,  # type: ignore[arg-type]
        embedding_service=embedding_service,
        vector_store=vector_store,
    )

    try:
        # Delete stale chunks for this item before re-indexing
        if request.item_version > 0:
            stale = await asyncio.to_thread(vector_store.get_item_version, request.library_id, request.item_key)
            if stale is not None and stale < request.item_version:
                deleted = await asyncio.to_thread(vector_store.delete_item_chunks, request.library_id, request.item_key)
                logger.info(
                    f"Deleted {deleted} stale chunks for {request.item_key} "
                    f"(v{stale} -> v{request.item_version})"
                )

        chunks_added = await processor._index_from_abstract(
            abstract_text=request.abstract_text,
            doc_metadata=doc_metadata,
            item_version=request.item_version,
            item_modified=request.zotero_modified or datetime.now(timezone.utc).isoformat(),
        )

        # Update library metadata
        lib_meta = await asyncio.to_thread(vector_store.get_library_metadata, request.library_id)
        if lib_meta is None:
            lib_meta = LibraryIndexMetadata(
                library_id=request.library_id,
                library_type=request.library_type,
                indexing_mode="incremental",
            )
        lib_meta.last_indexed_version = max(lib_meta.last_indexed_version, request.item_version)
        lib_meta.last_indexed_at = datetime.now(timezone.utc).isoformat()
        lib_meta.total_chunks = await asyncio.to_thread(vector_store.count_library_chunks, request.library_id)
        lib_meta.total_items_indexed += 1
        await asyncio.to_thread(vector_store.update_library_metadata, lib_meta)

    except Exception as e:
        logger.error(f"Error indexing abstract for {request.item_key}: {e}", exc_info=True)
        return DocumentUploadResult(
            library_id=request.library_id,
            item_key=request.item_key,
            attachment_key=abstract_key,
            chunks_added=0,
            status="error",
            message=str(e),
        )

    status = "indexed" if chunks_added > 0 else "skipped_duplicate"
    if status == "indexed":
        _update_item_cache(request.library_id, {request.item_key: {"item_version": request.item_version, "schema_version": CURRENT_SCHEMA_VERSION}})
    return DocumentUploadResult(
        library_id=request.library_id,
        item_key=request.item_key,
        attachment_key=abstract_key,
        chunks_added=chunks_added,
        status=status,
        message=f"Indexed {chunks_added} abstract chunks" if chunks_added > 0 else "Abstract already indexed",
        rate_limit_retries=embedding_service.rate_limit_retries,
        rate_limit_headers=await embedding_service.get_rate_limit_info(),
    )


@router.post(
    "/index/items/metadata",
    response_model=BatchMetadataUpdateResult,
    summary="Update payload metadata for existing chunks without re-embedding",
)
def batch_update_metadata(
    request: BatchMetadataUpdateRequest,
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Update bibliographic metadata fields on existing indexed chunks without re-embedding.

    Used by the plugin after check-indexed returns needs_metadata_update=True for items
    whose schema_version is below the current version (e.g. item_type was added in v3).

    No file bytes are uploaded; the backend calls Qdrant set_payload() directly.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    updated_items = 0
    updated_chunks = 0

    for item in request.items:
        fields: dict = {"schema_version": CURRENT_SCHEMA_VERSION}
        if item.title is not None:
            fields["title"] = item.title
        if item.authors:
            fields["authors"] = item.authors
            fields["author_lastnames"] = _extract_lastnames(item.authors)
        if item.year is not None:
            fields["year"] = item.year
        if item.item_type is not None:
            fields["item_type"] = item.item_type

        n = vector_store.update_item_metadata(request.library_id, item.item_key, fields)
        if n > 0:
            updated_items += 1
            updated_chunks += n

    logger.info(
        f"Metadata update: library={request.library_id} "
        f"items={updated_items}/{len(request.items)} chunks={updated_chunks}"
    )
    # Update cache: bump schema_version for each successfully updated item
    # (item_version unchanged — preserve whatever was already cached)
    for item in request.items:
        with _check_indexed_item_cache_lock:
            lib = _check_indexed_item_cache.get(request.library_id, {})
            if item.item_key in lib and lib[item.item_key] is not None:
                lib[item.item_key] = {**lib[item.item_key], "schema_version": CURRENT_SCHEMA_VERSION}
    return BatchMetadataUpdateResult(
        library_id=request.library_id,
        updated_items=updated_items,
        updated_chunks=updated_chunks,
    )
