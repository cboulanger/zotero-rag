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

from backend.dependencies import make_llm_service
from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_ocr import run as ocr_run
from backend.services.chapter_retrofit import run as retrofit_run
from backend.services.chapter_segmentation import run as analyze_run
from backend.services.chapter_upload import run as upload_run
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
    enable_llm_fallback: bool = False
    # With enable_llm_fallback: retry across the active preset's available
    # models (most-available first) on error or an unusable response,
    # instead of a single fixed model. Never a hardcoded model name --
    # resolved live from the preset/provider (see make_llm_service).
    auto_select_model: bool = False
    # Same default as OcrRequest.cache_dir -- lets a re-run after the /ocr
    # endpoint pick up already-OCR'd page text automatically.
    ocr_cache_dir: str = "data/ocr_cache"


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
            # make_llm_service(auto_select_model=True) can synchronously call
            # a live models-status endpoint (e.g. KISSKI, via httpx.post) --
            # run it in a thread so it never blocks the event loop (this task
            # already runs off the request/response path via
            # asyncio.create_task below, but it still executes on the same
            # event loop once scheduled).
            llm_service = (
                await asyncio.to_thread(make_llm_service, auto_select_model=request.auto_select_model)
                if request.enable_llm_fallback else None
            )
            result = await analyze_run(
                zotero_client=client,
                library_id=library_id,
                library_type=library_type,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                relink=request.relink,
                progress_callback=lambda p, m: tracker.update(job_id, progress=p, message=m),
                llm_service=llm_service,
                ocr_cache_dir=_Path(request.ocr_cache_dir),
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
    committed: bool = False
    # A prior dry run's `would_link` list (this endpoint's own response
    # shape, see JobStatusResponse.result). When given together with
    # committed=True, chapter_retrofit.run() replays it instead of
    # re-fetching and re-matching the whole library -- see run()'s
    # docstring in backend/services/chapter_retrofit.py.
    would_link: list[dict] | None = None


@router.post(
    "/chapter-linking/retrofit-link",
    response_model=JobIdResponse,
    summary="Retrofit-link existing book/bookSection item pairs (dry-run by default)",
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
                commit=request.committed,
                would_link=request.would_link,
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking retrofit-link job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return JobIdResponse(job_id=job_id)


class SegmentUploadRequest(BaseModel):
    library_slug: str
    api_key: str
    analyses: list[dict]
    committed: bool = False
    # Calibrated against the real 7-book evaluation set (backend/evaluation/
    # book-segmentation/README.md "Current results"): with the current
    # heuristics ~91% of ALL proposed chapters are already exactly correct,
    # and the sweep shows precision-among-kept barely moves with the
    # threshold (0.91 at 0.5 -> 0.93 at 0.96) while recall of correct
    # chapters collapses above ~0.94 (0.96 keeps only 78% of them) -- the
    # remaining errors are end-boundary quirks the start-match confidence
    # cannot see. 0.90 keeps 97% of correct chapters at 0.91 precision.
    # Re-calibrate (see backend/evaluation/book-segmentation/README.md's
    # "Running an evaluation" section) if match_confidence's formula
    # changes, the locate heuristics change, or the evaluation set grows
    # meaningfully -- this value is not stable across such changes and has
    # been re-picked twice already (0.98 -> 0.96 -> 0.90).
    confidence_threshold: float = 0.90
    target_collection: str = "Book Chapters"
    max_items: int | None = None


@router.post(
    "/chapter-linking/segment-upload",
    response_model=JobIdResponse,
    summary="Segment book PDFs into chapters and upload (dry-run by default)",
)
async def start_segment_upload(request: SegmentUploadRequest) -> JobIdResponse:
    library_type, numeric_id, _library_id = parse_library_slug(request.library_slug)
    write_client = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=request.api_key)
    read_client = ZoteroWebAPI(api_key=request.api_key)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await upload_run(
                zotero_write_client=write_client,
                zotero_read_client=read_client,
                slug=request.library_slug,
                analyses=request.analyses,
                commit=request.committed,
                confidence_threshold=request.confidence_threshold,
                target_collection=request.target_collection,
                max_items=request.max_items,
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking segment-upload job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return JobIdResponse(job_id=job_id)
