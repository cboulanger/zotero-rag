"""API routes for chapter segmentation & book/chapter linking: analyze, OCR,
retrofit-link, segment-upload, and a shared job-status poll.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 4. Each POST .../{analyze,ocr,retrofit-link,segment-upload} endpoint
is added in its own phase (Tasks 12, 17, 23, 29) once that script's core
run() function exists; this task only wires up the shared job registry and
its polling endpoint.
"""

import asyncio
import logging
from pathlib import Path as _Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pyzotero import zotero

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_ocr import run as ocr_run
from backend.services.chapter_retrofit import run as retrofit_run
from backend.services.chapter_segmentation import run as analyze_run
from backend.services.extraction import create_document_extractor
from backend.services.job_tracker import JobTracker
from backend.zotero.web_api import ZoteroWebAPI

logger = logging.getLogger(__name__)
router = APIRouter()

# Module-level singleton, shared by every chapter-linking endpoint in this
# router — mirrors backend/api/document_upload.py's module-level _upload_tasks.
tracker = JobTracker()


class JobStatusResponse(BaseModel):
    """Response from the job-status poll endpoint."""

    job_id: str
    status: str  # "processing" | "done" | "error"
    progress: float
    message: str
    result: Any | None = None
    error: str | None = None


@router.get(
    "/chapter-linking/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll the status of a chapter-linking background job",
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = tracker.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id!r} not found (may have expired or never existed)",
        )
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        result=job.result,
        error=job.error,
    )


class AnalyzeRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    relink: bool = False
    max_items: int | None = None


class JobIdResponse(BaseModel):
    """Response containing the job ID from analyze/OCR/retrofit-link/segment-upload endpoints."""

    job_id: str


@router.post("/chapter-linking/analyze", response_model=JobIdResponse, summary="Analyze book PDFs for chapter-segmentation candidates")
async def start_analyze(request: AnalyzeRequest) -> JobIdResponse:
    library_type, _numeric_id, library_id = parse_library_slug(request.library_slug)
    client = ZoteroWebAPI(api_key=request.api_key)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await analyze_run(
                zotero_client=client,
                library_id=library_id,
                library_type=library_type,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                relink=request.relink,
                progress_callback=lambda p, m: tracker.update(job_id, progress=p, message=m),
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001 — surfaced via job status, not re-raised
            logger.exception("chapter-linking analyze job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return JobIdResponse(job_id=job_id)


class AttachmentSpec(BaseModel):
    item_key: str
    attachment_key: str


class OcrRequest(BaseModel):
    library_slug: str
    api_key: str
    attachment_specs: list[AttachmentSpec]
    max_items: int | None = None
    cache_dir: str = "data/ocr_cache"


@router.post(
    "/chapter-linking/ocr",
    response_model=JobIdResponse,
    summary="OCR attachments lacking a text layer",
)
async def start_ocr(request: OcrRequest) -> JobIdResponse:
    library_type, _numeric_id, library_id = parse_library_slug(request.library_slug)
    client = ZoteroWebAPI(api_key=request.api_key)
    extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await ocr_run(
                zotero_client=client,
                extractor=extractor,
                library_id=library_id,
                library_type=library_type,
                attachment_specs=[s.model_dump() for s in request.attachment_specs],
                max_items=request.max_items,
                cache_dir=_Path(request.cache_dir),
                progress_callback=lambda p, m: tracker.update(job_id, progress=p, message=m),
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking ocr job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return JobIdResponse(job_id=job_id)


class RetrofitLinkRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    max_items: int | None = None


@router.post(
    "/chapter-linking/retrofit-link",
    response_model=JobIdResponse,
    summary="Retrofit-link existing book/bookSection item pairs",
)
async def start_retrofit_link(request: RetrofitLinkRequest) -> JobIdResponse:
    library_type, numeric_id, _library_id = parse_library_slug(request.library_slug)
    zot = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=request.api_key)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await asyncio.to_thread(
                retrofit_run,
                zotero_write_client=zot,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking retrofit-link job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return JobIdResponse(job_id=job_id)
