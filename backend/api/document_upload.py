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

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from backend.db.vector_store import VectorStore
from backend.dependencies import get_client_api_keys, get_vector_store, make_embedding_service
from backend.models.document import (
    DocumentMetadata,
)
from backend.models.library import LibraryIndexMetadata
from backend.services.document_processor import DocumentProcessor
from backend.config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)


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


class AttachmentIndexStatus(BaseModel):
    """Per-attachment result returned by the check endpoint."""

    item_key: str
    attachment_key: str
    needs_indexing: bool
    reason: str  # "not_indexed" | "version_changed" | "up_to_date"


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

    indexed_versions = vector_store.get_item_versions_bulk(
        library_id, [att.item_key for att in request.attachments]
    )

    for att in request.attachments:
        indexed_version = indexed_versions.get(att.item_key)

        if indexed_version is None:
            statuses.append(AttachmentIndexStatus(
                item_key=att.item_key,
                attachment_key=att.attachment_key,
                needs_indexing=True,
                reason="not_indexed",
            ))
        elif indexed_version < att.item_version:
            statuses.append(AttachmentIndexStatus(
                item_key=att.item_key,
                attachment_key=att.attachment_key,
                needs_indexing=True,
                reason="version_changed",
            ))
        else:
            statuses.append(AttachmentIndexStatus(
                item_key=att.item_key,
                attachment_key=att.attachment_key,
                needs_indexing=False,
                reason="up_to_date",
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
    library_type: str = meta_dict.get("library_type", "user")
    mime_type: str = meta_dict.get("mime_type", "application/pdf")
    item_version: int = int(meta_dict.get("item_version", 0))
    attachment_version: int = int(meta_dict.get("attachment_version", 0))
    item_modified: str = meta_dict.get(
        "zotero_modified", datetime.utcnow().isoformat()
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

    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    content_hash = hashlib.sha256(file_bytes).hexdigest()

    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)
    if True:  # keep indentation for the block below
        if vector_store.check_duplicate(content_hash, library_id=library_id):
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
            stale = vector_store.get_item_version(library_id, item_key)
            if stale is not None and stale < item_version:
                deleted = vector_store.delete_item_chunks(library_id, item_key)
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
            chunks_added = await processor._process_attachment_bytes(
                file_bytes=file_bytes,
                mime_type=mime_type,
                doc_metadata=doc_metadata,
                item_version=item_version,
                attachment_version=attachment_version,
                item_modified=item_modified,
            )

            # Update library metadata so index-status reflects this upload
            lib_meta = vector_store.get_library_metadata(library_id)
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
            lib_meta.total_chunks = vector_store.count_library_chunks(library_id)
            lib_meta.total_items_indexed += 1
            vector_store.update_library_metadata(lib_meta)
        except Exception as e:
            import openai
            if isinstance(e, openai.InternalServerError):
                logger.warning(
                    f"Upstream embedding service error for {attachment_key}: {e}"
                )
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

    return DocumentUploadResult(
        library_id=library_id,
        item_key=item_key,
        attachment_key=attachment_key,
        chunks_added=chunks_added,
        status="indexed",
        message=f"Indexed {chunks_added} chunks",
        rate_limit_retries=embedding_service.rate_limit_retries,
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
    word_count = len(request.abstract_text.split())
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
            stale = vector_store.get_item_version(request.library_id, request.item_key)
            if stale is not None and stale < request.item_version:
                deleted = vector_store.delete_item_chunks(request.library_id, request.item_key)
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
        lib_meta = vector_store.get_library_metadata(request.library_id)
        if lib_meta is None:
            lib_meta = LibraryIndexMetadata(
                library_id=request.library_id,
                library_type=request.library_type,
                library_name=request.library_name,
                indexing_mode="incremental",
            )
        lib_meta.last_indexed_version = max(lib_meta.last_indexed_version, request.item_version)
        lib_meta.last_indexed_at = datetime.now(timezone.utc).isoformat()
        lib_meta.total_chunks = vector_store.count_library_chunks(request.library_id)
        lib_meta.total_items_indexed += 1
        vector_store.update_library_metadata(lib_meta)

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
    return DocumentUploadResult(
        library_id=request.library_id,
        item_key=request.item_key,
        attachment_key=abstract_key,
        chunks_added=chunks_added,
        status=status,
        message=f"Indexed {chunks_added} abstract chunks" if chunks_added > 0 else "Abstract already indexed",
        rate_limit_retries=embedding_service.rate_limit_retries,
    )
