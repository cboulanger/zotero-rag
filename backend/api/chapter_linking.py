"""API routes for chapter segmentation & book/chapter linking: analyze, OCR,
retrofit-link, segment-upload, and a shared job-status poll.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 4. Each POST .../{analyze,ocr,retrofit-link,segment-upload} endpoint
is added in its own phase (Tasks 12, 17, 23, 29) once that script's core
run() function exists; this task only wires up the shared job registry and
its polling endpoint.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.job_tracker import JobTracker

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
