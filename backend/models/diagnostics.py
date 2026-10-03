"""Pydantic models for opt-in per-request upload diagnostics.

Returned in ``DocumentUploadResult.diagnostics`` only when the client sets
``include_diagnostics`` (used by the plugin's Fix Unavailable "Download
debugging information" option).
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class DiagnosticLogRecord(BaseModel):
    """One server log record emitted while the request was being processed."""

    ts: str  # ISO-8601 UTC
    level: str  # DEBUG | INFO | WARNING | ERROR | CRITICAL
    logger: str
    message: str
    exc_text: Optional[str] = None  # formatted traceback, if the record had exc_info


class DiagnosticStage(BaseModel):
    """Timing, outcome and details of one processing stage."""

    name: str  # dedup_check | stale_chunk_purge | extraction | embedding | store | library_metadata
    started_at: str
    duration_ms: int
    outcome: str  # ok | skipped | error
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsPayload(BaseModel):
    """Everything the server can tell about how one upload was processed."""

    request_id: str  # also logged on the server for cross-reference
    server: dict[str, Any] = Field(default_factory=dict)  # allowlisted, non-secret
    stages: list[DiagnosticStage] = Field(default_factory=list)
    log_records: list[DiagnosticLogRecord] = Field(default_factory=list)
    final_status: str = ""
    error: Optional[dict[str, str]] = None  # {type, message, traceback}
    truncated: dict[str, int] = Field(default_factory=dict)
