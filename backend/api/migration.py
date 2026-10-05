"""
Cross-instance library RAG data migration endpoints.

Lets an operator copy one Zotero library's indexed vectors
(document_chunks), dedup records, and index metadata from one zotero-rag
instance to another (e.g. production -> local dev) via
bin/migrate_library.py, without either side needing direct Qdrant access.
See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md.

Every endpoint requires require_authorized_group_admin — the same
dependency gating the autoindex scheduler's admin controls — evaluated
against *this* instance's own AUTHORIZED_GROUP_ID. The source and
destination admin accounts need not match; this is an instance-admin
action, not a per-library-access check (unlike assert_can_access used by
the normal library/query endpoints).
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store, require_authorized_group_admin
from backend.models.library import LibraryIndexMetadata
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()
logger = logging.getLogger(__name__)

MigrationCollection = Literal["chunks", "dedup"]


class EmbeddingInfoResponse(BaseModel):
    """This instance's embedding model/dimension, for the migration script's compatibility check."""
    embedding_model_name: str
    embedding_dim: int


class ExportPoint(BaseModel):
    """One raw Qdrant point as transferred between instances."""
    id: str
    vector: list[float]
    payload: dict


class ExportPageResponse(BaseModel):
    """One page of exported points plus the cursor for the next page (None when done)."""
    points: list[ExportPoint]
    next_offset: Optional[str] = None


class ImportBeginRequest(BaseModel):
    library_id: str


class ImportBeginResponse(BaseModel):
    """Counts of what import/begin deleted on the destination before any new data is written."""
    library_id: str
    chunks_deleted: int
    dedup_deleted: int
    metadata_deleted: bool


class ImportPointsRequest(BaseModel):
    library_id: str
    points: list[ExportPoint]


class ImportMetadataRequest(BaseModel):
    library_id: str
    payload: dict


@router.get("/migration/embedding-info", response_model=EmbeddingInfoResponse)
def get_embedding_info(
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Report this instance's embedding model/dimension (admin only)."""
    info = vector_store.get_collection_info()
    return EmbeddingInfoResponse(
        embedding_model_name=info["embedding_model_name"],
        embedding_dim=info["embedding_dim"],
    )


@router.get("/migration/export/metadata", response_model=LibraryIndexMetadata)
def export_library_metadata(
    library_id: str,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Export a library's index metadata point (admin only). 404 if never indexed here."""
    metadata = vector_store.get_library_metadata(library_id)
    if metadata is None:
        raise HTTPException(
            status_code=404,
            detail=f"Library {library_id!r} has never been indexed on this instance.",
        )
    return metadata


@router.get("/migration/export/count")
def export_library_point_count(
    library_id: str,
    collection: MigrationCollection,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> dict:
    """Report how many points a library has in document_chunks or deduplication (admin only).

    Used by the migration script to show progress ("N/total transferred")
    while paginating GET /migration/export.
    """
    return {"count": vector_store.count_library_points(collection, library_id)}


@router.get("/migration/export", response_model=ExportPageResponse)
def export_library_points(
    library_id: str,
    collection: MigrationCollection,
    offset: Optional[str] = None,
    limit: int = 200,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Export one page of a library's document_chunks or deduplication points (admin only)."""
    points, next_offset = vector_store.export_points(collection, library_id, offset, limit)
    return ExportPageResponse(points=points, next_offset=next_offset)


@router.post("/migration/import/begin", response_model=ImportBeginResponse)
def import_begin(
    body: ImportBeginRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Clear this instance's existing chunks/dedup/metadata for a library (admin only).

    Always called before any import batch — the migration overwrites the
    destination's data for the library rather than merging into it.
    """
    chunks_deleted = vector_store.delete_library_chunks(body.library_id)
    dedup_deleted = vector_store.delete_library_deduplication_records(body.library_id)
    metadata_deleted = vector_store.delete_library_metadata(body.library_id)
    return ImportBeginResponse(
        library_id=body.library_id,
        chunks_deleted=chunks_deleted,
        dedup_deleted=dedup_deleted,
        metadata_deleted=metadata_deleted,
    )


@router.post("/migration/import")
def import_points_batch(
    collection: MigrationCollection,
    body: ImportPointsRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> dict:
    """Import one batch of exported points into document_chunks or deduplication (admin only)."""
    for point in body.points:
        point_library_id = point.payload.get("library_id")
        if point_library_id != body.library_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Point {point.id!r} has payload.library_id={point_library_id!r}, "
                    f"which does not match the request's library_id={body.library_id!r}."
                ),
            )
    imported = vector_store.import_points(
        collection,
        [p.model_dump() for p in body.points],
    )
    return {"imported": imported}


@router.post("/migration/import/metadata")
def import_library_metadata(
    body: ImportMetadataRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> dict:
    """Import a library's index metadata point, exactly as returned by export/metadata (admin only)."""
    if body.payload.get("library_id") != body.library_id:
        raise HTTPException(status_code=400, detail="payload.library_id does not match library_id.")
    try:
        metadata = LibraryIndexMetadata(**body.payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    vector_store.update_library_metadata(metadata)
    return {"library_id": body.library_id}
