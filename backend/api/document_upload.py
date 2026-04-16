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
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.models.document import (
    DeduplicationRecord,
    DocumentMetadata,
)
from backend.services.document_processor import DocumentProcessor
from backend.services.embeddings import create_embedding_service

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


# ---------------------------------------------------------------------------
# Helper: build services for the upload path
# ---------------------------------------------------------------------------


def _make_embedding_service():
    """Create an EmbeddingService from the current settings."""
    settings = get_settings()
    preset = settings.get_hardware_preset()
    return create_embedding_service(
        preset.embedding,
        cache_dir=str(settings.model_weights_path),
        hf_token=settings.get_api_key("HF_TOKEN"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/libraries/{library_id}/check-indexed",
    response_model=CheckIndexedResponse,
    summary="Check which attachments need indexing (remote mode)",
)
async def check_indexed(library_id: str, request: CheckIndexedRequest):
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
    """
    if library_id != request.library_id:
        raise HTTPException(
            status_code=400,
            detail="library_id in URL must match library_id in request body",
        )

    settings = get_settings()
    statuses: list[AttachmentIndexStatus] = []

    embedding_service = _make_embedding_service()
    with VectorStore(
        storage_path=settings.vector_db_path,
        embedding_dim=embedding_service.get_embedding_dim(),
    ) as vector_store:
        for att in request.attachments:
            indexed_version = vector_store.get_item_version(library_id, att.item_key)

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

    return CheckIndexedResponse(library_id=library_id, statuses=statuses)


@router.post(
    "/index/document",
    response_model=DocumentUploadResult,
    summary="Upload and index a single document attachment (remote mode)",
)
async def upload_and_index_document(
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

    settings = get_settings()
    content_hash = hashlib.sha256(file_bytes).hexdigest()

    embedding_service = _make_embedding_service()
    with VectorStore(
        storage_path=settings.vector_db_path,
        embedding_dim=embedding_service.get_embedding_dim(),
    ) as vector_store:
        if vector_store.check_duplicate(content_hash):
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
        except Exception as e:
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
    )
